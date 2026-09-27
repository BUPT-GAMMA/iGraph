import { useEffect, useRef, useState } from 'react';
import type { RemoteConnection, RemoteConnectionWrite } from '../types';
import { deleteRemoteGraph, saveRemoteGraph, testRemoteGraph, USER_KEY } from '../api';
import { remoteGraphError } from '../remoteGraph';
import { useI18n } from '../i18n';
import { DrawerShell } from './DrawerShell';
import './RemoteGraphConnections.css';

type Props = {
  connections: RemoteConnection[]; selectedId: string | null;
  onSelect: (id: string | null) => void | Promise<void>;
  onClose: () => void; onChanged?: () => void | Promise<void>; busy?: boolean;
  unavailableReason?: string | null; loadError?: string;
};
const blank = (): RemoteConnectionWrite => ({ name: '', uri: '', database: 'neo4j', username: '', password: '' });
const editable = (c: RemoteConnection): RemoteConnectionWrite => ({ connection_id: c.connection_id, revision: c.revision, name: c.name, uri: c.uri, database: c.database, username: c.username, password: '' });
export function RemoteGraphConnections({ connections, selectedId, onSelect, onClose, onChanged, busy, unavailableReason, loadError }: Props) {
  const { t } = useI18n();
  const [form, setForm] = useState<RemoteConnectionWrite | null>(null);
  const [pending, setPending] = useState<'test' | 'save' | 'use' | 'delete' | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saved, setSaved] = useState<RemoteConnection | null>(null);
  const content = useRef<HTMLDivElement>(null);
  const editing = form !== null;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    return () => { if (previous?.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    content.current?.querySelector<HTMLElement>(editing ? 'input' : 'button')?.focus();
  }, [editing]);
  const alive = useRef(true);
  const owner = useRef(localStorage.getItem(USER_KEY));
  const inFlight = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const current = () => alive.current && owner.current === localStorage.getItem(USER_KEY);
  const disabled = Boolean(busy || pending);
  async function perform(kind: NonNullable<typeof pending>, action: () => Promise<void>) {
    if (disabled || inFlight.current || !current()) return;
    inFlight.current = true; setPending(kind); setError(''); setNotice('');
    try { await action(); } catch (cause) { if (current()) setError(remoteGraphError(cause)); }
    finally { inFlight.current = false; if (current()) setPending(null); }
  }
  function openForm(value: RemoteConnectionWrite | null) {
    setForm(value); setSaved(null); setError(''); setNotice('');
  }
  async function bind(connection: RemoteConnection) {
    await onSelect(connection.connection_id);
    if (!current()) return;
    setSaved(null); setForm(null); setNotice(t('Connection saved and in use', '连接已保存并用于当前会话'));
  }
  async function saveAndUse() {
    if (!form) return;
    const { connection } = await saveRemoteGraph(form);
    if (!current()) return;
    // Persist this ID before binding: a retry must never create another connection.
    setSaved(connection); setForm(editable(connection));
    try { await bind(connection); }
    finally { if (current()) await onChanged?.(); }
  }
  return <DrawerShell ariaLabel={t('Remote databases', '远程数据库')} className="remote-graph-drawer" onClose={onClose}>{close => <div ref={content} onKeyDown={event => {
    if (event.key === 'Escape') { event.stopPropagation(); close(); }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(content.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)') || []);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }}>
    <div className="drawer-head"><strong>{t('Remote databases', '远程数据库')}</strong><button onClick={close} aria-label={t('Close', '关闭')}>×</button></div>
    <p className="remote-intro">{t('Connect to Neo4j and ask questions in this conversation. Your local graph remains available.', '连接 Neo4j，在当前会话中提问。本地图仍可使用。')}</p>
    {(error || loadError || unavailableReason) && <p className="remote-feedback is-error" role="alert">{error || loadError || remoteGraphError(unavailableReason)}</p>}
    {notice && <p className="remote-feedback is-success" role="status">{notice}</p>}
    {pending && <p className="remote-feedback" role="status">{pending === 'test' ? t('Testing connection…', '正在测试连接…') : pending === 'save' ? t('Saving and using…', '正在保存并使用…') : t('Updating connection…', '正在更新连接…')}</p>}
    {form ? <>
      <button className="remote-back" aria-label={t('Back to connections', '返回连接列表')} disabled={disabled} onClick={() => openForm(null)}>← {t('Back to connections', '返回连接列表')}</button>
      <form className="remote-connection-form" onSubmit={event => { event.preventDefault(); void perform(saved ? 'use' : 'save', () => saved ? bind(saved) : saveAndUse()); }}>
        <h3>{form.connection_id ? t('Edit connection', '编辑连接') : t('New connection', '新建连接')}</h3>
        <span className="remote-badge">Neo4j</span>
        {saved && <p className="remote-feedback" role="status">{t('Saved, not in use. Retry to use this saved connection in the conversation.', '已保存，未使用。请重试将此连接用于当前会话。')}</p>}
        {([
          ['name', t('Name', '名称')], ['uri', 'URI'], ['database', t('Database', '数据库')], ['username', t('Username', '用户名')], ['password', t('Password', '密码')],
        ] as const).map(([field, label]) => <label key={field}>{label}<input aria-label={label} required={field !== 'password' || !form.connection_id} disabled={disabled || Boolean(saved)} type={field === 'password' ? 'password' : 'text'} autoComplete={field === 'password' ? 'new-password' : 'off'} placeholder={field === 'uri' ? 'bolt://db.example.com:7687' : undefined} aria-describedby={['uri', 'database', 'password'].includes(field) ? `remote-help-${field}` : undefined} value={form[field] || ''} onChange={event => { setForm({ ...form, [field]: event.target.value }); setNotice(''); setError(''); }} />
          {field === 'uri' && <small id="remote-help-uri">{t('Example: bolt://db.example.com:7687. Connections start from the server; localhost refers to the server environment (or its container).', '例如 bolt://db.example.com:7687。连接由服务端发起；localhost 指服务端所在环境（或容器）。')}</small>}
          {field === 'database' && <small id="remote-help-database">{t('The database name in Neo4j, usually neo4j; this is not the connection display name.', 'Neo4j 中的数据库名称，通常为 neo4j，并非连接的显示名称。')}</small>}
          {field === 'password' && <small id="remote-help-password">{form.connection_id ? t('Leave the password blank to keep the saved credential.', '密码留空表示保留已存凭据。') : t('Credentials are stored on the server and are not sent to the model.', '凭据保存在服务端，不会发送给模型。')}</small>}
        </label>)}
        <div className="remote-actions remote-form-actions">
          <button type="button" disabled={disabled || Boolean(saved)} onClick={() => void perform('test', async () => {
            const result = await testRemoteGraph(form);
            if (!current()) return;
            if (!result.success || !result.connection_usable) throw new Error('test failed');
            setNotice(result.database_read_only === 'verified_read_only'
              ? t('Connection usable. Database read-only permissions verified. Testing does not save or select it.', '连接可用；已通过权限元数据核验数据库只读权限。测试不会保存或选择连接。')
              : result.database_read_only === 'not_read_only'
                ? t('Connection usable. This account has write or administrative permissions; application queries remain read-only. Testing does not save or select it.', '连接可用；账号具有写入或管理权限，应用仍仅允许查询。测试不会保存或选择连接。')
                : t('Connection usable. Database read-only permissions are unknown; application queries remain read-only. Testing does not save or select it.', '连接可用；数据库只读权限未验证，应用仍仅允许查询。测试不会保存或选择连接。'));
          })}>{t('Test connection', '测试连接')}</button>
          <button className="is-primary" type="submit" disabled={disabled}>{saved ? t('Retry use', '重试使用') : t('Save and use', '保存并使用')}</button>
        </div>
      </form>
    </> : <>
      <div className="remote-list-heading"><h3>{t('Connections', '连接列表')}</h3><button className="is-primary" disabled={disabled} onClick={() => openForm(blank())}>{t('New connection', '新建连接')}</button></div>
      {!connections.length && !busy && !loadError && <div className="remote-empty"><strong>{t('No connections yet', '暂无连接')}</strong><p>{t('Add a Neo4j connection to start exploring your database.', '新建 Neo4j 连接，开始探索数据库。')}</p></div>}
      <div className="remote-connection-list">{connections.map(connection => <div className={`remote-connection-row${selectedId === connection.connection_id ? ' is-selected' : ''}`} key={connection.connection_id}>
        <div className="remote-connection-title"><strong>{connection.name}</strong>{selectedId === connection.connection_id && <span className="remote-badge">{t('Selected', '当前会话已选择')}</span>}</div>
        <small>Neo4j · {connection.database} · {connection.username}</small>
        <div className="remote-actions">
          <button disabled={disabled} onClick={() => void perform('use', async () => { await onSelect(connection.connection_id); })}>{t(`Use ${connection.name}`, `使用 ${connection.name}`)}</button>
          <button disabled={disabled} onClick={() => openForm(editable(connection))}>{t(`Edit ${connection.name}`, `编辑 ${connection.name}`)}</button>
          <button className="is-danger" disabled={disabled} onClick={() => void perform('delete', async () => { await deleteRemoteGraph(connection.connection_id, connection.revision); if (current()) await onChanged?.(); })}>{t(`Delete ${connection.name}`, `删除 ${connection.name}`)}</button>
        </div>
      </div>)}</div>
      {selectedId && <div className="remote-disconnect"><button disabled={disabled} onClick={() => void perform('use', async () => { await onSelect(null); })}>{t('Disconnect from conversation', '解除会话选择')}</button><small>{t('Keeps the saved connection. Deleting removes it from your connection list.', '解除仅影响当前会话，保留连接；删除则从连接列表中移除。')}</small></div>}
    </>}
  </div>}</DrawerShell>;
}
