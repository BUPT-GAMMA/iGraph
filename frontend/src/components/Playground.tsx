import { useEffect, useRef, useState } from 'react';
import { ApiError, getPlayground, listPlaygrounds, getPlaygroundConnection, modelErrorMessage, sendPlaygroundTurn,
  type PlaygroundConnection, type PlaygroundState } from '../api';
import { useI18n } from '../i18n';
import { randomUUID } from '../randomId';
import type { Project } from '../types';
import { PlaygroundConnectionForm } from './PlaygroundConnection';
import { Icon } from './Icon';
import './Playground.css';

type Props = {
  onConnectionSaved?: () => void;
  onPending: (id: string) => void;
  onReady: (project: Project) => void;
  onOpen: (project: Project) => void;
};

function fresh(): PlaygroundState {
  return { id: randomUUID(), revision: 0, status: 'draft', messages: [], live_reply: '', plan: null,
    confirmable: false, confirmed: false, result: null, error: null };
}

export function Playground({ onConnectionSaved, onPending, onReady, onOpen }: Props) {
  const { t, language } = useI18n();
  const [scene, setScene] = useState(fresh);
  const [history, setHistory] = useState<{ id: string; title: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [connection, setConnection] = useState<PlaygroundConnection | null>(null);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [connectionError, setConnectionError] = useState(false);
  const [connectionLoad, setConnectionLoad] = useState(0);
  const [sending, setSending] = useState(false);
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const epoch = useRef(0);
  const submitLock = useRef(false);
  const scroll = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const pending = scene.status === 'thinking' || scene.status === 'building';
  const busy = loading || sending || pending;
  const readyRef = useRef(onReady);
  readyRef.current = onReady;

  function remember(next: PlaygroundState) {
    setScene(next);
    if (next.messages.length) {
      const row = { id: next.id, title: next.plan?.title || next.messages[0].text.slice(0, 70) };
      setHistory(items => items[0]?.id === row.id && items[0].title === row.title
        ? items : [row, ...items.filter(item => item.id !== row.id)]);
    }
    if (next.status === 'thinking' || next.status === 'building') onPending(next.id);
    if (next.result && next.result.available !== false) readyRef.current(next.result.project);
  }

  useEffect(() => {
    let current = true;
    getPlaygroundConnection().then(value => {
      if (current) { setConnection(value); setConnectionError(false); }
    }).catch(() => { if (current) setConnectionError(true); });
    return () => { current = false; };
  }, [connectionLoad]);

  async function load(id?: string) {
    const key = ++epoch.current;
    setLoading(true);
    setError('');
    try {
      if (!id) {
        const response = await listPlaygrounds();
        if (key !== epoch.current) return;
        setHistory(response.scenarios);
        id = response.scenarios[0]?.id;
      }
      const next = id ? await getPlayground(id) : fresh();
      if (key === epoch.current) { remember(next); setText(''); }
    } catch {
      if (key === epoch.current) setError(t('Could not load this conversation.', '未能加载场景对话。'));
    } finally { if (key === epoch.current) setLoading(false); }
  }
  useEffect(() => {
    void load();
    return () => { epoch.current++; };
  }, []);

  useEffect(() => {
    if (!pending) return;
    const abort = new AbortController();
    const key = epoch.current;
    let timer: number;
    async function poll() {
      let delay = 600;
      try {
        const next = await getPlayground(scene.id, abort.signal);
        if (abort.signal.aborted || key !== epoch.current) return;
        remember(next);
        setError('');
        if (next.status !== 'thinking' && next.status !== 'building') return;
      } catch {
        if (abort.signal.aborted || key !== epoch.current) return;
        delay = 1800;
        setError(t('Reconnecting to the conversation…', '正在重新连接场景对话…'));
      }
      timer = window.setTimeout(poll, delay);
    }
    timer = window.setTimeout(poll, 250);
    return () => { abort.abort(); window.clearTimeout(timer); };
  }, [scene.id, pending]);

  useEffect(() => {
    const el = scroll.current;
    if (el) el.scrollTop = connectionOpen ? 0 : el.scrollHeight;
  }, [scene.id, scene.messages.length, scene.live_reply, scene.status, connectionOpen]);

  async function submit(message = text, confirm = false) {
    const value = message.trim();
    if (!connection?.configured || busy || submitLock.current || scene.result || !value) return;
    submitLock.current = true;
    setSending(true); setError('');
    const key = epoch.current;
    const turnId = randomUUID();
    try {
      const next = await sendPlaygroundTurn(scene.id, { turn_id: turnId, revision: scene.revision,
        message: value, language, confirm, connection_revision: connection.revision });
      if (key === epoch.current) { remember(next); setText(''); }
    } catch (cause) {
      if (key !== epoch.current) return;
      if (cause instanceof ApiError && ['MODEL_CONFIG_CHANGED', 'PLAYGROUND_NOT_CONFIGURED'].includes(cause.code || '')) {
        setConnection(null); setConnectionOpen(true); setConnectionLoad(n => n + 1);
      }
      // A lost acknowledgement can follow a successful submission.
      try {
        const latest = await getPlayground(scene.id);
        if (key !== epoch.current) return;
        remember(latest);
        if (latest.turn_id === turnId) { setText(''); return; }
      } catch { /* An unsent first turn has no server conversation yet. */ }
      if (key === epoch.current) setError(cause instanceof ApiError && cause.code === 'PLAYGROUND_CHANGED'
        ? t('The conversation changed. Review the latest reply and send again.', '对话已有更新，请查看最新回复后再发送。')
        : t('Could not send the message. Try again.', '消息发送失败，请重试。'));
    } finally {
      submitLock.current = false;
      if (key === epoch.current) setSending(false);
    }
  }

  function newScene() {
    epoch.current++; setScene(fresh()); setText(''); setError(''); setLoading(false); setSending(false);
    composer.current?.focus();
  }
  const confirmText = t('Confirm and build', '确认好问题了，开始构图');
  const issue = scene.error && (modelErrorMessage(scene.error) || ({
    PLAYGROUND_NOT_CONFIGURED: t('The Playground model service is not configured.', '场景广场的模型服务尚未配置。'),
    PLAYGROUND_INVALID_REPLY: t('The model returned an incomplete plan. Try again.', '模型返回的方案不完整，可以重试。'),
    PLAYGROUND_INTERRUPTED: t('The turn was interrupted. You can resume here.', '本轮对话已中断，可以在这里重试。'),
    PLAYGROUND_TIMEOUT: t('This turn timed out. Try again.', '本轮处理超时，可以重试。'),
  }[scene.error]) || (scene.confirmed ? t('Could not save the example graph. Retry to resume.', '示例图暂未保存，重试可继续构图。')
    : t('The model could not complete this turn. Try again.', '模型未能完成这轮对话，可以重试。')));
  const result = scene.result;
  return <section className="playground" aria-label={t('Playground', '场景广场')}>
    <header className="playground-head">
      <span className="playground-mark"><Icon name="compass" size={22} /></span>
      <h1>{t('Playground', '场景广场')}</h1>
      <div className="playground-history">
        {connection?.configured && <button type="button" className="secondary-command" disabled={busy} onClick={() => setConnectionOpen(open => !open)} aria-expanded={connectionOpen}><Icon name="settings" size={15} />{t('Model connection', '模型连接')}</button>}
        {!!history.length && <select aria-label={t('Scenario history', '场景历史')} value={history.some(row => row.id === scene.id) ? scene.id : ''}
          disabled={loading || sending} onChange={event => void load(event.target.value)}>
          <option value="" disabled>{t('Scenario history', '场景历史')}</option>
          {history.map(row => <option key={row.id} value={row.id}>{row.title}</option>)}
        </select>}
        <button type="button" className="secondary-command" onClick={newScene} disabled={loading || sending}>
          <Icon name="plus" size={15} />{t('New scenario', '新场景')}</button>
      </div>
    </header>
    <div ref={scroll} className="playground-scroll" role="log" aria-label={t('Scenario conversation', '场景对话')}>
      <div className="playground-thread">
        {connectionError && <div role="alert">{t('Could not load the model connection.', '模型连接配置加载失败。')} <button type="button" className="text-button" onClick={() => setConnectionLoad(n => n + 1)}>{t('Retry', '重试')}</button></div>}
        {connection && (!connection.configured || connectionOpen) && <PlaygroundConnectionForm key={connection.revision} connection={connection} onSaved={next => { onConnectionSaved?.(); setConnection(next); setConnectionOpen(false); setError(''); }} onCancel={() => setConnectionOpen(false)} />}
        {!scene.messages.length && !loading && connection?.configured && !connectionOpen && <div className="playground-welcome">
          <span className="playground-mark"><Icon name="compass" size={28} /></span>
          <h2>{t('What would you like to explore?', '你想探索什么场景？')}</h2>
          <div className="playground-starters">{[
            t('Find bottlenecks in a supply chain', '我想找出供应链中的关键瓶颈'),
            t('Explore collaborations between research teams', '我想分析科研团队之间的合作关系'),
          ].map(value => <button type="button" key={value} onClick={() => { setText(value); composer.current?.focus(); }}>{value}<Icon name="arrow-right" size={14} /></button>)}</div>
        </div>}
        {loading && <p role="status">{t('Loading conversation…', '正在加载对话…')}</p>}
        {scene.messages.map((m, i) => <article className={'playground-turn ' + m.role} key={i}>
          {m.role === 'assistant' && <strong className="playground-speaker">iGraph</strong>}
          <div className="playground-bubble">{m.text}</div>
          {m.questions && <ol className="playground-questions">{m.questions.map(q => <li key={q.zh}>{q[language]}</li>)}</ol>}
        </article>)}
        {pending && <div className="playground-turn assistant" role="status"><strong className="playground-speaker">iGraph</strong>
          <div className="playground-bubble">{scene.live_reply || (scene.status === 'building'
            ? t('Building the sample graph and saving your project…', '正在构建示例图并保存项目…')
            : t('Thinking about your scenario…', '正在梳理你的场景…'))}</div></div>}
        {result && <article className="playground-result">
          <span className="playground-mark"><Icon name="folder-open" size={22} /></span><div>
            <span>{result.available === false ? t('The project or graph was deleted', '项目或示例图已删除') : t('Saved to My projects', '已保存到「我的项目」')}</span><h2>{result.project.name}</h2>
            <p>{t(`Synthetic graph · ${result.node_count} nodes · ${result.edge_count} edges`, `模拟示例图 · ${result.node_count} 个节点 · ${result.edge_count} 条边`)}</p></div>
          <button className="primary-command" disabled={result.available === false} onClick={() => onOpen(result.project)}>{t('Open for analysis', '打开项目分析')}<Icon name="arrow-right" size={15} /></button>
        </article>}
      </div>
    </div>
    <div className="playground-bottom">
      {(issue || error) && <div className="playground-error" role="alert">{error || issue}
        {scene.status === 'failed' && connection?.configured && !busy && <button onClick={() => void submit(scene.confirmed ? confirmText : [...scene.messages].reverse().find(m => m.role === 'user')?.text, scene.confirmed)}>{t('Retry', '重试')}</button>}
        {error && !pending && <button onClick={() => void load(scene.revision ? scene.id : undefined)}>{t('Reload', '重新加载')}</button>}
      </div>}
      {scene.confirmable && connection?.configured && !busy && !scene.confirmed && <button type="button" className="playground-confirm" onClick={() => void submit(confirmText, true)}>
        <Icon name="check" size={15} />{confirmText}</button>}
      {!result && connection?.configured && <form className="playground-compose composer" onSubmit={event => { event.preventDefault(); void submit(); }}>
        <textarea ref={composer} rows={2} value={text} maxLength={4000} disabled={busy || scene.confirmed}
          aria-label={t('Message the scenario agent', '与场景助手对话')} placeholder={scene.revision ? t('Refine the questions or confirm the build…', '继续调整问题，或告诉我可以构图了…') : t('Describe your scenario…', '描述你的场景…')}
          onChange={event => setText(event.target.value)} onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); void submit(); }
          }} />
        <div className="playground-compose-actions">
          <span>{t('Shift + Enter for a new line', 'Shift + Enter 换行')}</span>
          <button className="playground-send" type="submit" aria-label={t('Send message', '发送消息')} disabled={busy || scene.confirmed || !text.trim()}><Icon name="send" size={17} /></button></div>
      </form>}
    </div>
  </section>;
}
