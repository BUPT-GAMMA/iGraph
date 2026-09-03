import { useEffect, useMemo, useState } from 'react';
import { apiFetch } from '../api';
import { DrawerShell } from './DrawerShell';
import { Icon } from './Icon';
import { SlidingTabs } from './SlidingTabs';

type MemoryItem = { memory_id?: string; key?: string; value?: string; category?: string; scope?: string };
type SkillItem = { name?: string; description?: string; source?: string; risk_level?: string; status?: string };

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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    setLoading(true); setError('');
    const request = tab === 'memory' ? readJson('/api/memory?scope=long_term&limit=200') : readJson('/api/skills?source=all');
    request.then(data => { if (tab === 'memory') setMemories(Array.isArray(data.items) ? data.items : []); else setSkills(Array.isArray(data.skills) ? data.skills : []); }).catch(cause => setError(cause.message || '管理数据读取失败')).finally(() => setLoading(false));
  }, [tab]);

  const filteredMemories = useMemo(() => { const text = query.trim().toLowerCase(); return memories.filter(item => !text || `${item.key || ''} ${item.value || ''} ${item.category || ''}`.toLowerCase().includes(text)); }, [memories, query]);
  const filteredSkills = useMemo(() => { const text = query.trim().toLowerCase(); return skills.filter(item => !text || `${item.name || ''} ${item.description || ''} ${item.source || ''}`.toLowerCase().includes(text)); }, [skills, query]);

  async function deleteMemory(item: MemoryItem) {
    if (!item.memory_id || !window.confirm(`确认删除记忆“${item.key || item.memory_id}”吗？`)) return;
    try { const response = await apiFetch(`/api/memory/${encodeURIComponent(item.memory_id)}?scope=${encodeURIComponent(item.scope || 'long_term')}`, { method: 'DELETE' }); if (!response.ok) throw new Error('记忆删除失败'); setMemories(current => current.filter(row => row.memory_id !== item.memory_id)); } catch (cause: any) { setError(cause.message || '记忆删除失败'); }
  }

  function changeTab(nextTab: 'memory' | 'skills') {
    setTab(nextTab);
    setQuery('');
    if (nextTab === 'memory') setSelectedSkill(null);
  }

  return <DrawerShell ariaLabel="Memory 与 Skills" className="management" onClose={onClose}>{requestClose => <><div className="drawer-head"><strong>Memory 与 Skills</strong><button className="plain-icon" onClick={requestClose} aria-label="关闭管理面板"><Icon name="close" size={17} /></button></div><SlidingTabs ariaLabel="管理内容" className="management-tabs" indicatorClassName="management-tab-indicator" options={[{ value: 'memory', label: <>Memory <span>{memories.length || ''}</span></> }, { value: 'skills', label: <>Skills <span>{skills.length || ''}</span></> }]} value={tab} onChange={changeTab} /><div className="management-search"><Icon name="search" size={14} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder={tab === 'memory' ? '搜索记忆内容' : '搜索 Skill'} /></div>{error ? <div className="management-error">{error}</div> : loading ? <div className="drawer-empty">正在读取...</div> : tab === 'memory' ? <div className="management-list">{filteredMemories.length ? filteredMemories.map((item, index) => <div className="memory-row" key={item.memory_id || index}><div className="management-icon"><Icon name="spark" size={15} /></div><div><strong>{item.key || '未命名记忆'}</strong><span>{item.value || '暂无内容'}</span><small>{item.category || item.scope || 'fact'}</small></div><button className="plain-icon danger-icon" onClick={() => deleteMemory(item)} aria-label="删除记忆"><Icon name="trash" size={14} /></button></div>) : <div className="drawer-empty">暂无匹配的记忆</div>}</div> : <div className="management-list">{filteredSkills.length ? filteredSkills.map((item, index) => <button className="skill-row" key={`${item.name || 'skill'}-${index}`} onClick={() => setSelectedSkill(item)}><div className="management-icon"><Icon name="cube" size={15} /></div><div><strong>{item.name || '未命名 Skill'}</strong><span>{item.description || '暂无描述'}</span><small>{item.source || 'unknown'}{item.risk_level ? ` · ${item.risk_level}` : ''}</small></div><Icon name="chevron" size={14} /></button>) : <div className="drawer-empty">暂无匹配的 Skill</div>}</div>}{selectedSkill ? <div className="skill-detail"><div className="inspector-title"><strong>{selectedSkill.name || 'Skill'}</strong><button className="plain-icon" onClick={() => setSelectedSkill(null)} aria-label="关闭 Skill 详情"><Icon name="close" size={14} /></button></div><p>{selectedSkill.description || '暂无描述'}</p><span>{selectedSkill.source || 'unknown'}{selectedSkill.status ? ` · ${selectedSkill.status}` : ''}</span></div> : null}</>}</DrawerShell>;
}
