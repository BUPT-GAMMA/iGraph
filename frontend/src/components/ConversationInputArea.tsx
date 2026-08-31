import { useEffect, useRef, type KeyboardEvent } from 'react';
import type { BottomInteraction } from '../sessionState';
import { Composer, type ComposerProps } from './Composer';
import { Icon } from './Icon';

export type ConversationInputAreaProps = ComposerProps & {
  interaction: BottomInteraction;
  onFeedback?: (decision: 'approve' | 'reject') => void;
};

function savedWorkLabel(value: string, attachmentCount: number, uploading: boolean) {
  const items: string[] = [];
  if (value.trim()) items.push('草稿已保存');
  if (attachmentCount) items.push(`${attachmentCount} 个附件已保存`);
  if (uploading) items.push('附件上传中…');
  return items.length ? items.join(' · ') : '确认完成后将恢复输入框';
}

function blockEnter(event: KeyboardEvent<HTMLElement>) {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  event.stopPropagation();
}

export function ConversationInputArea({ interaction, onFeedback, ...composerProps }: ConversationInputAreaProps) {
  const headingRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (interaction.kind !== 'composer') headingRef.current?.focus();
  }, [interaction.kind, interaction.kind === 'approval' ? interaction.phase : 'syncing']);

  if (interaction.kind === 'composer') return <Composer {...composerProps} />;

  const savedLabel = savedWorkLabel(composerProps.value, composerProps.attachments.length, composerProps.uploading);
  if (interaction.kind === 'syncing_feedback') {
    const missingFeedback = interaction.reason === 'missing_feedback';
    return <div className="interaction-dock-wrap">
      <section className="confirmation-card confirmation-syncing" aria-labelledby="feedback-sync-heading" aria-busy="true">
        <div ref={headingRef} tabIndex={-1} className="confirmation-heading" id="feedback-sync-heading">
          <span className="confirmation-mark"><span className="loader" /></span>
          <div><strong>{missingFeedback ? '正在同步确认信息' : '正在加载会话状态'}</strong><span>{missingFeedback ? '输入区已安全锁定，正在从服务端恢复待确认操作' : '正在确认当前会话是否存在运行中或待确认的任务'}</span></div>
        </div>
        <div className="confirmation-saved-state"><Icon name="file" size={13} /> {savedLabel}</div>
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
        <div><strong>{submitting ? '正在提交确认' : 'Agent 请求确认'}</strong><span>{feedback.tool_name || '图机器学习工具'}</span></div>
      </div>
      <p>{feedback.description || '该操作可能消耗较多计算资源，请确认是否继续。'}</p>
      {args ? <details className="confirmation-arguments" open><summary>查看操作参数</summary><pre>{args}</pre></details> : null}
      <div className="confirmation-saved-state"><Icon name="file" size={13} /> {savedLabel}</div>
      <div className="confirmation-actions">
        <button type="button" className="confirmation-reject" disabled={submitting} onClick={() => onFeedback?.('reject')}><Icon name="close" size={14} /> {submitting ? '提交中…' : '拒绝'}</button>
        <button type="button" className="confirmation-approve" disabled={submitting} onClick={() => onFeedback?.('approve')}><Icon name="check" size={14} /> {submitting ? '提交确认中…' : '批准并执行'}</button>
      </div>
    </section>
  </div>;
}
