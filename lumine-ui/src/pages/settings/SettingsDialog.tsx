import { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Icon } from "../home/components/Icon";
import { SETTINGS_SECTIONS, groupedSections, type SettingsSection } from "./SettingsNav";

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
 *
 * The rail is the only navigation in the app that lists where everything is, so
 * it carries each section's description rather than a bare label. Arrow keys move
 * between sections the way a list does, Escape leaves, and focus stays inside the
 * overlay while it is open.
 */
export function SettingsDialog({ initialSection = "appearance", onClose, children }: SettingsDialogProps) {
  const [section, setSection] = useState<SettingsSection>(initialSection);
  const shellRef = useRef<HTMLElement>(null);
  const railRef = useRef<HTMLDivElement>(null);
  const reduceMotion = useReducedMotion();
  const groups = groupedSections();
  const flat = groups.flatMap((group) => group.sections);
  const current = SETTINGS_SECTIONS.find((entry) => entry.id === section);

  // Focus the overlay itself on open so the first Tab lands inside the settings
  // rather than back out in the app behind the scrim.
  useEffect(() => {
    shellRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }

      if (!event.key.startsWith("Arrow") && event.key !== "Home" && event.key !== "End") return;

      // Only take the arrow keys when the rail has focus. A select or a colour
      // input inside the content pane needs them for itself.
      const railHasFocus = railRef.current?.contains(document.activeElement);
      if (!railHasFocus) return;

      event.preventDefault();
      const index = flat.findIndex((entry) => entry.id === section);
      const last = flat.length - 1;
      const next =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? last
            : event.key === "ArrowDown"
              ? Math.min(index + 1, last)
              : Math.max(index - 1, 0);
      setSection(flat[next].id);
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [flat, onClose, section]);

  // Follow the selection in the rail so arrow-key navigation and click agree.
  useEffect(() => {
    railRef.current?.querySelector<HTMLElement>(`[data-section="${section}"]`)?.focus();
  }, [section]);

  /** Keep Tab inside the overlay while it is open. */
  const trapFocus = useCallback((event: React.KeyboardEvent) => {
    if (event.key !== "Tab" || !shellRef.current) return;
    const focusable = shellRef.current.querySelectorAll<HTMLElement>(
      'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }, []);

  const transition = reduceMotion
    ? { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 } }
    : {
        initial: { opacity: 0, y: 8 },
        animate: { opacity: 1, y: 0 },
        exit: { opacity: 0, y: -6 },
      };

  return (
    <div className="settings-scrim settings-scrim--wide" role="presentation" onMouseDown={onClose}>
      <section
        className="settings-shell"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        aria-describedby="settings-summary"
        ref={shellRef}
        tabIndex={-1}
        onKeyDown={trapFocus}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <aside className="settings-rail">
          <header>
            <p className="eyebrow">Lumine</p>
            <h2 id="settings-title">Settings</h2>
            <p id="settings-summary" className="settings-rail-summary">
              {current?.description}
            </p>
          </header>

          <div className="settings-rail-scroll" ref={railRef}>
            <nav aria-label="Settings sections">
              {groups.map((group) => (
                <div className="settings-rail-group" key={group.group}>
                  <span className="settings-rail-label">{group.group}</span>
                  {group.sections.map((entry) => (
                    <button
                      key={entry.id}
                      type="button"
                      data-section={entry.id}
                      className={section === entry.id ? "is-active" : ""}
                      aria-current={section === entry.id ? "page" : undefined}
                      onClick={() => setSection(entry.id)}
                    >
                      <span className="settings-rail-icon">
                        <Icon name={entry.icon} size={16} />
                      </span>
                      <span className="settings-rail-copy">
                        <span className="settings-rail-name">{entry.label}</span>
                        <span className="settings-rail-note">{entry.description}</span>
                      </span>
                    </button>
                  ))}
                </div>
              ))}
            </nav>
          </div>

          <footer>
            <button type="button" className="settings-rail-close" onClick={onClose}>
              Close
              <kbd>Esc</kbd>
            </button>
          </footer>
        </aside>

        <div className="settings-content">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={section}
              className="settings-content-inner"
              initial={transition.initial}
              animate={transition.animate}
              exit={transition.exit}
              transition={{ duration: reduceMotion ? 0.12 : 0.22, ease: [0.22, 1, 0.36, 1] }}
            >
              {children(section)}
            </motion.div>
          </AnimatePresence>
        </div>
      </section>
    </div>
  );
}
