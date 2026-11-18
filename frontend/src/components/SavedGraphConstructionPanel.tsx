import { useEffect, useRef, useState } from 'react';
import type { Dataset } from '../types';
import { useI18n } from '../i18n';
import { useExampleLabels } from '../userCases';
import { Icon } from './Icon';
import { isMolecularGraph } from '../construction';
import { SavedGraphResult } from './SavedGraphResult';
import { MoleculeCanvas } from './MoleculeCanvas';
import { Popup, PopupBackdrop } from './PopupMotion';
import './GraphConstructionPanel.css';
import './MolecularConstructionPanel.css';

type Props = { dataset: Dataset; onClose: () => void; embedded?: boolean };

export function SavedGraphConstructionPanel({ dataset, onClose, embedded = false }: Props) {
  const { t } = useI18n();
  const labels = useExampleLabels();
  const molecular = isMolecularGraph(dataset);
  const [layer, setLayer] = useState<'schema' | 'instances'>(molecular ? 'instances' : 'schema');
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    setSelected(null);
  }, [dataset.dataset_id, dataset.project_id]);
  useEffect(() => {
    if (embedded) return;
    const previous = document.activeElement as HTMLElement | null;
    closeButton.current?.focus();
    return () => { previous?.focus(); };
  }, [embedded]);
  const Backdrop = embedded ? 'div' : PopupBackdrop;
  const Panel = embedded ? 'section' : Popup.section;
  return <Backdrop className={embedded ? 'construction-page' : 'construction-backdrop'}>
    <Panel ref={panel} className="construction-panel saved-graph-panel molecular-construction-panel" role={embedded ? 'region' : 'dialog'} aria-modal={embedded ? undefined : true} aria-label={molecular ? t('Molecular graph construction', '分子图构图') : t('Saved graph result', '已保存的构图结果')} onKeyDown={event => {
      if (embedded) return;
      if (event.key === 'Escape') { event.stopPropagation(); onClose(); }
      if (event.key === 'Tab') {
        const items = Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), [tabindex="0"]') || []).filter(item => item.getClientRects().length);
        const first = items[0], last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    }}>
      <header className="construction-header">
        <div className="construction-heading"><span className="construction-mark"><Icon name="graph" size={23} /></span><div>
          <span className="construction-eyebrow">{t('Construction result', '构图结果')}</span>
          <h2>{labels.name(dataset.name)}</h2>
        </div></div>
        <div className="construction-header-actions">{embedded
          ? <button ref={closeButton} className="secondary-command construction-back-button" onClick={onClose}><Icon name="arrow-left" size={16} /> {t('Back to construction', '返回构图')}</button>
          : <button ref={closeButton} className="plain-icon" aria-label={t('Close graph construction workspace', '关闭构图工作台')} onClick={onClose}><Icon name="close" size={20} /></button>}
        </div>
      </header>
      <div className="graph-layer-switch" role="group" aria-label={t('Result views', '构图结果视图')}>
        <button type="button" aria-pressed={layer === 'schema'} onClick={() => setLayer('schema')}>{t('Graph schema', '构图结构（Schema）')}</button>
        <button type="button" aria-pressed={layer === 'instances'} onClick={() => setLayer('instances')}>
          {molecular ? t('Molecular structure', '分子结构') : t('Graph instances', '实例图')}
        </button>
      </div>
      {molecular && layer === 'instances'
        ? <MoleculeCanvas dataset={dataset} highlights={[]} selected={selected} onSelect={setSelected} />
        : <SavedGraphResult dataset={dataset} layer={layer} />}
    </Panel>
  </Backdrop>;
}
