import { useEffect, useRef, useState } from 'react';
import type { RemoteConnection, RemoteConnectionWrite, RemoteDatabaseType, RemoteGraphKind } from '../types';
import { remoteDatabaseName } from '../types';
import { deleteRemoteGraph, listRemoteGraphTypes, saveRemoteGraph, testRemoteGraph, USER_KEY } from '../api';
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
const blank = (kind: RemoteGraphKind = 'neo4j'): RemoteConnectionWrite => ({ kind, name: '', uri: '', database: kind === 'neo4j' ? 'neo4j' : '', username: '', password: '' });
const editable = (c: RemoteConnection): RemoteConnectionWrite => ({ connection_id: c.connection_id, revision: c.revision, name: c.name, kind: c.kind || 'neo4j', uri: c.uri, database: c.database, username: c.username, password: '' });
export function RemoteGraphConnections({ connections, selectedId, onSelect, onClose, onChanged, busy, unavailableReason, loadError }: Props) {
  const { t } = useI18n();
  const [form, setForm] = useState<RemoteConnectionWrite | null>(null);
  const [pending, setPending] = useState<'test' | 'save' | 'use' | 'delete' | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saved, setSaved] = useState<RemoteConnection | null>(null);
  const [types, setTypes] = useState<RemoteDatabaseType[]>([]);
  const [typesError, setTypesError] = useState(false);
  const [typesLoading, setTypesLoading] = useState(true);
  const testEpoch = useRef(0);
  const operationId = useRef(0);
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
  async function loadTypes() {
    setTypesLoading(true); setTypesError(false);
    try {
      const result = await listRemoteGraphTypes();
      if (current()) {
        if (!result.types.length) throw new Error('No registered database types');
        setTypes(result.types); setTypesError(false);
        setForm(previous => previous && !previous.connection_id && !result.types.some(type => type.kind === previous.kind)
          ? { ...blank(result.types[0].kind), name: previous.name } : previous);
      }
    } catch {
      if (current()) { setTypes([]); setTypesError(true); }
    } finally { if (current()) setTypesLoading(false); }
  }
  useEffect(() => { void loadTypes(); }, []);
  const disabled = Boolean(busy || pending);
  const selectedType = types.find(type => type.kind === form?.kind);
  async function perform(kind: NonNullable<typeof pending>, action: () => Promise<void>) {
    if (disabled || inFlight.current || !current()) return;
    const operation = ++operationId.current;
    inFlight.current = true; setPending(kind); setError(''); setNotice('');
    const epoch = testEpoch.current;
    try { await action(); } catch (cause) { if (current() && (kind !== 'test' || epoch === testEpoch.current)) setError(remoteGraphError(cause)); }
    finally { if (operation === operationId.current) { inFlight.current = false; if (current()) setPending(null); } }
  }
  function openForm(value: RemoteConnectionWrite | null) {
    testEpoch.current++;
    setForm(value); setSaved(null); setError(''); setNotice('');
  }
  async function bind(connection: RemoteConnection) {
    await onSelect(connection.connection_id);
    if (!current()) return;
    setSaved(null); setForm(null); setNotice(t('Connection saved and in use', '连接已保存并用于当前会话'));
  }
  async function saveAndUse() {
    if (!form) return;
    if (!form.connection_id && !types.some(type => type.kind === form.kind)) return;
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
    const focusable = Array.from(content.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled)') || []);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }}>
    <div className="drawer-head"><strong>{t('Remote databases', '远程数据库')}</strong><button onClick={close} aria-label={t('Close', '关闭')}>×</button></div>
    <p className="remote-intro">{t('Connect to a remote graph database and ask questions in this conversation. Your local graph remains available.', '连接远程图数据库，在当前会话中提问。本地图仍可使用。')}</p>
    {typesError && <div className="remote-feedback is-error" role="alert">{t('Could not load database types.', '无法加载数据库类型。')} <button type="button" onClick={() => void loadTypes()}>{t('Retry database types', '重试加载数据库类型')}</button></div>}
    {(error || loadError || unavailableReason) && <p className="remote-feedback is-error" role="alert">{error || loadError || remoteGraphError(unavailableReason)}</p>}
    {notice && <p className="remote-feedback is-success" role="status">{notice}</p>}
    {pending && <p className="remote-feedback" role="status">{pending === 'test' ? t('Testing connection…', '正在测试连接…') : pending === 'save' ? t('Saving and using…', '正在保存并使用…') : t('Updating connection…', '正在更新连接…')}</p>}
    {form ? <>
      <button className="remote-back" aria-label={t('Back to connections', '返回连接列表')} disabled={disabled} onClick={() => openForm(null)}>← {t('Back to connections', '返回连接列表')}</button>
      <form className="remote-connection-form" onSubmit={event => { event.preventDefault(); void perform(saved ? 'use' : 'save', () => saved ? bind(saved) : saveAndUse()); }}>
        <h3>{form.connection_id ? t('Edit connection', '编辑连接') : t('New connection', '新建连接')}</h3>
        {form.connection_id ? <span className="remote-badge">{types.find(type => type.kind === form.kind)?.display_name || remoteDatabaseName(form.kind || 'neo4j')}</span>
          : <label>{t('Database type', '数据库类型')}<select aria-label={t('Database type', '数据库类型')} value={form.kind || 'neo4j'} disabled={Boolean(busy || saved || typesLoading || typesError || !types.length || (pending && pending !== 'test'))} onChange={event => {
            const kind = event.target.value as RemoteGraphKind;
            if (!types.some(type => type.kind === kind)) return;
            testEpoch.current++;
            if (pending === 'test') { operationId.current++; inFlight.current = false; setPending(null); }
            setForm({ ...blank(kind), name: form.name });
            setNotice(t('Changing type clears the previous address, target, username, password and test result.', '切换类型会清除原地址、目标、用户名、密码及测试结果。'));
            setError('');
          }}>{types.length ? types.map(type => <option key={type.kind} value={type.kind}>{type.display_name}</option>) : <option value="neo4j">{t('Loading types…', '正在加载类型…')}</option>}</select>
          {(form.uri || form.username || form.password || form.database !== (form.kind === 'neo4j' ? 'neo4j' : '')) && <small>{t('Changing type will clear the address, target, username, password and test result.', '切换类型将清除地址、目标、用户名、密码及测试结果。')}</small>}</label>}
        {saved && <p className="remote-feedback" role="status">{t('Saved, not in use. Retry to use this saved connection in the conversation.', '已保存，未使用。请重试将此连接用于当前会话。')}</p>}
        {([
          ['name', t('Name', '名称')], ['uri', 'URI'], ['database', (selectedType?.target_label === 'Graph' || (!selectedType && form.kind === 'falkordb')) ? t('Graph', '图名称') : t('Database', '数据库')], ['username', t('Username', '用户名')], ['password', t('Password', '密码')],
        ] as const).map(([field, label]) => <label key={field}>{label}<input aria-label={label} required={field === 'username' ? (selectedType?.username_required ?? form.kind !== 'falkordb') : field !== 'password' || !form.connection_id} disabled={disabled || Boolean(saved)} type={field === 'password' ? 'password' : 'text'} autoComplete={field === 'password' ? 'new-password' : 'off'} placeholder={field === 'uri' ? selectedType?.uri_example : undefined} aria-describedby={['uri', 'database', 'password'].includes(field) ? `remote-help-${field}` : undefined} value={form[field] || ''} onChange={event => { testEpoch.current++; setForm({ ...form, [field]: event.target.value }); setNotice(''); setError(''); }} />
          {field === 'uri' && <small id="remote-help-uri">{t(`Example: ${selectedType?.uri_example || (form.kind === 'falkordb' ? 'rediss://db.example.com:6379' : 'bolt+s://db.example.com:7687')}. Connections start from the server; localhost refers to the server environment (or its container).`, `例如 ${selectedType?.uri_example || (form.kind === 'falkordb' ? 'rediss://db.example.com:6379' : 'bolt+s://db.example.com:7687')}。连接由服务端发起；localhost 指服务端所在环境（或容器）。`)}</small>}
          {field === 'database' && <small id="remote-help-database">{form.kind === 'falkordb' ? t('The graph name in FalkorDB; this is not a Redis logical database number.', 'FalkorDB 中的图名称，并非 Redis 逻辑库编号。') : t('The database name in Neo4j, usually neo4j; this is not the connection display name.', 'Neo4j 中的数据库名称，通常为 neo4j，并非连接的显示名称。')}</small>}
          {field === 'password' && <small id="remote-help-password">{form.connection_id ? t('Leave the password blank to keep the saved credential.', '密码留空表示保留已存凭据。') : t('Credentials are stored on the server and are not sent to the model.', '凭据保存在服务端，不会发送给模型。')}</small>}
        </label>)}
        <div className="remote-actions remote-form-actions">
          <button type="button" disabled={disabled || Boolean(saved) || (!form.connection_id && !types.some(type => type.kind === form.kind))} onClick={() => void perform('test', async () => {
            const epoch = testEpoch.current;
            const result = await testRemoteGraph(form);
            if (!current() || epoch !== testEpoch.current) return;
            if (!result.success || !result.connection_usable) throw new Error('test failed');
            setNotice(result.database_read_only === 'verified_read_only'
              ? t('Connection usable. Database read-only permissions verified. Testing does not save or select it.', '连接可用；已通过权限元数据核验数据库只读权限。测试不会保存或选择连接。')
              : result.database_read_only === 'not_read_only'
                ? t('Connection usable. This account has write or administrative permissions; application queries remain read-only. Testing does not save or select it.', '连接可用；账号具有写入或管理权限，应用仍仅允许查询。测试不会保存或选择连接。')
                : t('Connection usable. Database read-only permissions are unknown; application queries remain read-only. Testing does not save or select it.', '连接可用；数据库只读权限未验证，应用仍仅允许查询。测试不会保存或选择连接。'));
          })}>{t('Test connection', '测试连接')}</button>
          <button className="is-primary" type="submit" disabled={disabled || (!form.connection_id && !types.some(type => type.kind === form.kind))}>{saved ? t('Retry use', '重试使用') : t('Save and use', '保存并使用')}</button>
        </div>
      </form>
    </> : <>
      <div className="remote-list-heading"><h3>{t('Connections', '连接列表')}</h3><button className="is-primary" disabled={disabled || typesError || (!typesLoading && !types.length)} onClick={() => openForm(blank(types[0]?.kind || 'neo4j'))}>{t('New connection', '新建连接')}</button></div>
      {!connections.length && !busy && !loadError && <div className="remote-empty"><strong>{t('No connections yet', '暂无连接')}</strong><p>{t('Add a remote connection to start exploring your database.', '新建远程连接，开始探索数据库。')}</p></div>}
      <div className="remote-connection-list">{connections.map(connection => <div className={`remote-connection-row${selectedId === connection.connection_id ? ' is-selected' : ''}`} key={connection.connection_id}>
        <div className="remote-connection-title"><strong>{connection.name}</strong>{selectedId === connection.connection_id && <span className="remote-badge">{t('Selected', '当前会话已选择')}</span>}</div>
        <small>{remoteDatabaseName(connection.kind || 'neo4j')} · {connection.database}{connection.username ? ` · ${connection.username}` : ''}</small>
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
