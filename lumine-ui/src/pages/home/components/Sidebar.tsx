import { NAV_ITEMS } from "../constants";
import type { IconName } from "../types";
import { Icon, Mark } from "./Icon";

/** Persistent navigation rail for the desktop shell. */
export function Sidebar({ active, onChange, onSettings }: { active: string; onChange: (id: string) => void; onSettings: () => void }) {
  return <aside className="sidebar">
    <button className="logo-button" aria-label="Lumine home"><Mark /></button>
    <nav aria-label="Main navigation">{NAV_ITEMS.map(([id, icon]) => <button key={id} title={id} className={`nav-button ${active === id ? "is-active" : ""}`} onClick={() => onChange(id)}><Icon name={icon as IconName} /></button>)}</nav>
    <button className="nav-button" title="Settings" onClick={onSettings} aria-label="Open settings"><Icon name="settings" /></button>
  </aside>;
}
