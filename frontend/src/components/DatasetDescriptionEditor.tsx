import { useEffect, useRef, useState } from 'react';
import { updateProjectDataset } from '../api';
import { useI18n } from '../i18n';
import type { Dataset } from '../types';
import { useExampleLabels } from '../userCases';
import './DatasetDescriptionEditor.css';

type Props = { dataset: Dataset; onSaved?: (dataset: Dataset) => void; disabled?: boolean };

export function DatasetDescriptionEditor({ dataset, onSaved, disabled = false }: Props) {
  const { t } = useI18n();
  const labels = useExampleLabels();
  const [description, setDescription] = useState(dataset.description || '');
  const [saved, setSaved] = useState(dataset.description || '');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [failed, setFailed] = useState(false);
  const version = useRef(0);
  useEffect(() => {
    version.current += 1;
    setDescription(dataset.description || ''); setSaved(dataset.description || '');
    setMessage(''); setSaving(false);
    return () => { version.current += 1; };
  }, [dataset.dataset_id, dataset.project_id, dataset.description]);

  async function save() {
    const current = version.current;
    setSaving(true); setMessage(''); setFailed(false);
    try {
      const result = await updateProjectDataset(dataset.project_id, dataset.dataset_id, { description: description.trim() });
      if (current !== version.current) return;
      setSaved(result.dataset.description || ''); setDescription(result.dataset.description || '');
      setMessage(t('Description saved.', '描述已保存。'));
      onSaved?.(result.dataset);
    } catch (cause) {
      if (current !== version.current) return;
      setFailed(true); setMessage(cause instanceof Error ? cause.message : t('Unable to save description.', '保存描述失败。'));
    } finally { if (current === version.current) setSaving(false); }
  }

  const displayedDescription = description === saved ? labels.description(description) : description;
  const unchanged = description.trim() === saved || description.trim() === labels.description(saved);
  return <section className="dataset-description-editor" aria-label={t('Dataset description editor', '数据集描述编辑')}>
    <label>{t('Dataset description', '数据集描述')}<textarea rows={3} maxLength={4000} value={displayedDescription} disabled={disabled || saving} onChange={event => { setDescription(event.target.value); setMessage(''); }} placeholder={t('Describe the source, entities, relationships, and intended use of this dataset.', '描述数据来源、实体含义、关系和使用场景。')} /></label>
    <div><small>{displayedDescription.length} / 4000</small><button type="button" className="secondary-command" disabled={disabled || saving || unchanged} onClick={() => void save()}>{saving ? t('Saving…', '正在保存…') : t('Save description', '保存描述')}</button></div>
    {message && <p role={failed ? 'alert' : 'status'}>{message}</p>}
  </section>;
}
