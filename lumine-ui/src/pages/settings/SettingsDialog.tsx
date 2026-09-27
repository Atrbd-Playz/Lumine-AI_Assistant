import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Icon } from "../home/components/Icon";
import { TabStrip } from "../../components/ui/tabstrip";
import {
  findSection,
  groupedSections,
  normaliseRoute,
  type SettingsRoute,
  type SettingsSection,
} from "./SettingsNav";

type SettingsDialogProps = {
  initialSection?: SettingsSection;
  /** Which sub-tab to open on. Normalised against the section's own tabs. */
  initialTab?: string | null;
  onClose: () => void;
  children: (route: SettingsRoute) => React.ReactNode;
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
 * it carries each section's description rather than a bare label. A section with
 * sub-tabs grows a second strip above its content; the rail then describes the
 * section and the strip separates its parts, which is two levels of navigation
 * doing two different jobs rather than one trying to do both.
 *
 * Arrow keys move between sections and tabs the way a list does, Escape leaves,
 * and focus stays inside the overlay while it is open.
 */
export function SettingsDialog({
  initialSection = "appearance",
  initialTab = null,
  onClose,
  children,
}: SettingsDialogProps) {
  // One piece of state, not two. A section and its tab are never set
  // independently, and keeping them apart is what allowed a section to be
  // showing a tab id it does not have.
  const [route, setRoute] = useState<SettingsRoute>(() => normaliseRoute(initialSection, initialTab));
  const shellRef = useRef<HTMLElement>(null);
  const railRef = useRef<HTMLDivElement>(null);
  const tabRef = useRef<HTMLDivElement>(null);
  const reduceMotion = useReducedMotion();
  const groups = groupedSections();
  const flat = groups.flatMap((group) => group.sections);
  const { section, tab } = route;
  const current = findSection(section);
  const tabs = useMemo(() => current?.tabs ?? [], [current]);

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

      // Only the rail's keys are handled here. The tab strip owns its own
      // arrows, and it used to have a second copy of this handler that fired
      // alongside this one — so a single ArrowRight moved two tabs, once for
      // each listener, and stopped being noticeable only because both landings
      // looked like "the tab changed".
      //
      // The guard is also why a select or a colour input inside the content pane
      // keeps the arrow keys for itself: focus has to be in the rail.
      if (!railRef.current?.contains(document.activeElement)) return;

      // The rail is a vertical list, so it takes the vertical keys and nothing
      // else.
      //
      // It used to fall through to `index - 1` for anything that was not Down,
      // which meant ArrowRight moved *up* the rail. It read as a shortcut, and it
      // was one — the wrong one, in a list with no horizontal axis, on a key that
      // a screen reader or a text field further along the overlay may still want.
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "Home" && event.key !== "End") return;

      const list = flat.map((entry) => entry.id);
      const index = list.indexOf(section);
      const last = list.length - 1;
      if (index < 0) return;
      const next =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? last
            : event.key === "ArrowDown"
              ? Math.min(index + 1, last)
              : Math.max(index - 1, 0);

      event.preventDefault();
      setRoute(normaliseRoute(list[next] as SettingsSection, null));
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [flat, onClose, section]);

  // Follow the selection, so arrow-key navigation and click agree.
  //
  // Each level only claims focus from within itself. That matters most for the
  // rail: stepping down to a section that has a stage strip used to dump focus into
  // the strip, and because the strip owns the arrow keys, the next ArrowUp went
  // nowhere and the rail became unreachable without a click. Two levels of
  // navigation are two places to *be*, and following the selection must not move
  // you between them.
  useEffect(() => {
    if (railRef.current?.contains(document.activeElement)) {
      railRef.current?.querySelector<HTMLElement>(`[data-section="${section}"]`)?.focus();
    }
    if (tab && tabRef.current?.contains(document.activeElement)) {
      tabRef.current?.querySelector<HTMLElement>(`[data-tab="${tab}"]`)?.focus();
    }
  }, [section, tab]);

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

    // The `animate` frame is the one the pane rests at, so `opacity` there is 1 in
  // both branches.
  //
  // It was 0 in the motion branch only, and that is the branch almost everybody
  // gets: it is what runs when `prefers-reduced-motion` is *not* set. The
  // reduced-motion path — the one you only reach by turning motion off, or by
  // having already noticed the screen was blank — was correct the whole time.
  // Which is why this read as "settings shows the tabs and nothing else" rather
  // than "settings is broken": the rail, the header and the tab strip all sit
  // outside this element, so they drew normally, and only the content pane sat
  // at zero opacity. A screen showing its own navigation and no content reads as
  // a rendering fault, which is exactly what it was.
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
                      onClick={() => setRoute(normaliseRoute(entry.id, null))}
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
          {tabs.length > 0 && (
            <TabStrip
              ref={tabRef}
              items={tabs}
              value={tab ?? tabs[0].id}
              onChange={(next) => setRoute(normaliseRoute(section, next))}
              label={`${current?.label} stages`}
            />
          )}

          <div className="settings-content-scroll">
            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                // Tab switches animate as well as section switches. The key is the
                // whole route, so a tab with the same name in another section
                // still counts as a move.
                key={`${section}:${tab ?? ""}`}
                className="settings-content-inner"
                initial={transition.initial}
                animate={transition.animate}
                exit={transition.exit}
                transition={{ duration: reduceMotion ? 0.12 : 0.22, ease: [0.22, 1, 0.36, 1] }}
              >
                {children(route)}
              </motion.div>
            </AnimatePresence>
          </div>
        </div>
      </section>
    </div>
  );
}
