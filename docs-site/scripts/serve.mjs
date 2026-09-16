import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { resolve, extname, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../dist/', import.meta.url));
const types = { '.html':'text/html; charset=utf-8', '.css':'text/css; charset=utf-8', '.js':'text/javascript; charset=utf-8', '.json':'application/json; charset=utf-8', '.svg':'image/svg+xml', '.md':'text/plain; charset=utf-8', '.txt':'text/plain; charset=utf-8', '.xml':'application/xml; charset=utf-8' };
createServer(async (req,res) => {
  try {
    const pathname = decodeURIComponent(new URL(req.url,'http://localhost').pathname);
    let file = resolve(root,'.'+pathname);
    if(file !== resolve(root) && !file.startsWith(resolve(root)+sep)) { res.writeHead(403).end(); return; }
    let info;
    try { info=await stat(file); } catch {}
    if(info?.isDirectory()) {
      if(!pathname.endsWith('/')) { res.writeHead(308,{Location:pathname+'/'}).end(); return; }
      file=resolve(file,'index.html');
    }
    let content;
    try { content=await readFile(file); } catch {
      res.statusCode=404;
      content=await readFile(resolve(root,pathname.startsWith('/zh/')?'zh/404.html':'404.html'));
      file='404.html';
    }
    res.setHeader('Content-Type',types[extname(file)] || 'application/octet-stream');
    res.setHeader('Cache-Control','no-store');
    res.end(req.method==='HEAD'?undefined:content);
  } catch { res.writeHead(400).end('Bad request'); }
}).listen(4173,'127.0.0.1',()=>console.log('iGraph Docs preview: http://127.0.0.1:4173'));
