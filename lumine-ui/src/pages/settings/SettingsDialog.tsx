import { useState } from "react";
import { Icon } from "../home/components/Icon";
import { groupedSections, type SettingsSection } from "./SettingsNav";

type SettingsDialogProps = {
  initialSection?: SettingsSection;
  onClose: () => void;
  children: (section: SettingsSection) => React.ReactNode;
};

/**
 * The settings surface.
 *
 * A full-height overlay with its own left rail rather than a small modal: the AI
 * Control Center has more than one screen, and a modal that has to grow into a
 * router is worse than an overlay that never has to. This keeps the existing
 * single-state `Home.tsx` model and adds no router dependency.
 */
export function SettingsDialog({ initialSection = "appearance", onClose, children }: SettingsDialogProps) {
  const [section, setSection] = useState<SettingsSection>(initialSection);

  return (
    <div
      className="settings-scrim settings-scrim--wide"
      role="presentation"
      onMouseDown={onClose}
    >
      <section
        className="settings-shell"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <aside className="settings-rail">
          <header>
            <p className="eyebrow">Lumine</p>
            <h2 id="settings-title">Settings</h2>
          </header>
          <nav aria-label="Settings sections">
            {groupedSections().map((group) => (
              <div className="settings-rail-group" key={group.group}>
                <span className="settings-rail-label">{group.group}</span>
                {group.sections.map((entry) => (
                  <button
                    key={entry.id}
                    type="button"
                    className={section === entry.id ? "is-active" : ""}
                    aria-current={section === entry.id ? "page" : undefined}
                    onClick={() => setSection(entry.id)}
                  >
                    <Icon name={entry.icon} size={17} />
                    <span>{entry.label}</span>
                  </button>
                ))}
              </div>
            ))}
          </nav>
          <footer>
            <button type="button" className="settings-rail-close" onClick={onClose}>
              Close
            </button>
          </footer>
        </aside>
        <div className="settings-content">{children(section)}</div>
      </section>
    </div>
  );
}
