import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

export async function loadPages(root) {
  const read = async file => JSON.parse(await readFile(join(root, file), 'utf8'));
  const [pages, example, tools, workspace, experiments] = await Promise.all([
    read('content.json'), read('construction-example.content.json'), read('graph-tools.content.json'), read('workspace.content.json'), read('experiments.content.json'),
  ]);
  for (const [after, page] of [['overview', experiments], ['quickstart', workspace], ['construction', example], ['graph-algorithms', tools]]) {
    const index = pages.findIndex(item => item.slug === after);
    if (index < 0) throw new Error('Missing parent page: ' + after);
    pages.splice(index + 1, 0, page);
  }
  return pages;
}

export const toolsIn = section => section.blocks.filter(block => block.type === 'tools').flatMap(block => block.items);
