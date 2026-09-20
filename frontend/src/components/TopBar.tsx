import { BrandLogo, Icon } from './Icon';
import { useI18n } from '../i18n';

export function TopBar({ onMenu, onModels }: { onMenu: () => void; onModels?: () => void }) {
  const { language, setLanguage, t } = useI18n();
  return <header className="topbar">
    <div className="brand-lockup"><div className="brand-mark"><BrandLogo /></div><strong>iGraph</strong></div>
    <div className="top-actions">
      {onModels && <button className="top-models" onClick={onModels}><Icon name="settings" size={15} />{t('Models', '模型管理')}</button>}
      <div className="language-switcher" role="group" aria-label={t('Language', '语言')}>
        <button type="button" lang="en" className={language === 'en' ? 'is-active' : ''} aria-pressed={language === 'en'} onClick={() => setLanguage('en')}>EN</button>
        <button type="button" lang="zh-CN" className={language === 'zh' ? 'is-active' : ''} aria-pressed={language === 'zh'} onClick={() => setLanguage('zh')}>中文</button>
      </div>
    </div>
    <button className="mobile-nav-toggle" onClick={onMenu} aria-label={t('Open sidebar', '打开侧栏')} title={t('Open sidebar', '打开侧栏')}><Icon name="panel" size={22} /></button>
  </header>;
}
