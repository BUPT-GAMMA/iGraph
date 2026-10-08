import { localizeUserCase, type UserCase } from '../userCases';
import { useI18n } from '../i18n';
export function UserCaseGallery({ cases, busy, onSelect }: { cases: UserCase[]; busy: boolean; onSelect: (id: string, question?: string) => void }) {
  const { t } = useI18n();
  cases = cases.map(item => localizeUserCase(item, t));
  return cases.length ? <section className="user-case-gallery" aria-label={t('Example graphs', '示例图分析')}><h2>{t('Try a ready-made graph', '使用预置图开始分析')}</h2><div className="user-case-cards">{cases.map(item => <article key={item.id}><h3>{item.name}</h3><p>{item.description}</p><button className="secondary-command" disabled={busy || !item.available} onClick={() => onSelect(item.id)}>{item.available ? t('Use example graph', '使用示例图') : t('Not installed', '尚未安装')}</button><div className="user-case-questions"><small>{t('Select a question to start analysis', '点击示例问题开始分析')}</small>{item.questions.map(question => <button key={question} disabled={busy || !item.available} onClick={() => onSelect(item.id, question)}>{question}</button>)}</div></article>)}</div></section> : null;
}
