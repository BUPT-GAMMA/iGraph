import { useActionDialog } from './useActionDialog';
import { PopupPresence } from './PopupMotion';
import { useEffect, useMemo, useState } from 'react';
import { apiFetch } from '../api';
import { DrawerShell } from './DrawerShell';
import { Icon } from './Icon';
import { SlidingTabs } from './SlidingTabs';
import { SkillDetails } from './SkillDetails';
import { useI18n, type Translate } from '../i18n';

type MemoryItem = { memory_id?: string; key?: string; value?: string; category?: string; scope?: string };
type SkillItem = { name?: string; description?: string; source?: string; risk_level?: string; status?: string };

const METADATA_LABELS: Record<string, readonly [english: string, chinese: string]> = {
  fact: ['Fact', '事实'],
  preference: ['Preference', '偏好'],
  profile: ['Profile', '个人信息'],
  long_term: ['Long-term memory', '长期记忆'],
  builtin: ['Built in', '内置'],
  bundled: ['Built in', '内置'],
  custom: ['Custom', '自定义'],
  user: ['Added by user', '用户添加'],
  workspace: ['Workspace', '工作区'],
  low: ['Low risk', '低风险'],
  medium: ['Medium risk', '中风险'],
  high: ['High risk', '高风险'],
  ready: ['Ready', '就绪'],
  enabled: ['Enabled', '已启用'],
  disabled: ['Disabled', '已停用'],
  unavailable: ['Unavailable', '不可用'],
};

function metadataLabel(value: string | undefined, fallback: readonly [string, string], t: Translate) {
  if (!value) return t(...fallback);
  const label = METADATA_LABELS[value];
  return label ? t(...label) : value;
}

async function readJson(path: string, t: Translate) {
  const response = await apiFetch(path);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || t('Failed to load management data', '管理数据读取失败'));
  return data;
}

