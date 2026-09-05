import { BrandLogo, Icon } from './Icon';

export function TopBar({ onMenu }: { onMenu: () => void }) {
  return <header className="topbar">
    <div className="brand-lockup"><div className="brand-mark"><BrandLogo /></div><strong>iGraph</strong></div>
    <button className="mobile-nav-toggle" onClick={onMenu} aria-label="打开侧栏" title="打开侧栏"><Icon name="panel" size={22} /></button>
  </header>;
}
