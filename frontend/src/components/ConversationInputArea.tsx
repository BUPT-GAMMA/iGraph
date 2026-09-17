import { useEffect, useRef, type KeyboardEvent } from 'react';
import type { BottomInteraction } from '../sessionState';
import { Composer, type ComposerProps } from './Composer';
import { Icon } from './Icon';
import { useI18n, type Translate } from '../i18n';

export type ConversationInputAreaProps = ComposerProps & {
  interaction: BottomInteraction;
  onFeedback?: (decision: 'approve' | 'reject') => void;
};

function savedWorkLabel(value: string, attachmentCount: number, uploading: boolean, t: Translate) {
  const items: string[] = [];
  if (value.trim()) items.push(t('Draft saved', '草稿已保存'));
  if (attachmentCount) items.push(t(`${attachmentCount} attachment${attachmentCount === 1 ? '' : 's'} saved`, `${attachmentCount} 个附件已保存`));
  if (uploading) items.push(t('Uploading attachments…', '附件上传中…'));
  return items.length ? items.join(' · ') : t('The input box will return after confirmation', '确认完成后将恢复输入框');
}

function blockEnter(event: KeyboardEvent<HTMLElement>) {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  event.stopPropagation();
}

export function ConversationInputArea({ interaction, onFeedback, ...composerProps }: ConversationInputAreaProps) {
  const { t } = useI18n();
  const headingRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (interaction.kind !== 'composer') headingRef.current?.focus();
  }, [interaction.kind, interaction.kind === 'approval' ? interaction.phase : 'syncing']);

  if (interaction.kind === 'composer') return <Composer {...composerProps} />;

  const savedLabel = savedWorkLabel(composerProps.value, composerProps.attachments.length, composerProps.uploading, t);
  if (interaction.kind === 'syncing_feedback') {
    const missingFeedback = interaction.reason === 'missing_feedback';
    return <div className="interaction-dock-wrap">
      <section className="confirmation-card confirmation-syncing" aria-labelledby="feedback-sync-heading" aria-busy="true">
        <div ref={headingRef} tabIndex={-1} className="confirmation-heading" id="feedback-sync-heading">
          <span className="confirmation-mark"><span className="loader" /></span>
          <div><strong>{missingFeedback ? t('Syncing confirmation details', '正在同步确认信息') : t('Loading conversation state', '正在加载会话状态')}</strong><span>{missingFeedback ? t('Input is locked while the pending confirmation is restored from the server', '输入区已安全锁定，正在从服务端恢复待确认操作') : t('Checking for active or pending tasks in this conversation', '正在确认当前会话是否存在运行中或待确认的任务')}</span></div>
        </div>
        <div className="confirmation-saved-state"><Icon name="file" size={13} /> {savedLabel}</div>
        {composerProps.canCancel && <button type="button"
          onClick={composerProps.onCancel}
          aria-label={t('Stop current task', '终止当前任务')}>
          {t('Stop current task', '终止当前任务')}
        </button>}
      </section>
    </div>;
  }

  const { feedback, phase } = interaction;
  const submitting = phase === 'submitting';
  const args = feedback.proposed_args && Object.keys(feedback.proposed_args).length
    ? JSON.stringify(feedback.proposed_args, null, 2)
    : '';

  return <div className="interaction-dock-wrap">
    <section className="confirmation-card" aria-labelledby="feedback-approval-heading" aria-busy={submitting} onKeyDownCapture={blockEnter}>
      <div ref={headingRef} tabIndex={-1} className="confirmation-heading" id="feedback-approval-heading">
        <span className="confirmation-mark"><Icon name="settings" size={15} /></span>
        <div><strong>{submitting ? t('Submitting confirmation', '正在提交确认') : t('Assistant requests confirmation', '助手请求确认')}</strong><span>{feedback.tool_name || t('Graph machine-learning tool', '图机器学习工具')}</span></div>
      </div>
      <p>{feedback.description || t('This operation may use significant compute resources. Confirm whether to continue.', '该操作可能消耗较多计算资源，请确认是否继续。')}</p>
      {args ? <details className="confirmation-arguments" open><summary>{t('View operation arguments', '查看操作参数')}</summary><pre>{args}</pre></details> : null}
      <div className="confirmation-saved-state"><Icon name="file" size={13} /> {savedLabel}</div>
      <div className="confirmation-actions">
        {composerProps.canCancel && <button type="button" onClick={composerProps.onCancel} aria-label={t('Stop current task', '终止当前任务')}><span className="stop-square" /> {t('Stop current task', '终止当前任务')}</button>}
        <button type="button" className="confirmation-reject" disabled={submitting} onClick={() => onFeedback?.('reject')}><Icon name="close" size={14} /> {submitting ? t('Submitting…', '提交中…') : t('Reject', '拒绝')}</button>
        <button type="button" className="confirmation-approve" disabled={submitting} onClick={() => onFeedback?.('approve')}><Icon name="check" size={14} /> {submitting ? t('Submitting confirmation…', '提交确认中…') : t('Approve and run', '批准并执行')}</button>
      </div>
    </section>
  </div>;
}