export function ManagementPanel({ onClose, initialTab = 'memory' }: { onClose: () => void; initialTab?: 'memory' | 'skills' }) {
  const { t } = useI18n();
  const { askConfirm, actionDialog } = useActionDialog();
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
    const request = tab === 'memory' ? readJson('/api/memory?scope=long_term&limit=200', t) : readJson('/api/skills?source=all', t);
    request.then(data => { if (!active) return; if (tab === 'memory') setMemories(Array.isArray(data.items) ? data.items : []); else setSkills(Array.isArray(data.skills) ? data.skills : []); }).catch(cause => { if (active) setError(cause.message || t('Failed to load management data', '管理数据读取失败')); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [tab, t]);



  const filteredMemories = useMemo(() => { const text = query.trim().toLowerCase(); return memories.filter(item => !text || `${item.key || ''} ${item.value || ''} ${item.category || ''}`.toLowerCase().includes(text)); }, [memories, query]);
  const filteredSkills = useMemo(() => { const text = query.trim().toLowerCase(); return skills.filter(item => !text || `${item.name || ''} ${item.description || ''} ${item.source || ''}`.toLowerCase().includes(text)); }, [skills, query]);

  const skillPageCount = Math.max(1, Math.ceil(filteredSkills.length / skillsPerPage));
  const currentPage = Math.min(skillPage, skillPageCount);
  const visibleSkills = filteredSkills.slice((currentPage - 1) * skillsPerPage, currentPage * skillsPerPage);
  useEffect(() => { setSkillPage(1); }, [query, tab]);

  async function deleteMemory(item: MemoryItem) {
    if (!item.memory_id || !await askConfirm(t(`Delete memory “${item.key || item.memory_id}”?`, `确认删除记忆“${item.key || item.memory_id}”吗？`))) return;
    try { const response = await apiFetch(`/api/memory/${encodeURIComponent(item.memory_id)}?scope=${encodeURIComponent(item.scope || 'long_term')}`, { method: 'DELETE' }); if (!response.ok) throw new Error(t('Failed to delete memory', '记忆删除失败')); setMemories(current => current.filter(row => row.memory_id !== item.memory_id)); } catch (cause: any) { setError(cause.message || t('Failed to delete memory', '记忆删除失败')); }
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
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : t('Failed to upload skill', '技能上传失败'));
      const data = await readJson('/api/skills?source=all', t);
      setSkills(Array.isArray(data.skills) ? data.skills : []);
    } catch (cause) { setError(cause instanceof Error ? cause.message : t('Failed to upload skill', '技能上传失败')); }
    finally { setUploading(false); }
  }

  async function skillAction(name: string, action: 'approve' | 'reject') {
    if (skillBusy) return;
    // Guard: only skills in review_queue can be approved or rejected.
    if (selectedSkill?.source !== 'review_queue') return;
    setSkillBusy(true); setError('');
    try {
      const response = await apiFetch('/api/skills/' + encodeURIComponent(name) + '/' + action,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      if (!response.ok) throw new Error(t('Failed to review skill', '技能审核失败'));
      const data = await readJson('/api/skills?source=all', t);
      setSkills(Array.isArray(data.skills) ? data.skills : []); setSelectedSkill(null);
    } catch (cause) { setError(cause instanceof Error ? cause.message : t('Failed to review skill', '技能审核失败')); }
    finally { setSkillBusy(false); }
  }

  async function deleteSkill(skill: SkillItem) {
    if (skillBusy || !skill.name || !['user', 'review_queue'].includes(skill.source || '')
      || !await askConfirm(t(`Delete uploaded skill “${skill.name}”?`, `确认删除上传的技能“${skill.name}”吗？`))) return;
    setSkillBusy(true); setError('');
    try {
      const source = skill.source as 'user' | 'review_queue';
      const response = await apiFetch(`/api/skills/${encodeURIComponent(skill.name)}?source=${source}`, { method: 'DELETE' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : t('Failed to delete skill', '技能删除失败'));
      setSkills(current => current.filter(item => !(item.name === skill.name && item.source === skill.source)));
      setSelectedSkill(null);
    } catch (cause) { setError(cause instanceof Error ? cause.message : t('Failed to delete skill', '技能删除失败')); }
    finally { setSkillBusy(false); }
  }

  const panelTitle = t('Memory and skills', '记忆与技能');
  return <DrawerShell ariaLabel={panelTitle} className="management" onClose={onClose}>{requestClose => <>
    {actionDialog}
    <div className="drawer-head">
      <strong>{panelTitle}</strong>
      <button className="plain-icon" onClick={requestClose} aria-label={t('Close management panel', '关闭管理面板')}><Icon name="close" size={17} /></button>
    </div>
    <SlidingTabs
      ariaLabel={t('Management content', '管理内容')}
      className="management-tabs"
      indicatorClassName="management-tab-indicator"
      options={[
        { value: 'memory', label: <>{t('Memory', '记忆')} <span>{memories.length || ''}</span></> },
        { value: 'skills', label: <>{t('Skills', '技能')} <span>{skills.length || ''}</span></> },
      ]}
      value={tab}
      onChange={changeTab}
    />
    <div className="management-search">
      {tab === 'skills' && <label className="skill-upload">
        {uploading ? t('Uploading…', '上传中…') : t('Upload skill (.md / .zip)', '上传技能（.md / .zip）')}
        <input aria-label={t('Upload skill (.md / .zip)', '上传技能（.md / .zip）')} type="file" accept=".md,.zip" hidden disabled={uploading} onChange={event => { const file = event.target.files?.[0]; if (file) void uploadSkill(file); event.currentTarget.value = ''; }} />
      </label>}
      <Icon name="search" size={14} />
      <input value={query} onChange={event => setQuery(event.target.value)} placeholder={tab === 'memory' ? t('Search memories', '搜索记忆内容') : t('Search skills', '搜索技能')} />
    </div>
    {error ? <div className="management-error">{error}</div> : loading ? <div className="drawer-empty">{t('Loading…', '正在读取…')}</div> : tab === 'memory' ? <div className="management-list">
      {filteredMemories.length ? filteredMemories.map((item, index) => <div className="memory-row" key={item.memory_id || index}>
        <div className="management-icon"><Icon name="spark" size={15} /></div>
        <div><strong>{item.key || t('Untitled memory', '未命名记忆')}</strong><span>{item.value || t('No content', '暂无内容')}</span><small>{metadataLabel(item.category || item.scope, ['Fact', '事实'], t)}</small></div>
        <button className="plain-icon danger-icon" onClick={() => deleteMemory(item)} aria-label={t('Delete memory', '删除记忆')}><Icon name="trash" size={14} /></button>
      </div>) : <div className="drawer-empty">{t('No matching memories', '暂无匹配的记忆')}</div>}
    </div> : <div className="management-list skill-grid">
      {filteredSkills.length ? visibleSkills.map((item, index) => <button className="skill-row" key={`${item.name || 'skill'}-${index}`} onClick={() => setSelectedSkill(item)}>
        <div className="management-icon"><Icon name="cube" size={15} /></div>
        <div><strong>{item.name || t('Untitled skill', '未命名技能')}</strong><span>{item.description || t('No description', '暂无描述')}</span><small>{metadataLabel(item.source, ['Unknown source', '未知来源'], t)}{item.risk_level ? ` · ${metadataLabel(item.risk_level, ['', ''], t)}` : ''}</small></div>
        <Icon name="chevron" size={14} />
      </button>) : <div className="drawer-empty">{t('No matching skills', '暂无匹配的技能')}</div>}
    </div>}
    {tab === 'skills' && !loading && skillPageCount > 1 ? <nav className="skill-pagination" aria-label={t('Skill pages', '技能分页')}>
      <button type="button" disabled={currentPage <= 1} onClick={() => setSkillPage(currentPage - 1)}>{t('Previous', '上一页')}</button>
      <span>{currentPage} / {skillPageCount}</span>
      <button type="button" disabled={currentPage >= skillPageCount} onClick={() => setSkillPage(currentPage + 1)}>{t('Next', '下一页')}</button>
    </nav> : null}
    <PopupPresence>{selectedSkill ? <SkillDetails
      closeLabel={t('Close skill details', '关闭技能详情')}
      summary={metadataLabel(selectedSkill.source, ['Unknown source', '未知来源'], t) + (selectedSkill.status ? ' · ' + metadataLabel(selectedSkill.status, ['', ''], t) : '')}
      skill={selectedSkill}
      onClose={() => setSelectedSkill(null)}
    >{selectedSkill.source === 'review_queue' ? <>
      <button disabled={skillBusy} onClick={() => void skillAction(selectedSkill.name || '', 'approve')}>{t('Approve', '批准')}</button>
      <button disabled={skillBusy} onClick={() => void skillAction(selectedSkill.name || '', 'reject')}>{t('Reject', '拒绝')}</button>
    </> : null}{selectedSkill.source === 'user' || selectedSkill.source === 'review_queue' ?
      <button disabled={skillBusy} onClick={() => void deleteSkill(selectedSkill)}>{t('Delete skill', '删除技能')}</button> : null}</SkillDetails> : null}</PopupPresence>
  </>}</DrawerShell>;
}
