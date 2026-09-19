import { useI18n } from '../i18n';

type Props = {
  nodes: number;
  edges: number;
  totalNodes: number;
  totalEdges: number;
  kind: 'instances' | 'schema' | 'tables';
};

export function GraphCoverage({ nodes, edges, totalNodes, totalEdges, kind }: Props) {
  const { language, t } = useI18n();
  const locale = language === 'zh' ? 'zh-CN' : 'en-US';
  const percent = new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 1 });
  const metrics = [
    { key: 'nodes', label: kind === 'tables' ? t('Tables', '表') : kind === 'schema' ? t('Node types', '节点类型') : t('Nodes', '节点'), shown: nodes, total: totalNodes },
    { key: 'edges', label: kind === 'tables' ? t('Foreign keys', '外键') : kind === 'schema' ? t('Relationships', '关系') : t('Edges', '边'), shown: edges, total: totalEdges },
  ];

  return <div className="graph-coverage" role="status" aria-label={t('Displayed / total', '已显示 / 总数')}>
    {metrics.map(metric => {
      const total = Math.max(metric.shown, Number.isFinite(metric.total) ? metric.total : metric.shown);
      const ratio = total > 0 ? metric.shown / total : 0;
      const percentage = ratio > 0 && ratio < .001 ? `<${percent.format(.001)}` : percent.format(ratio);
      const shownText = metric.shown.toLocaleString(locale), totalText = total.toLocaleString(locale);
      return <div className={`graph-coverage-metric graph-coverage-${metric.key}`} key={metric.key}>
        <div className="graph-coverage-heading"><span>{metric.label}</span><span className="graph-coverage-percent">{percentage}</span></div>
        <div className="graph-coverage-count"><strong>{shownText}</strong><span>/ {totalText}</span></div>
        <div className="graph-coverage-track" role="meter" aria-label={metric.label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={ratio * 100}
          aria-valuetext={t(`${shownText} of ${totalText} displayed (${percentage})`, `已显示 ${shownText} / ${totalText}（${percentage}）`)}>
          <span style={{ width: `${ratio * 100}%` }} />
        </div>
      </div>;
    })}
  </div>;
}
