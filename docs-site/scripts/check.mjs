import assert from 'node:assert/strict';
import { readFile, readdir, stat } from 'node:fs/promises';
import { resolve, relative, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { loadPages, toolsIn } from './content.mjs';
import { diagramNames, renderDiagram } from './diagrams.mjs';
import { scoreRank } from './experiment-tables.mjs';

const root=fileURLToPath(new URL('../',import.meta.url));
const out=resolve(root,'dist');
const pages=await loadPages(root);
const htmlEscape=value=>String(value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function checkLocales(value) {
  if (!value || typeof value !== 'object') return;
  if ('zh' in value || 'en' in value) {
    assert.equal(typeof value.zh,'string','Missing Chinese content');
    assert.equal(typeof value.en,'string','Missing English content');
    assert.ok(value.zh.trim() && value.en.trim(),'Empty translation');
  }
  for(const child of Object.values(value)) checkLocales(child);
}
checkLocales(pages);
assert.equal(new Set(pages.map(page=>page.slug)).size,pages.length,'Duplicate page slug');
for(const page of pages) {
  assert.match(page.slug,/^[a-z0-9-]+$/);
  assert.equal(new Set(page.sections.map(section=>section.id)).size,page.sections.length,'Duplicate section anchor');
  for(const section of page.sections) for(const block of section.blocks) {
    if(block.type==='code' && block.language==='json') assert.doesNotThrow(()=>JSON.parse(block.code),`Invalid JSON example: ${page.slug}#${section.id}`);
    if(block.type==='cards') for(const item of block.items) assert.ok(pages.some(page=>page.slug===item.slug),`Missing card target ${item.slug}`);
    if(block.type==='table') for(const row of block.rows) assert.equal(row.length,block.headers.length,'Invalid table row');
    if(block.type==='experiment-table') {
      assert.equal(block.baseModel,'GLM-5-turbo','Unexpected experiment base model');
      assert.equal(block.metric,'SR','Only SR belongs in the experiment tables');
      assert.equal(block.unit,'percent');
      const columns=block.groups.reduce((count,group)=>count+group.tasks.length,0);
      assert.equal(new Set(block.rows.map(row=>row.harness)).size,block.rows.length,'Duplicate harness');
      for(const row of block.rows) {
        assert.ok(['Hermes','Claude Code','OpenClaw','iGraph'].includes(row.harness),'Unexpected harness');
        assert.equal(row.scores.length,columns,'Invalid experiment row');
        assert.ok(row.source,'Missing result provenance');
        for(const score of row.scores) assert.ok(Number.isFinite(score) && score>=0 && score<=100,'SR must be a percentage');
      }
    }
    if(block.type==='tools') for(const tool of block.items) {
      assert.match(tool.name,/^[a-z0-9_]+$/);
      assert.equal(new Set(tool.parameters.map(param=>param.name)).size,tool.parameters.length,'Duplicate tool parameter');
      assert.ok(tool.result.zh && tool.result.en,'Missing tool result');
    }
  }
}
async function filesAt(dir) {
  const entries=await readdir(dir,{withFileTypes:true});
  return (await Promise.all(entries.map(entry=>entry.isDirectory()?filesAt(resolve(dir,entry.name)):resolve(dir,entry.name)))).flat();
}
const files=await filesAt(out);
assert.ok(files.length<20000,'Cloudflare Free static file count exceeded');
let total=0;
let checkedLinks=0;
for(const file of files) {
  const size=(await stat(file)).size;
  total+=size;
  assert.ok(size<=25*1024*1024,`Cloudflare single-file limit: ${file}`);
  assert.ok(!/\.(env|py|pt|graphml|sqlite)$/i.test(file),'Application data must not be published');
  if(extname(file)==='.sql') assert.equal(relative(out,file).replaceAll('\\','/'),'examples/commerce.sql','Only the authored commerce SQL example may be published');
  if(extname(file)!=='.html') continue;
  const body=await readFile(file,'utf8');
  const ids=[...body.matchAll(/\sid="([^"]+)"/g)].map(match=>match[1]);
  assert.equal(new Set(ids).size,ids.length,`Duplicate HTML id: ${file}`);
  // Descriptions may legitimately discuss undefined metrics or undefined_reason.
  // Reject missing-value placeholders, rather than that English word in prose.
  assert.ok(!/>\s*undefined\s*</.test(body) && !/="undefined"/.test(body),`Undefined content rendered: ${relative(out,file)}`);
  assert.ok(!/<script(?![^>]*\bsrc=)[^>]*>\s*[^<\s]/.test(body),'Inline script violates CSP');
  const base='https://docs.invalid/'+relative(out,file).replaceAll('\\','/');
  for(const match of body.matchAll(/\s(?:href|src|data-markdown)="([^"]+)"/g)) {
    const value=match[1];
    if (/^(https?:|mailto:|data:)/.test(value)) continue;
    const url=new URL(value,base);
    let target=resolve(out,'.'+url.pathname);
    if(url.pathname.endsWith('/')) target=resolve(target,'index.html');
    assert.ok(files.includes(target),`Broken local link ${value} in ${relative(out,file)}`);
    if(url.hash && extname(target)==='.html') {
      const targetHtml=target===file?body:await readFile(target,'utf8');
      assert.ok(targetHtml.includes(`id="${decodeURIComponent(url.hash.slice(1))}"`),`Missing anchor ${value}`);
    }
    checkedLinks++;
  }
  for(const match of body.matchAll(/data-copy="([^"]+)"/g)) assert.ok(ids.includes(match[1]),'Broken code-copy target');
}
for(const locale of ['zh','en']) for(const page of pages) {
  const html=await readFile(resolve(out,locale,page.slug,'index.html'),'utf8');
  assert.ok(html.includes(locale==='zh'?'lang="zh-CN"':'lang="en"'));
  assert.ok(html.includes(htmlEscape(page.title[locale])),`Missing escaped page title: ${page.slug}/${locale}`);
  assert.ok((await readFile(resolve(out,'markdown',locale,page.slug+'.md'),'utf8')).startsWith('# '+page.title[locale]));
}
const search=JSON.parse(await readFile(resolve(out,'search-index.json'),'utf8'));
const experiments=pages.find(page=>page.slug==='experiments');
const experimentTables=experiments.sections.flatMap(section=>section.blocks.filter(block=>block.type==='experiment-table'));
assert.equal(experimentTables.length,4,'Expected four experiment tables');
assert.deepEqual(experimentTables.map(table=>table.rows.length),[2,2,4,4],'Unexpected harness coverage');
// Covers ties and columns where iGraph is second rather than first.
const tiedScores=[{scores:[95]},{scores:[95]},{scores:[90]},{scores:[80]}];
assert.deepEqual(tiedScores.map(row=>scoreRank(tiedScores,0,row.scores[0])),[1,1,2,3]);
for(const locale of ['zh','en']) {
  const html=await readFile(resolve(out,locale,'experiments/index.html'),'utf8');
  const markdown=await readFile(resolve(out,'markdown',locale,'experiments.md'),'utf8');
  const tables=[...html.matchAll(/<table class="experiment-table">([\s\S]*?)<\/table>/g)];
  assert.equal(tables.length,4,'Missing rendered experiment table');
  assert.ok(!/TSA|Qwen|DeepSeek|GPT-5-mini|Gemini|GLM-4\.5/.test(html),'Unexpected metric or base model');
  for(const [index,match] of tables.entries()) {
    const table=experimentTables[index];
    const renderedRows=[...match[1].matchAll(/<tr[^>]*><th scope="row">([^<]+)<\/th>([\s\S]*?)<\/tr>/g)];
    assert.equal(renderedRows.length,table.rows.length);
    for(const [rowIndex,row] of table.rows.entries()) {
      assert.equal(renderedRows[rowIndex][1],row.harness);
      const cells=[...renderedRows[rowIndex][2].matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/g)].map(cell=>cell[1]);
      assert.equal(cells.length,row.scores.length);
      row.scores.forEach((value,column)=>{
        assert.equal(cells[column].replace(/<[^>]+>/g,''),value.toFixed(2),'Rendered SR differs from source');
        const higher=[...new Set(table.rows.map(other=>other.scores[column]))].filter(score=>score>value).length;
        assert.equal(cells[column].startsWith('<strong'),higher===0,'Incorrect highest-score marking');
        assert.equal(cells[column].startsWith('<u '),higher===1,'Incorrect second-highest-score marking');
      });
    }
  }
  for(const score of ['95.23','88.89']) assert.ok(tables[2][1].includes(`>${score}</strong>`),'Claude Code best score missing');
  for(const score of ['90.00','85.00']) assert.ok(tables[2][1].includes(`>${score}</u>`),'iGraph second-best score missing');
  assert.equal((markdown.match(/<strong /g)||[]).length,29,'Markdown must preserve highest scores');
  assert.equal((markdown.match(/<u /g)||[]).length,29,'Markdown must preserve second-highest scores');
}
for(const [file,locale] of [['index.html','en'],['zh/index.html','zh'],['en/index.html','en']]) {
  const cover=await readFile(resolve(out,file),'utf8');
  assert.ok(cover.includes('class="cover-page"'),`Missing cover at ${file}`);
  assert.ok(cover.includes(`data-locale="${locale}"`),'Incorrect cover locale');
  assert.equal((cover.match(/<h1\b/g)||[]).length,1,'Cover requires one main heading');
  assert.ok(cover.includes(`class="start-reading" href="/${locale}/overview/"`),'Missing direct documentation CTA');
  assert.ok(cover.includes(`href="/${locale==='zh'?'en':'zh'}/"`),'Missing cover language switch');
  assert.ok(cover.includes('src="/assets/point-cloud-fallback.svg"'),'Missing static cloud fallback');
  assert.ok(!cover.includes('src="/assets/site.js"'),'Cover must not load document-only controls');
}
for(const locale of ['zh','en']) {
  const html=await readFile(resolve(out,locale,'overview/index.html'),'utf8');
  assert.ok(html.includes(`class="brand" href="/${locale}/"`),'Documentation brand must return to cover');
}
assert.equal(search.length,pages.reduce((count,page)=>count+page.sections.reduce((n,section)=>n+1+toolsIn(section).length,0)*2,0));
const catalog=pages.flatMap(page=>page.sections.flatMap(toolsIn));
assert.equal(catalog.length,72,'Expected 72 Graph Engine business tools (including optional reporting)');
assert.equal(new Set(catalog.map(tool=>tool.name)).size,catalog.length,'Duplicate tool');
for(const name of diagramNames) for(const locale of ['zh','en']) for(const dark of [false,true]) {
  const svg=renderDiagram(name,locale,dark);
  assert.ok(svg.includes('<title') && svg.includes('<desc'),'Missing diagram accessible description');
  assert.equal(await readFile(resolve(out,`diagrams/${name}-${locale}${dark?'-dark':''}.svg`),'utf8'),svg,'Stale diagram');
}
for(const item of search) {
  const url=new URL(item.href,'https://docs.invalid');
  const html=await readFile(resolve(out,'.'+url.pathname,'index.html'),'utf8');
  assert.ok(html.includes(`id="${url.hash.slice(1)}"`));
}
for(const script of ['site.js','theme.js','entry.js','cover.js','particles.js','cloud-geometry.js']) execFileSync(process.execPath,['--check',resolve(root,'assets',script)]);
console.log(`Passed: ${pages.length * 2} documentation pages, 2 localized covers, ${catalog.length} tools, ${search.length} search entries, ${checkedLinks} local links, diagrams, copy targets, JavaScript syntax, and asset limits.`);
console.log(`Static artifact: ${files.length} files, ${(total/1024/1024).toFixed(2)} MiB. Documentation and authored examples only.`);
