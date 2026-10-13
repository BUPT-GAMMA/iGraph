import { localizeUserCase, type UserCase } from '../userCases';
import { useI18n } from '../i18n';
import { BrandLogo, Icon } from './Icon';
import { ExampleQuestionButton } from './ExampleQuestionButton';
import './UserCaseGallery.css';

export function UserCaseGallery({ cases, busy, onSelect }: { cases: UserCase[]; busy: boolean; onSelect: (id: string, question?: string) => void }) {
  const { t } = useI18n();
  if (!cases.length) return null;
  return <section className="user-case-gallery" aria-label={t('Example graphs', '示例图分析')}>
    <header className="user-case-gallery-heading">
      <span className="user-case-gallery-mark" aria-hidden="true"><BrandLogo /></span>
      <h2>{t('Start your analysis here', '从这里开始分析')}</h2>
      <p>{t('Explore an example graph. Start with a question.', '选择一张示例图，从一个问题开始探索。')}</p>
    </header>
    <div className="user-case-cards">{cases.map(source => {
      const item = localizeUserCase(source, t);
      const graphType = item.id === 'borylation' ? t('Molecular structure', '分子结构图')
        : item.id === 'tianlongbabu' ? t('Character relationships', '人物关系图')
        : item.id === 'finance_risk' ? t('Equity relationships', '股权关系图') : t('Example graph', '示例图');
      return <article key={item.id} className="user-case-card" aria-label={item.name}>
        <div className="user-case-card-meta">
          <span className="user-case-card-icon"><Icon name={item.id === 'finance_risk' ? 'schema' : item.id === 'borylation' ? 'cube' : 'graph'} size={19} /></span>
          <span>{graphType}</span>
        </div>
        <h3>{item.name}</h3>
        <p className="user-case-description">{item.description}</p>
        <div className="user-case-questions" role="group" aria-label={t('Questions for ', '示例问题：') + item.name}>
          {item.questions.map((question, index) => <ExampleQuestionButton key={question}
            title={item.question_titles?.[index] || question} question={question} disabled={busy || !item.available}
            onSelect={() => onSelect(item.id, question)} />)}
        </div>
        <button type="button" className="user-case-open" disabled={busy || !item.available} onClick={() => onSelect(item.id)}>
          {item.available ? t('Use example graph', '使用示例图') : t('Not installed', '尚未安装')}<Icon name="arrow-right" size={15} />
        </button>
      </article>;
    })}</div>
    <p className="user-case-gallery-hint">{t('Choose a topic to analyze · Hover or tap ? to see the full question', '点击主题开始分析 · 悬停或点击问号查看完整问题')}</p>
  </section>;
}
