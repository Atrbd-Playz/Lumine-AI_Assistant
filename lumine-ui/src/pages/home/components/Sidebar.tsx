import { NAV_ITEMS } from "../constants";
import type { IconName } from "../types";
import { Icon, Mark } from "./Icon";

/** Persistent navigation rail for the desktop shell. */
export function Sidebar({ active, onChange, onSettings }: { active: string; onChange: (id: string) => void; onSettings: () => void }) {
  return <aside className="sidebar">
    <div className="sidebar-brand"><button className="logo-button" aria-label="Lumine home" onClick={() => onChange("home")}><Mark /></button><span>Lumine</span></div>
    <nav aria-label="Main navigation">{NAV_ITEMS.map(([id, icon, label]) => <button key={id} title={label} aria-label={label} className={`nav-button ${active === id ? "is-active" : ""}`} onClick={() => onChange(id)}><Icon name={icon as IconName} /><span>{label}</span></button>)}<button className={`nav-button settings-nav-button ${active === "settings" ? "is-active" : ""}`} title="Settings" onClick={onSettings} aria-label="Open settings"><Icon name="settings" /><span>Settings</span></button></nav>
  </aside>;
}
