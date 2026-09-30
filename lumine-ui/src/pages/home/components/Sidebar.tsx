import { useEffect, useRef, useState } from "react";
import { NAV_ITEMS, PRIMARY_NAV_IDS, type NavId } from "../constants";
import type { IconName } from "../types";
import { Icon, Mark } from "./Icon";

/**
 * Persistent navigation rail for the desktop shell.
 *
 * `active` is a `NavId`, and Settings is deliberately not one. Settings is a
 * dialog over whatever page you were on, so there is no destination to mark and
 * nothing for a persistent `is-active` to mean — highlighting the rail while an
 * overlay is up would claim the page changed when it did not.
 *
 * ## Three shapes, one component
 *
 * The rail is wide by default, folds to icons on request, and becomes a thumb bar
 * on a phone. All three are this component rather than three, because the three
 * differ in *presentation only* — the same six destinations, the same handler, the
 * same active state — and a second copy is how the list and the router come to
 * disagree about what a destination is called.
 *
 * The glass switch is gone from here. It belongs to the stage's topbar, where a
 * presentation preference sits with the other presentation controls; a preference
 * that reads the same from every destination is also a preference nobody can find
 * on the destination it actually changes.
 *
 * The collapse control folds the rail rather than closing the page: the workspace
 * beside it is the content, and a rail that took the content with it would be a
 * close button wearing a different icon.
 */
export function Sidebar({
  active,
  onChange,
  onSettings,
  collapsed,
  onCollapsedChange,
}: {
  active: NavId;
  onChange: (id: NavId) => void;
  onSettings: () => void;
  /** Folded to icons. Controlled by the shell so it survives a route change. */
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // The menu is a popover with no focus trap of its own, so the dismissal is the
  // click that lands outside it. Without this it stays open over whatever the
  // reader navigated to, which is the one state a menu must never be in.
  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  const pick = (id: NavId) => {
    setMenuOpen(false);
    onChange(id);
  };

  const collapseLabel = collapsed ? "Expand the navigation" : "Collapse the navigation";

  return (
    <aside className={`sidebar${collapsed ? " is-collapsed" : ""}`}>
      <div className="sidebar-brand">
        <button className="logo-button" aria-label="Lumine home" onClick={() => onChange("home")}>
          <Mark />
        </button>
        <span>Lumine</span>
      </div>

      <nav aria-label="Main navigation" className="sidebar-nav">
        {NAV_ITEMS.map(([id, icon, label]) => (
          <button
            key={id}
            title={label}
            aria-label={label}
            // The wide rail was the only navigation in the app that did not say
            // where it was. Its compact twin at line 120 marks itself, so on
            // desktop — the platform this rail *is* — a screen reader was told
            // six indistinguishable links, and on a phone it was told one.
            aria-current={active === id ? "page" : undefined}
            className={`nav-button ${active === id ? "is-active" : ""}`}
            onClick={() => onChange(id)}
          >
            <Icon name={icon as IconName} />
            <span>{label}</span>
          </button>
        ))}
        <button
          className="nav-button settings-nav-button"
          title="Settings"
          onClick={onSettings}
          aria-label="Open settings"
        >
          <Icon name="settings" />
          <span>Settings</span>
        </button>
      </nav>

      {/*
        The phone bar. Same destinations, a different number of them: three stay
        visible and the rest are one tap away. Rendered only where the wide rail
        is not, so the two never both send focus to the same destination.
      */}
      <nav aria-label="Main navigation" className="sidebar-compact">
        {PRIMARY_NAV_IDS.map((id) => {
          const entry = NAV_ITEMS.find(([candidate]) => candidate === id);
          if (!entry) return null;
          const [, icon, label] = entry;
          return (
            <button
              key={id}
              className={`nav-button ${active === id ? "is-active" : ""}`}
              aria-label={label}
              aria-current={active === id ? "page" : undefined}
              onClick={() => onChange(id)}
            >
              <Icon name={icon as IconName} />
              <span>{label}</span>
            </button>
          );
        })}

        <div className="sidebar-more relative min-w-0" ref={menuRef}>
          <button
            className={`nav-button ${menuOpen ? "is-active" : ""}`}
            aria-label="More destinations"
            aria-expanded={menuOpen}
            aria-haspopup="menu"
            onClick={() => setMenuOpen((open) => !open)}
          >
            <Icon name="menu" />
            <span>More</span>
          </button>
          {menuOpen && (
            <div className="sidebar-menu" role="menu">
              {NAV_ITEMS.filter(([id]) => !(PRIMARY_NAV_IDS as readonly string[]).includes(id)).map(
                ([id, icon, label]) => (
                  <button
                    key={id}
                    role="menuitem"
                    className={active === id ? "is-active" : ""}
                    onClick={() => pick(id)}
                  >
                    <Icon name={icon as IconName} />
                    <span>{label}</span>
                  </button>
                ),
              )}
              <button role="menuitem" onClick={() => { setMenuOpen(false); onSettings(); }}>
                <Icon name="settings" />
                <span>Settings</span>
              </button>
            </div>
          )}
        </div>
      </nav>

      <div className="sidebar-foot w-full pt-3 mt-3 border-t border-t-border">
        <button
          type="button"
          className="sidebar-collapse"
          onClick={() => onCollapsedChange(!collapsed)}
          aria-pressed={collapsed}
          // One string for both. The label said "the navigation" and the tooltip
          // said "navigation", so hovering and hearing were two different
          // descriptions of one button — and the tooltip is what a sighted mouse
          // user reads while the label is what everyone else reads, which is the
          // wrong pair of sentences to keep apart.
          aria-label={collapseLabel}
          title={collapseLabel}
        >
          <Icon name={collapsed ? "expand" : "collapse"} size={16} />
          <span>Collapse</span>
        </button>
      </div>
    </aside>
  );
}
