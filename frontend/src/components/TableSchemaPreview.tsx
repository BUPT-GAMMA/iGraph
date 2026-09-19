import { useEffect, useRef, useState } from 'react';
import type { ConstructionSourceTable, GraphData } from '../types';
import { useI18n } from '../i18n';
import { GraphWorkspace } from './GraphWorkspace';
import { SourceTableFields } from './ConstructionSchemaFields';
import { Icon } from './Icon';
import './TableSchemaPreview.css';

export function TableSchemaPreview({ tables, graph }: { tables: ConstructionSourceTable[]; graph: GraphData }) {
  const { t } = useI18n();
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
  const table = tables.find(item => item.name === selected?.id);
  const inspector = useRef<HTMLElement>(null);
  useEffect(() => {
    if (table) inspector.current?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
  }, [table?.name]);
  return <div className="table-schema-preview">
    <div className="construction-graph"><GraphWorkspace data={graph} tab="Graph" onTab={() => undefined}
      selected={selected} onSelect={setSelected} collapsed={false} onToggle={() => undefined} embedded /></div>
    <aside ref={inspector} className="table-node-inspector" aria-live="polite" aria-label={t('Table node properties', '表节点属性')}>
      <h3><Icon name="table" size={17} />{t('Table node properties', '表节点属性')}</h3>
      {table ? <SourceTableFields table={table} /> : <div className="table-node-empty"><Icon name="table" size={28} /><p>{t('Select a table node to inspect fields and PK/FK.', '点击表节点查看字段与主外键')}</p></div>}
    </aside>
  </div>;
}
