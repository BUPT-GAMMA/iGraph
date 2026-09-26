import { useState } from 'react';
import type { RemoteConnection, RemoteConnectionWrite } from '../types';
import { deleteRemoteGraph, saveRemoteGraph, testRemoteGraph } from '../api';
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
export function RemoteGraphConnections({ connections, selectedId, onSelect, onClose, onChanged, busy, unavailableReason, loadError }: Props) {
  const { t } = useI18n();
  const [form, setForm] = useState(blank);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const disabled = busy || pending;
  async function perform(action: () => Promise<void>) {
    setPending(true); setError(''); setNotice('');
    try { await action(); } catch (cause) { setError(remoteGraphError(cause)); }
    finally { setPending(false); }
  }
  function edit(connection: RemoteConnection) {
    setForm({ connection_id: connection.connection_id, revision: connection.revision, name: connection.name, uri: connection.uri, database: connection.database, username: connection.username, password: '' });
    setError(''); setNotice('');
  }
  return <DrawerShell ariaLabel={t('Remote databases', '远程数据库')} className="remote-graph-drawer" onClose={onClose}>{close => <>
    <div className="drawer-head"><strong>{t('Remote databases', '远程数据库')}</strong><button onClick={close} aria-label={t('Close', '关闭')}>×</button></div>
    <p>{t('Read-only queries. The local graph stays available in this conversation.', '只读查询；会话中的本地图仍然保留。')}</p>
    {(error || loadError || unavailableReason) && <p role="alert">{error || loadError || remoteGraphError(unavailableReason)}</p>}
    {notice && <p role="status">{notice}</p>}
    <div className="remote-connection-list">{connections.map(connection => <div className="remote-connection-row" key={connection.connection_id}>
      <div><strong>{connection.name}</strong><small>{connection.database} · {connection.username}</small>{selectedId === connection.connection_id && <span>{t('Selected', '已选择')}</span>}</div>
      <div className="remote-actions">
        <button disabled={disabled} onClick={() => void perform(async () => { await onSelect(connection.connection_id); })}>{t(`Use ${connection.name}`, `使用 ${connection.name}`)}</button>
        <button disabled={disabled} onClick={() => edit(connection)}>{t(`Edit ${connection.name}`, `编辑 ${connection.name}`)}</button>
        <button disabled={disabled} onClick={() => void perform(async () => { await deleteRemoteGraph(connection.connection_id, connection.revision); if (form.connection_id === connection.connection_id) setForm(blank()); await onChanged?.(); })}>{t(`Delete ${connection.name}`, `删除 ${connection.name}`)}</button>
      </div>
    </div>)}</div>
    {selectedId && <button disabled={disabled} onClick={() => void perform(async () => { await onSelect(null); })}>{t('Disconnect from conversation', '解除会话选择')}</button>}
    <form className="remote-connection-form" onSubmit={event => { event.preventDefault(); void perform(async () => { await saveRemoteGraph(form); setForm(blank()); setNotice(t('Connection saved', '连接已保存')); await onChanged?.(); }); }}>
      <h3>{form.connection_id ? t('Edit connection', '编辑连接') : t('New connection', '新建连接')}</h3>
      {([
        ['name', t('Name', '名称')], ['uri', 'URI'], ['database', t('Database', '数据库')], ['username', t('Username', '用户名')], ['password', t('Password', '密码')],
      ] as const).map(([field, label]) => <label key={field}>{label}<input required={field !== 'password' || !form.connection_id} disabled={disabled} type={field === 'password' ? 'password' : 'text'} autoComplete={field === 'password' ? 'new-password' : 'off'} value={form[field] || ''} onChange={event => { setForm(current => ({ ...current, [field]: event.target.value })); setNotice(''); }} /></label>)}
      {form.connection_id && <small>{t('Leave the password blank to keep the saved credential.', '密码留空表示保留已存凭据。')}</small>}
      <div className="remote-actions">
        <button type="button" disabled={disabled} onClick={() => void perform(async () => { const result = await testRemoteGraph(form); if (!result.success) throw new Error('test failed'); setNotice(t('Read-only connection verified. Not saved.', '只读连接验证成功，尚未保存。')); })}>{t('Test connection', '测试连接')}</button>
        <button type="submit" disabled={disabled}>{t('Save connection', '保存连接')}</button>
        {form.connection_id && <button type="button" disabled={disabled} onClick={() => setForm(blank())}>{t('New connection', '新建连接')}</button>}
      </div>
    </form>
  </>}</DrawerShell>;
}
