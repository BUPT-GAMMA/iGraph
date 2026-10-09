import { useState } from 'react';
import { useI18n } from '../i18n';
import { helpCategories, helpModels, helpTasks, helpTools, helpToolTitle, matchesHelpQuery } from '../helpCatalog';
import { DrawerShell } from './DrawerShell';
import { Icon } from './Icon';
import { HelpPipeline } from './HelpPipeline';
import './HelpPage.css';

export function HelpPage({ onClose = () => undefined }: { onClose?: () => void }) {
  const { language, t } = useI18n();
  const [query, setQuery] = useState('');
  const [tab, setTab] = useState<'pipeline' | 'tools' | 'models'>('pipeline');
  const [category, setCategory] = useState('all');
  const tools = helpTools.filter(item => (category === 'all' || item.category === category) && matchesHelpQuery(query, item.name, helpToolTitle(item.name, 'zh'), item.description.en, item.description.zh, helpCategories[item.category].en, helpCategories[item.category].zh));
  const models = helpModels.filter(item => matchesHelpQuery(query, item.name, item.title, item.description.en, item.description.zh, ...item.tasks.flatMap(task => [helpTasks[task].en, helpTasks[task].zh])));
  const title = t('Help & capabilities', '帮助与能力清单');

  return <DrawerShell
    ariaLabel={title}
    className="help-drawer"
    onClose={onClose}
    resize={{
      defaultWidth: 760,
      minimumWidth: 360,
      maximumWidth: 1200,
      storageKey: 'igraph-layout-help-drawer-width',
      label: t('Resize help panel', '调整帮助栏宽度'),
    }}
  >{requestClose => <>
    <header className="drawer-head help-drawer-head">
      <h1 id="help-page-title">{title}</h1>
      <button type="button" className="plain-icon" onClick={requestClose} aria-label={t('Close help', '关闭帮助')}><Icon name="close" size={17} /></button>
    </header>
    <section className="help-page" aria-labelledby="help-page-title">
      <div className="help-page-inner">
      <div className="help-toolbar">
        <div className="help-tabs" role="tablist" aria-label={t('Help sections', '帮助分类')}>
          <button type="button" id="help-pipeline-tab" role="tab" aria-controls="help-pipeline-panel" aria-selected={tab === 'pipeline'} onClick={() => setTab('pipeline')}>{t('Usage pipeline', '使用流程')}</button>
          <button type="button" id="help-tools-tab" role="tab" aria-controls="help-tools-panel" aria-selected={tab === 'tools'} onClick={() => setTab('tools')}>{t('Graph analysis tools', '图分析计算工具')}<span>{helpTools.length}</span></button>
          <button type="button" id="help-models-tab" role="tab" aria-controls="help-models-panel" aria-selected={tab === 'models'} onClick={() => setTab('models')}>{t('Graph learning models', '图学习模型')}<span>{helpModels.length}</span></button>
        </div>
        {tab !== 'pipeline' && <label className="help-search"><Icon name="search" size={16} /><input type="search" aria-label={t('Search help', '搜索帮助')} placeholder={t('Search names, tasks or descriptions…', '搜索名称、任务或用途…')} value={query} onChange={event => setQuery(event.target.value)} /></label>}
      </div>
      <div role="tabpanel" id="help-pipeline-panel" aria-labelledby="help-pipeline-tab" hidden={tab !== 'pipeline'}><HelpPipeline /></div>
      {tab === 'tools' ? <div role="tabpanel" id="help-tools-panel" aria-labelledby="help-tools-tab">
        <div className="help-filter-row"><label htmlFor="help-category">{t('Category', '工具分类')}</label><select id="help-category" value={category} onChange={event => setCategory(event.target.value)}><option value="all">{t('All categories', '全部分类')}</option>{Object.entries(helpCategories).map(([id, label]) => <option value={id} key={id}>{label[language]}</option>)}</select><span role="status">{t(`${tools.length} tools`, `${tools.length} 项工具`)}</span></div>
        {tools.length ? <div className="help-tool-list">{tools.map(item => <article className="help-tool-card" key={item.name}><div className="help-card-heading"><h2>{helpToolTitle(item.name, language)}</h2><span>{helpCategories[item.category][language]}</span></div><code>{item.name}</code><p>{item.description[language]}</p></article>)}</div> : <p className="help-empty">{t('No matching tools. Try another keyword or category.', '没有匹配的工具，请更换关键词或分类。')}</p>}
      </div> : tab === 'models' ? <div role="tabpanel" id="help-models-panel" aria-labelledby="help-models-tab">
        <details className="help-requirements"><summary>{t('Training requirements', '训练要求')}</summary><p>{t('Training requires suitable features and labels; graph classification uses uploaded HIV/PCBA datasets.', '训练需要合适的特征和标签；图分类使用上传的 HIV/PCBA 数据集。')}</p></details>
        <div className="help-model-grid">{models.map(item => <article className="help-model-card" key={item.name}><h2>{item.name === 'node2vec' ? 'Node2Vec' : item.name}</h2><strong>{item.title}</strong><p>{item.description[language]}</p><div className="help-task-tags" aria-label={t('Supported tasks', '支持任务')}>{item.tasks.map(task => <span key={task}>{helpTasks[task][language]}</span>)}</div></article>)}</div>
        {!models.length && <p className="help-empty">{t('No matching models. Try another keyword.', '没有匹配的模型，请更换关键词。')}</p>}
      </div> : null}
      </div>
    </section>
  </>}</DrawerShell>;
}
