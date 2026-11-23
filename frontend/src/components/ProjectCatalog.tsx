import type { Project } from '../types';
import { useExampleLabels, type UserCase } from '../userCases';
import { useI18n } from '../i18n';
import { Icon } from './Icon';
import { UserCaseGallery } from './UserCaseGallery';
import './WorkspaceCatalog.css';

type Props = {
  projects: Project[];
  cases: UserCase[];
  busy: boolean;
  onCreate: () => void;
  onOpen: (id: string) => void;
  onDatasets: (id: string) => void;
  onConstruct: (id: string) => void;
  onEdit: (project: Project) => void;
  onDelete: (project: Project) => void;
  onExample: (id: string, question?: string) => void;
};

export function ProjectCatalog({ projects, cases, busy, onCreate, onOpen, onDatasets, onConstruct, onEdit, onDelete, onExample }: Props) {
  const { language, t } = useI18n();
  const labels = useExampleLabels();
  const number = new Intl.NumberFormat(language);
  const date = new Intl.DateTimeFormat(language, { dateStyle: 'medium' });

  return <div className="workspace-catalog">
    <header className="catalog-page-head">
      <span className="context-icon" aria-hidden="true"><Icon name="folder" size={23} /></span>
      <h1>{t('My projects', '我的项目')}</h1>
      <button type="button" className="primary-command" disabled={busy} onClick={onCreate}>
        <Icon name="plus" size={16} />{t('New project', '新建项目')}
      </button>
    </header>
    <div className="catalog-page-content">
      {projects.length ? <div className="catalog-card-grid">
        {projects.map(project => {
          const datasetCount = project.dataset_count ?? Number(Boolean(project.default_dataset_id));
          const hasData = datasetCount > 0;
          const description = project.description ? labels.description(project.description) : '';
          const value = project.updated_at || project.created_at;
          const modified = typeof value === 'number' ? new Date(value * 1000) : value ? new Date(value) : null;
          return <article className="catalog-card" key={project.project_id} aria-label={project.name}>
            <div className="catalog-card-head">
              <span className="catalog-card-icon"><Icon name="folder" size={20} /></span>
              <h2 title={project.name}>{project.name}</h2>
            </div>
            <p className="catalog-card-description" title={description}>{description || t('No project description', '暂无项目描述')}</p>
            <div className="catalog-card-meta">
              <span>{t(`${number.format(datasetCount)} ${datasetCount === 1 ? 'dataset' : 'datasets'}`, `${number.format(datasetCount)} 个数据集`)}
                {modified && Number.isFinite(modified.getTime()) && <> · <time dateTime={modified.toISOString()}>{date.format(modified)}</time></>}
              </span>
              <span className="catalog-status" data-tone={hasData ? 'ready' : 'neutral'}>{hasData ? t('Data added', '已有数据源') : t('Awaiting data', '待添加数据源')}</span>
            </div>
            <div className="catalog-card-actions">
              {hasData && <button type="button" className="primary-command" disabled={busy} onClick={() => onOpen(project.project_id)}>
                <Icon name="arrow-right" size={15} />{t('Start analysis', '开始分析')}
              </button>}
              <button type="button" className={hasData ? 'secondary-command' : 'primary-command'} disabled={busy} onClick={() => onConstruct(project.project_id)}>
                <Icon name="graph-create" size={15} />{t('Create your graph', '创建你的图')}
              </button>
              <button type="button" className="secondary-command" disabled={busy} onClick={() => onDatasets(project.project_id)}>
                <Icon name="database" size={15} />{hasData ? t('Project datasets', '项目数据集') : t('Select data source', '选择数据源')}
              </button>
              <details className="catalog-card-menu">
                <summary aria-label={t(`Project actions for ${project.name}`, `${project.name} 的项目操作`)} title={t('Project actions', '项目操作')}>
                  <Icon name="more" size={19} />
                </summary>
                <div>
                  <button type="button" disabled={busy} onClick={event => { const menu = event.currentTarget.closest('details'); if (menu) menu.open = false; onEdit(project); }}>
                    <Icon name="edit" size={14} />{t('Edit project', '编辑项目')}
                  </button>
                  <button type="button" className="danger" disabled={busy} onClick={event => { const menu = event.currentTarget.closest('details'); if (menu) menu.open = false; onDelete(project); }}>
                    <Icon name="trash" size={14} />{t('Delete project', '删除项目')}
                  </button>
                </div>
              </details>
            </div>
          </article>;
        })}
      </div> : <div className="catalog-empty">
        <span className="catalog-card-icon"><Icon name="folder" size={22} /></span>
        <strong>{t('No projects yet', '暂无项目')}</strong>
        <button type="button" className="secondary-command" onClick={onCreate} disabled={busy}>{t('Create your first project', '创建第一个项目')}</button>
      </div>}
      <UserCaseGallery cases={cases} busy={busy} onSelect={onExample} projectsView />
    </div>
  </div>;
}
