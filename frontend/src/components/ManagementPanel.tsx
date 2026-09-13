import { useEffect, useMemo, useState } from 'react';
import { apiFetch } from '../api';
import { DrawerShell } from './DrawerShell';
import { Icon } from './Icon';
import { SlidingTabs } from './SlidingTabs';
import { SkillDetails } from './SkillDetails';

type MemoryItem = { memory_id?: string; key?: string; value?: string; category?: string; scope?: string };
type SkillItem = { name?: string; description?: string; source?: string; risk_level?: string; status?: string };

const METADATA_LABELS: Record<string, string> = {
  fact: '事实',
  preference: '偏好',
  profile: '个人信息',
  long_term: '长期记忆',
  builtin: '内置',
  bundled: '内置',
  custom: '自定义',
  user: '用户添加',
  workspace: '工作区',
  low: '低风险',
  medium: '中风险',
  high: '高风险',
  ready: '就绪',
  enabled: '已启用',
  disabled: '已停用',
  unavailable: '不可用',
};

function metadataLabel(value: string | undefined, fallback: string) {
  if (!value) return fallback;
  return METADATA_LABELS[value] || value;
}

async function readJson(path: string) {
  const response = await apiFetch(path);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || '管理数据读取失败');
  return data;
}

export function ManagementPanel({ onClose, initialTab = 'memory' }: { onClose: () => void; initialTab?: 'memory' | 'skills' }) {
  const [tab, setTab] = useState<'memory' | 'skills'>(initialTab);
  const [query, setQuery] = useState('');
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [selectedSkill, setSelectedSkill] = useState<SkillItem | null>(null);
  const [uploading, setUploading] = useState(false);
  const [skillBusy, setSkillBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [skillPage, setSkillPage] = useState(1);
  const skillsPerPage = 9;
  useEffect(() => { setTab(initialTab); setQuery(''); setSelectedSkill(null); setSkillPage(1); }, [initialTab]);

  useEffect(() => {
    let active = true;
    setLoading(true); setError('');
    const request = tab === 'memory' ? readJson('/api/memory?scope=long_term&limit=200') : readJson('/api/skills?source=all');
    request.then(data => { if (!active) return; if (tab === 'memory') setMemories(Array.isArray(data.items) ? data.items : []); else setSkills(Array.isArray(data.skills) ? data.skills : []); }).catch(cause => { if (active) setError(cause.message || '管理数据读取失败'); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [tab]);



  const filteredMemories = useMemo(() => { const text = query.trim().toLowerCase(); return memories.filter(item => !text || `${item.key || ''} ${item.value || ''} ${item.category || ''}`.toLowerCase().includes(text)); }, [memories, query]);
  const filteredSkills = useMemo(() => { const text = query.trim().toLowerCase(); return skills.filter(item => !text || `${item.name || ''} ${item.description || ''} ${item.source || ''}`.toLowerCase().includes(text)); }, [skills, query]);

  const skillPageCount = Math.max(1, Math.ceil(filteredSkills.length / skillsPerPage));
  const currentPage = Math.min(skillPage, skillPageCount);
  const visibleSkills = filteredSkills.slice((currentPage - 1) * skillsPerPage, currentPage * skillsPerPage);
  useEffect(() => { setSkillPage(1); }, [query, tab]);

  async function deleteMemory(item: MemoryItem) {
    if (!item.memory_id || !window.confirm(`确认删除记忆“${item.key || item.memory_id}”吗？`)) return;
    try { const response = await apiFetch(`/api/memory/${encodeURIComponent(item.memory_id)}?scope=${encodeURIComponent(item.scope || 'long_term')}`, { method: 'DELETE' }); if (!response.ok) throw new Error('记忆删除失败'); setMemories(current => current.filter(row => row.memory_id !== item.memory_id)); } catch (cause: any) { setError(cause.message || '记忆删除失败'); }
  }

  function changeTab(nextTab: 'memory' | 'skills') {
    setTab(nextTab);
    setQuery('');
    if (nextTab === 'memory') setSelectedSkill(null);
  }

  async function uploadSkill(file: File) {
    setUploading(true); setError('');
    try {
      const body = new FormData(); body.append('file', file);
      const response = await apiFetch('/api/skills/upload', { method: 'POST', body });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '技能上传失败');
      const data = await readJson('/api/skills?source=all');
      setSkills(Array.isArray(data.skills) ? data.skills : []);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '技能上传失败'); }
    finally { setUploading(false); }
  }

  async function skillAction(name: string, action: 'approve' | 'reject') {
    if (skillBusy) return;
    setSkillBusy(true); setError('');
    try {
      const response = await apiFetch('/api/skills/' + encodeURIComponent(name) + '/' + action,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      if (!response.ok) throw new Error('技能审核失败');
      const data = await readJson('/api/skills?source=all');
      setSkills(Array.isArray(data.skills) ? data.skills : []); setSelectedSkill(null);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '技能审核失败'); }
    finally { setSkillBusy(false); }
  }

  return <DrawerShell ariaLabel="记忆与技能" className="management" onClose={onClose}>{requestClose => <><div className="drawer-head"><strong>记忆与技能</strong><button className="plain-icon" onClick={requestClose} aria-label="关闭管理面板"><Icon name="close" size={17} /></button></div><SlidingTabs ariaLabel="管理内容" className="management-tabs" indicatorClassName="management-tab-indicator" options={[{ value: 'memory', label: <>记忆 <span>{memories.length || ''}</span></> }, { value: 'skills', label: <>技能 <span>{skills.length || ''}</span></> }]} value={tab} onChange={changeTab} /><div className="management-search">{tab === 'skills' && <label className="skill-upload">{uploading ? '上传中…' : '上传技能 ZIP'}<input aria-label="上传技能 ZIP" type="file" accept=".zip" hidden disabled={uploading} onChange={event => { const file = event.target.files?.[0]; if (file) void uploadSkill(file); event.currentTarget.value = ''; }} /></label>}<Icon name="search" size={14} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder={tab === 'memory' ? '搜索记忆内容' : '搜索技能'} /></div>{error ? <div className="management-error">{error}</div> : loading ? <div className="drawer-empty">正在读取...</div> : tab === 'memory' ? <div className="management-list">{filteredMemories.length ? filteredMemories.map((item, index) => <div className="memory-row" key={item.memory_id || index}><div className="management-icon"><Icon name="spark" size={15} /></div><div><strong>{item.key || '未命名记忆'}</strong><span>{item.value || '暂无内容'}</span><small>{metadataLabel(item.category || item.scope, '事实')}</small></div><button className="plain-icon danger-icon" onClick={() => deleteMemory(item)} aria-label="删除记忆"><Icon name="trash" size={14} /></button></div>) : <div className="drawer-empty">暂无匹配的记忆</div>}</div> : <div className="management-list skill-grid">{filteredSkills.length ? visibleSkills.map((item, index) => <button className="skill-row" key={`${item.name || 'skill'}-${index}`} onClick={() => setSelectedSkill(item)}><div className="management-icon"><Icon name="cube" size={15} /></div><div><strong>{item.name || '未命名技能'}</strong><span>{item.description || '暂无描述'}</span><small>{metadataLabel(item.source, '未知来源')}{item.risk_level ? ` · ${metadataLabel(item.risk_level, '')}` : ''}</small></div><Icon name="chevron" size={14} /></button>) : <div className="drawer-empty">暂无匹配的技能</div>}</div>}{tab === 'skills' && !loading && skillPageCount > 1 ? <nav className="skill-pagination" aria-label="技能分页">
    <button type="button" disabled={currentPage <= 1} onClick={() => setSkillPage(currentPage - 1)}>上一页</button>
    <span>{currentPage} / {skillPageCount}</span>
    <button type="button" disabled={currentPage >= skillPageCount} onClick={() => setSkillPage(currentPage + 1)}>下一页</button>
  </nav> : null}{selectedSkill ? <SkillDetails closeLabel="关闭技能详情" summary={metadataLabel(selectedSkill.source, '未知来源') + (selectedSkill.status ? ' · ' + metadataLabel(selectedSkill.status, '') : '')} skill={selectedSkill} onClose={() => setSelectedSkill(null)}>{selectedSkill.source === 'review_queue' ? <><button disabled={skillBusy} onClick={() => void skillAction(selectedSkill.name || '', 'approve')}>批准</button><button disabled={skillBusy} onClick={() => void skillAction(selectedSkill.name || '', 'reject')}>拒绝</button></> : null}</SkillDetails> : null}</>}</DrawerShell>;
}
