import { useEffect, useRef, useState } from 'react';
import type { Dataset } from '../types';
import { useI18n } from '../i18n';
import { useExampleLabels } from '../userCases';
import { Icon } from './Icon';
import { MoleculeCanvas } from './MoleculeCanvas';
import { Popup, PopupBackdrop } from './PopupMotion';
import './GraphConstructionPanel.css';
import './MolecularConstructionPanel.css';

type Props = { dataset: Dataset; onClose: () => void; embedded?: boolean };

export function MolecularConstructionPanel({ dataset, onClose, embedded = false }: Props) {
  const { t } = useI18n();
  const labels = useExampleLabels();
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
    <Panel ref={panel} className="construction-panel molecular-construction-panel" role={embedded ? 'region' : 'dialog'} aria-modal={embedded ? undefined : true} aria-label={t('Molecular graph construction', '分子图构图')} onKeyDown={event => {
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
          <span className="construction-eyebrow">{t('Graph Construction / Molecular Structure', '构图工作台 / 分子结构')}</span>
          <h2>{labels.name(dataset.name)}</h2>
        </div></div>
        <div className="construction-header-actions">{embedded
          ? <button ref={closeButton} className="secondary-command construction-back-button" onClick={onClose}><Icon name="arrow-left" size={16} /> {t('Back to construction', '返回构图')}</button>
          : <button ref={closeButton} className="plain-icon" aria-label={t('Close graph construction workspace', '关闭构图工作台')} onClick={onClose}><Icon name="close" size={20} /></button>}
        </div>
      </header>
      <MoleculeCanvas dataset={dataset} highlights={[]} selected={selected} onSelect={setSelected} />
    </Panel>
  </Backdrop>;
}
