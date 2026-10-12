import { useEffect, useState, type ReactNode } from 'react';
import { getBorylationCase, type BorylationCaseResponse } from '../api';
import { useI18n, type Translate } from '../i18n';
import { localizeUserCase, type UserCase } from '../userCases';
import './BorylationStoryPanel.css';

type Props = {
  userCase: UserCase;
  sessionId?: string;
  activeQuestion?: string;
  onQuestion: (question: string) => void;
  questionDisabled?: boolean;
};

export function BorylationStoryPanel({ userCase, sessionId, activeQuestion, onQuestion, questionDisabled = false }: Props) {
  const { t } = useI18n();
  const localizedCase = localizeUserCase(userCase, t);
  const steps = localizedCase.story_steps || [];
  const [expanded, setExpanded] = useState(!sessionId);
  const [selectedId, setSelectedId] = useState(steps[0]?.id || '');
  const [response, setResponse] = useState<BorylationCaseResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [imageFailed, setImageFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const selected = steps.find(step => step.id === selectedId) || steps[0];
  const candidateImage = selected?.analysis_step === 'structure' || selected?.analysis_step === 'symmetry';
  const neutralImage = selected?.analysis_step === 'properties' || selected?.analysis_step === 'rings';
  const imageUrl = neutralImage ? '/examples/borylation/BO-01-molecule.svg' : candidateImage ? '/examples/borylation/BO-01.png' : '/examples/borylation/BO-01-observed.png';

  useEffect(() => {
    if (!activeQuestion) return;
    const normalize = (value: string) => value.normalize('NFKC').trim().replace(/\s+/g, ' ').toLocaleLowerCase();
    const prompt = normalize(activeQuestion);
    const step = userCase.story_steps?.find(item => normalize(item.question) === prompt
      || userCase.translations?.en?.story_steps?.some(en => en.id === item.id && normalize(en.question) === prompt));
    if (step) setSelectedId(step.id);
  }, [activeQuestion, userCase]);

  useEffect(() => { setImageFailed(false); }, [imageUrl]);

  useEffect(() => {
    setResponse(null); setError('');
    if (!sessionId || !selected || !expanded) { setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true);
    void getBorylationCase(sessionId, selected.analysis_step, controller.signal)
      .then(result => {
        if (controller.signal.aborted) return;
        if (!result.success || result.step !== selected.analysis_step || !result.result || typeof result.result !== 'object') throw new Error(result.error || result.message || t('Unable to load the experimental records.', '无法读取实验记录。'));
        setResponse(result);
      })
      .catch(cause => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : t('Unable to load the experimental records.', '无法读取实验记录。'));
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [sessionId, selected?.analysis_step, expanded, reload, t]);

  return <section className="borylation-story" aria-label={t('Borylation case walkthrough', '分子硼化案例分析')}>
    <button type="button" className="borylation-story-heading" aria-expanded={expanded} aria-controls="borylation-story-body" onClick={() => setExpanded(value => !value)}>
      <span><strong>{localizedCase.name}</strong><small>{t('Four questions: properties, rings, sites, and symmetry', '四个问题：性质、环结构、位点与对称性')}</small></span>
      <span className="borylation-story-toggle">{expanded ? t('Collapse', '收起') : t('Expand', '展开')}</span>
    </button>
    {expanded && <div className="borylation-story-body" id="borylation-story-body">
      <nav className="borylation-story-steps" aria-label={t('Case questions', '案例问题')}>
        {steps.map((step, index) => <button type="button" key={step.id} className={selected?.id === step.id ? 'is-selected' : ''} aria-pressed={selected?.id === step.id} disabled={questionDisabled}
          onClick={() => { setSelectedId(step.id); onQuestion(step.question); }}><span>{index + 1}</span>{step.title}</button>)}
      </nav>
      {selected && <p className="borylation-story-question">{selected.question}<small>{t('Choose a question and send it, or ask your own.', '选择问题后发送，也可以自由提问。')}</small></p>}
      <div className="borylation-story-evidence">
        <figure className="borylation-molecule">
          {!imageFailed ? <a href={imageUrl} target="_blank" rel="noopener noreferrer" aria-label={t('View the full molecular structure image', '查看分子结构原图')}><img src={imageUrl} alt={neutralImage ? t('BO-01 numbered molecular structure', 'BO-01 带原子编号的分子结构') : candidateImage ? t('BO-01 molecular structure with candidate aromatic C–H sites', 'BO-01 分子结构与候选芳香 C–H 位点') : t('BO-01 molecular structure, with experimentally reported sites marked in green', 'BO-01 分子结构，绿色标出论文已报道的反应位点')} onError={() => setImageFailed(true)} /></a> : <p role="status">{t('The molecular structure image is unavailable.', '分子结构图暂时无法显示。')}</p>}
          <figcaption>{neutralImage ? t('Numbers identify atoms in the molecular graph', '编号对应分子图中的原子') : candidateImage ? t('Blue circles: candidate aromatic C–H sites', '蓝色圆点：候选芳香 C–H 位点') : <><span className="borylation-site-dot" />{t('Green circles: experimentally reported sites', '绿色圆点：论文已报道位点')}</>}<small>{t('Click to enlarge', '点击查看大图')}</small></figcaption>
        </figure>
        <div className="borylation-step-data" aria-live="polite">
          {!sessionId ? <p>{t('Send the first question to start the analysis.', '发送第一个问题开始分析。')}</p> : loading ? <p role="status"><span className="loader" /> {t('Loading experimental records…', '正在读取实验记录…')}</p> : error ? <div role="alert"><p>{error}</p><button type="button" className="secondary-command" onClick={() => setReload(value => value + 1)}>{t('Retry', '重试')}</button></div> : response ? <BorylationEvidence response={response} /> : null}
        </div>
      </div>
    </div>}
  </section>;
}

const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const rows = (value: unknown) => Array.isArray(value) ? value.map(record) : [];
const numbers = (value: unknown) => Array.isArray(value) ? value.filter(item => typeof item === 'number' && Number.isFinite(item)).join(', ') : '—';
const textValue = (value: unknown) => typeof value === 'string' ? value : '—';
const numberValue = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? String(value) : '—';
const percentage = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(2)}%` : '—';
const sourceRecord = (value: Record<string, unknown>) => `${textValue(value.source_file).split(/[\\/]/).pop()}${typeof value.source_line === 'number' ? `:${value.source_line}` : ''}`;
function sourceUrl(value: unknown) {
  if (typeof value !== 'string') return undefined;
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined; } catch { return undefined; }
}

function DataTable({ headers, data }: { headers: string[]; data: ReactNode[][] }) {
  const { t } = useI18n();
  return data.length ? <div className="borylation-table-wrap"><table className="borylation-data-table"><thead><tr>{headers.map(header => <th key={header} scope="col">{header}</th>)}</tr></thead><tbody>{data.map((row, index) => <tr key={index}>{row.map((cell, column) => <td key={column}>{cell}</td>)}</tr>)}</tbody></table></div> : <p>{t('No records were returned for this view.', '本次查询未返回记录。')}</p>;
}

function BorylationEvidence({ response }: { response: BorylationCaseResponse }) {
  const { t } = useI18n();
  const data = response.result;
  const provenance = response.provenance || {};
  const paperUrl = sourceUrl(provenance.paper_url), dataUrl = sourceUrl(provenance.data_url);
  let contents: ReactNode;
  if (response.step === 'properties') {
    const d = record(data.descriptors), method = record(data.method);
    contents = <><h4>{t('Calculated structural descriptors', '结构计算描述符')}</h4>
      <DataTable headers={[t('Descriptor', '描述符'), t('Value', '数值')]} data={[
        [t('Molecular formula', '分子式'), textValue(d.formula)],
        [t('Molecular weight (g/mol)', '分子量（g/mol）'), numberValue(d.molecular_weight_g_mol)],
        ['cLogP', numberValue(d.clogp)], ['TPSA (Å²)', numberValue(d.tpsa_angstrom2)],
        [t('H-bond donors / acceptors', '氢键供体 / 受体'), `${numberValue(d.h_bond_donors)} / ${numberValue(d.h_bond_acceptors)}`],
      ]} />
      <p className="borylation-data-note">RDKit {textValue(method.version)} · {t('TPSA includes S/P. cLogP is not a solubility measurement.', 'TPSA 包含 S/P 贡献。cLogP 不是溶解度测量值。')}</p>
      <h4>{t('Evidence available in this dataset', '本数据集的证据范围')}</h4>
      <p>{t('Solubility: no measurements or model predictions provided. Reaction solvents do not prove complete dissolution.', '溶解性：无实测或模型预测。反应中使用过某种溶剂，不代表已证实完全溶解。')}</p>
      <p>{t('Toxicity: unknown. No toxicity measurements or model predictions are provided.', '毒性：未知，未提供毒性测定或模型预测。')}</p>
      <p>{t('Recorded reaction solvents', '记录中的反应溶剂')}：{rows(record(data.solubility).recorded_reaction_solvents).map(s => solventName(s.name, t)).join(', ')}</p></>;
  } else if (response.step === 'rings') {
    contents = <><dl className="borylation-facts"><div><dt>{t('Rings', '环数量')}</dt><dd>{numberValue(data.ring_count)}</dd></div><div><dt>{t('Ring types', '环类型')}</dt><dd>{numberValue(data.ring_type_count)}</dd></div></dl>
      <DataTable headers={[t('Ring', '环'), t('Atoms', '原子编号'), t('Composition', '元素组成'), t('Aromaticity', '芳香性')]} data={rows(data.rings).map(r => [numberValue(r.ring_id), numbers(r.display_numbers), Object.entries(record(r.elements)).map(([element, count]) => `${element}${numberValue(count)}`).join(' '), r.aromatic === true ? t('Aromatic', '芳香') : t('Non-aromatic', '非芳香')])} />
      <p className="borylation-data-note">{t('Types group rings by size, elements, and aromaticity. Substituent environments may differ.', '按大小、元素组成与芳香性分类；同类型环的取代环境可以不同。')}</p></>;
  } else if (response.step === 'structure') {
    contents = <><dl className="borylation-facts"><div><dt>{t('Heavy atoms', '重原子')}</dt><dd>{numberValue(data.heavy_atom_count)}</dd></div><div><dt>{t('Chemical bonds', '化学键')}</dt><dd>{numberValue(data.chemical_bond_count)}</dd></div><div><dt>{t('Aromatic C–H sites', '芳香 C–H 位点')}</dt><dd>{numberValue(data.aromatic_ch_count)}</dd></div></dl>
      <p>{t('Candidate site numbers', '候选位点编号')}：{numbers(rows(data.candidate_atoms).map(atom => atom.display_number))}</p></>;
  } else if (response.step === 'symmetry') {
    contents = <><p>{t('Candidate positions', '候选位置')} {numberValue(data.candidate_atom_count)} → {t('Distinct site classes', '不等价位点类别')} {numberValue(data.distinct_site_count)}</p>
      <DataTable headers={[t('Site class', '位点类别'), t('Equivalent positions', '等价位置')]} data={rows(data.site_classes).map((row, index) => [String(index + 1), numbers(row.display_numbers)])} /></>;
  } else if (response.step === 'products') {
    contents = <><p>{t('Reported products', '已报道产物')}：{numberValue(data.product_count)} · {t('Reported site numbers', '已报道位点编号')}：{numbers(data.observed_display_numbers)}</p>
      <DataTable headers={[t('Product', '产物'), t('Structure', '结构'), t('Site', '位点'), t('Source record', '来源记录')]} data={rows(data.products).map(row => {
        const productId = textValue(row.product_id), match = /^BO-01:P([1-3])$/.exec(productId);
        const image = match ? `/examples/borylation/BO-01-product-${match[1]}.png` : null;
        return [productId, image ? <a href={image} target="_blank" rel="noopener noreferrer" title={t('View the full product structure', '查看产物结构原图')}><img className="borylation-product-image" src={image} alt={t(`${productId} product structure`, `${productId} 产物结构`)} /></a> : '—', numbers(row.display_numbers), sourceRecord(row)];
      })} />
      <p className="borylation-data-note">{t('Screening conditions for individual products: not provided.', '产物对应的实验条件：未提供。')}</p></>;
  } else if (response.step === 'conditions') {
    const contrast = record(data.recorded_contrast);
    contents = <><dl className="borylation-facts"><div><dt>{t('Experiments', '实验记录')}</dt><dd>{numberValue(data.record_count)}</dd></div><div><dt>{t('Above the success threshold', '达到成功阈值')}</dt><dd>{numberValue(data.success_count)}</dd></div><div><dt>{t('Below the threshold', '低于阈值')}</dt><dd>{numberValue(data.negative_count)}</dd></div></dl>
      {typeof data.success_definition === 'string' && <p className="borylation-threshold-note">{t('Success threshold: monoborylation conversion ≥ 5%.', '成功阈值：单硼化转化率 ≥ 5%。')}</p>}
      {typeof data.nonzero_count === 'number' && typeof data.subthreshold_nonzero_count === 'number' && <p className="borylation-threshold-note">{t('Records with nonzero conversion', '有非零转化的记录')}：{numberValue(data.nonzero_count)} · {t('Nonzero but below the threshold', '非零但未达阈值')}：{numberValue(data.subthreshold_nonzero_count)}</p>}
      <h4>{t('Three highest recorded conversions', '转化率最高的三条记录')}</h4>
      <ConditionTable records={rows(data.top_conditions)} />
      <details className="borylation-data-details"><summary>{t('Ligand × solvent screen', '展开配体 × 溶剂筛选网格')}（{numberValue(data.ligand_count)} × {numberValue(data.solvent_count)}）</summary><ConditionMatrix data={data} /></details>
      {Object.keys(contrast).length ? <details className="borylation-data-details"><summary>{t('Recorded solvent comparison', '查看同配体的溶剂对照')}</summary><ConditionTable records={[record(contrast.higher_record), record(contrast.comparison_record)]} /><p>{t('Two historical records for the same substrate and ligand; repeat experiments to confirm the difference.', textValue(contrast.interpretation))}</p></details> : null}
      </>;
  } else {
    contents = <><p>{t('Follow-up plan', '复核方案')}：{numberValue(data.condition_group_count)} {t('condition groups', '组条件')}，{t('total', '共')} {numberValue(data.recommended_total_runs)} {t('runs', '次实验')}。</p>
      <DataTable headers={[t('Experiment', '实验'), t('Ligand / solvent', '配体 / 溶剂'), t('Replicates', '重复次数')]} data={rows(data.experiments).map(row => {
        const reference = record(row.reference);
        return [<>{textValue(row.experiment_id)}<small>{textValue(row.purpose) === '同配体溶剂对照' ? t('Solvent control with the same ligand', textValue(row.purpose)) : t('Repeat a previously higher-conversion condition', textValue(row.purpose))}</small></>, <>{textValue(reference.ligand_name)} / {solventName(reference.solvent_name, t)}<small>{t('Historical conversion', '历史转化率')}：{percentage(reference.conversion_percent)}</small></>, numberValue(row.replicates)];
      })} />
      <p>{t('Sites to identify', '待鉴定位点')}：{numbers(data.positions_to_identify)}</p><p className="borylation-data-note">{t('Measure conversion and identify product sites and regioisomer ratios.', '测定转化率，并确认产物位点和异构体比例。')}</p></>;
  }
  return <>
    {contents}
    {paperUrl || dataUrl ? <p className="borylation-sources">{t('Sources', '来源')}：{paperUrl && <a href={paperUrl} target="_blank" rel="noopener noreferrer">{t('Research paper', '论文')}</a>}{paperUrl && dataUrl ? ' · ' : null}{dataUrl && <a href={dataUrl} target="_blank" rel="noopener noreferrer">{t('Public data', '公开数据')}</a>}{typeof provenance.license === 'string' ? ` · ${provenance.license}` : ''}</p> : null}
  </>;
}

const solventNames: Record<string, string> = { '环己烷': 'Cyclohexane', '正庚烷': 'n-Heptane', '甲基环己烷': 'Methylcyclohexane', '四氢呋喃': 'THF', '2-甲基四氢呋喃': '2-Methyltetrahydrofuran', '乙腈': 'Acetonitrile', '二氧六环': 'Dioxane', '1,4-二氧六环': '1,4-Dioxane' };
function solventName(value: unknown, t: Translate) { const text = textValue(value); return t(solventNames[text] || text, text); }

function ConditionTable({ records }: { records: Record<string, unknown>[] }) {
  const { t } = useI18n();
  return <DataTable headers={[t('Ligand', '配体'), t('Solvent', '溶剂'), t('Monoborylation conversion', '单硼化转化率'), t('Record', '记录')]} data={records.map(row => [textValue(row.ligand_name), solventName(row.solvent_name, t), percentage(row.conversion_percent), <span title={sourceRecord(row)}>{textValue(row.rxn_id)}</span>])} />;
}

function ConditionMatrix({ data }: { data: Record<string, unknown> }) {
  const { t } = useI18n();
  const ligands = rows(data.ligands), solvents = rows(data.solvents), cells = rows(data.matrix);
  const index = new Map(cells.map(cell => [`${cell.ligand_id}:${cell.solvent_id}`, cell]));
  const maximum = typeof data.conversion_max_percent === 'number' && Number.isFinite(data.conversion_max_percent) ? data.conversion_max_percent : 0;
  return <div className="borylation-table-wrap"><table className="borylation-data-table borylation-matrix"><caption>{t('Monoborylation conversion (%) · darker green indicates a higher recorded conversion', '单硼化转化率（%）· 绿色越深，记录转化率越高')}</caption><thead><tr><th scope="col">{t('Ligand / solvent', '配体 / 溶剂')}</th>{solvents.map(solvent => <th scope="col" key={textValue(solvent.id)}>{solventName(solvent.name, t)}</th>)}</tr></thead><tbody>{ligands.map(ligand => <tr key={textValue(ligand.id)}><th scope="row">{textValue(ligand.name)}</th>{solvents.map(solvent => {
    const cell = index.get(`${ligand.id}:${solvent.id}`);
    const value = cell?.conversion_percent;
    const score = typeof value === 'number' && maximum > 0 ? value / maximum : 0;
    return <td key={textValue(solvent.id)} style={cell ? { background: `rgba(34, 160, 107, ${.06 + .44 * score})` } : undefined} title={cell ? `${textValue(cell.rxn_id)} · ${sourceRecord(cell)}` : t('No record', '无记录')}>{typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '—'}</td>;
  })}</tr>)}</tbody></table></div>;
}
