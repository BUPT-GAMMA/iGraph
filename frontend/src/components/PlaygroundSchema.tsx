import type { PlaygroundSchema, PlaygroundState, PlaygroundTypeChange } from '../api';
import type { Project } from '../types';
import { useI18n } from '../i18n';
import { Icon } from './Icon';

export function PlaygroundResult({ result, onOpen }: { result: NonNullable<PlaygroundState['result']>; onOpen: (p: Project) => void }) {
  const { t } = useI18n();
  return <article className="playground-result">
    <div className="playground-result-head">
      <span className="playground-mark"><Icon name="folder-open" size={22} /></span><div>
        <span>{result.available === false ? t('The project or graph was deleted', '项目或示例图已删除') : t('Saved to My projects', '已保存到「我的项目」')}</span><h2>{result.project.name}</h2>
        <p>{t(`Synthetic graph · ${result.node_count} nodes · ${result.edge_count} edges`, `模拟示例图 · ${result.node_count} 个节点 · ${result.edge_count} 条边`)}</p></div>
      <button className="primary-command" disabled={result.available === false} onClick={() => onOpen(result.project)}>{t('Open for analysis', '打开项目分析')}<Icon name="arrow-right" size={15} /></button>
    </div>
    {result.available !== false && result.schema && <GraphTypes schema={result.schema} />}
    {result.schema_unavailable && <p role="status">{t('Could not refresh the saved graph types.', '暂时无法刷新已保存图的类型。')}</p>}
  </article>;
}

export function GraphTypes({ schema }: { schema: PlaygroundSchema }) {
  const { t, language } = useI18n();
  return <div className="playground-types">
    <section>
      <h3><Icon name="cube" size={15} />{t('Node types', '节点类型')}</h3>
      <dl>{schema.nodes.map(node => <div key={node.type}>
        <dt><strong>{node.type}</strong><small>{t(`${node.count} nodes`, `${node.count} 个节点`)}</small></dt>
        <dd>{node.description[language]}</dd>
        {!!node.examples.length && <dd className="type-examples">{t('Examples', '示例')} · {node.examples.join(language === 'zh' ? '、' : ', ')}</dd>}
      </div>)}</dl>
      {!schema.nodes.length && <p>{t('No node types', '暂无节点类型')}</p>}
    </section>
    <section>
      <h3><Icon name="schema" size={15} />{t('Edge types', '边类型')}</h3>
      <dl>{schema.edges.map(edge => <div key={JSON.stringify([edge.source, edge.type, edge.target])}>
        <dt><strong>{edge.type}</strong><small>{t(`${edge.count} edges`, `${edge.count} 条边`)}</small></dt>
        <dd className="type-direction">{edge.source} → {edge.target}</dd>
        <dd>{edge.description[language]}</dd>
      </div>)}</dl>
      {!schema.edges.length && <p>{t('No edge types', '暂无边类型')}</p>}
    </section>
  </div>;
}

export function TypePreview({ changes, schema, before }: { changes: PlaygroundTypeChange[]; schema: PlaygroundSchema; before?: Pick<PlaygroundSchema, 'node_count' | 'edge_count'> }) {
  const { t, language } = useI18n();
  const verbs = { add: t('Add', '新增'), update: t('Update', '修改'), remove: t('Remove', '移除'), reverse: t('Reverse', '反转') };
  return <div className="playground-type-preview">
    <ul>{changes.map((c, i) => <li key={i}>
      {verbs[c.action]} {c.kind === 'node' ? t('node type', '节点类型') : t('edge type', '边类型')} <strong>{c.name}</strong>
      {c.new_name && c.new_name !== c.name && <> → <strong>{c.new_name}</strong></>}
      {c.source && c.target && <> · {c.source} → {c.target}</>}
      {(c.new_source || c.new_target) && <> · {t('New endpoints', '修改后的起点和终点')} {c.new_source || c.source} → {c.new_target || c.target}</>}
      {c.description && <> · {c.description[language]}</>}
    </li>)}</ul>
    {before && (before.node_count !== schema.node_count || before.edge_count !== schema.edge_count) &&
      <p>{t('Nodes', '节点')} {before.node_count} → {schema.node_count} · {t('Edges', '边')} {before.edge_count} → {schema.edge_count}</p>}
    <details><summary>{t('Preview graph types', '预览修改后的类型')} · {t(`${schema.node_count} nodes, ${schema.edge_count} edges`, `${schema.node_count} 个节点，${schema.edge_count} 条边`)}</summary>
      <GraphTypes schema={schema} />
    </details>
  </div>;
}
