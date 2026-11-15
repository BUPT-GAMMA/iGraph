import { localizeUserCase, type UserCase } from '../userCases';
import { useI18n } from '../i18n';
import { Icon } from './Icon';
import { ExampleQuestionButton } from './ExampleQuestionButton';
import './UserCaseGallery.css';

export function UserCaseGallery({ cases, busy, onSelect, projectsView = false }: { cases: UserCase[]; busy: boolean; onSelect: (id: string, question?: string) => void; projectsView?: boolean }) {
  const { t } = useI18n();
  if (!cases.length) return null;
  return <section className="user-case-gallery" aria-label={t('Example graphs', '示例图分析')}>
    <header className="user-case-gallery-heading">
      <Icon name="graph" size={19} />
      <h2>{projectsView ? t('Example projects', '样例项目') : t('Start your analysis here', '从这里开始分析')}</h2>
    </header>
    <div className="user-case-cards">{cases.map(source => {
      const item = localizeUserCase(source, t);
      const graphType = item.id === 'borylation' ? t('Molecular structure', '分子结构图')
        : item.id === 'tianlongbabu' ? t('Character relationships', '人物关系图')
        : item.id === 'finance_risk' ? t('Equity relationships', '股权关系图') : t('Example graph', '示例图');
      return <article key={item.id} className="user-case-card" data-case={item.id} aria-label={item.name}>
        <div className="user-case-card-head">
          <span className="user-case-card-icon"><Icon name={item.id === 'finance_risk' ? 'schema' : item.id === 'borylation' ? 'cube' : 'graph'} size={19} /></span>
          <div><h3>{item.name}</h3><small>{graphType}</small></div>
        </div>
        {item.description && <p className="user-case-description">{item.description}</p>}
        <div className="user-case-questions" role="group" aria-label={t('Questions for ', '示例问题：') + item.name}>
          {item.questions.map((question, index) => <ExampleQuestionButton key={question}
            title={item.question_titles?.[index] || question} question={question} disabled={busy || !item.available}
            onSelect={() => onSelect(item.id, question)} />)}
        </div>
        <div className="user-case-card-footer">
          <button type="button" className="user-case-open" disabled={busy || !item.available} onClick={() => onSelect(item.id)}>
            {item.available ? t('Use example graph', '使用示例图') : t('Not installed', '尚未安装')}<Icon name="arrow-right" size={15} />
          </button>
        </div>
      </article>;
    })}</div>
  </section>;
}
