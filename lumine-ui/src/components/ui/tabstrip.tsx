import { useCallback, useEffect, useRef } from "react";
import { Hint } from "./hint";

/**
 * Point one ref at a node from two places.
 *
 * The strip needs its own ref for the arrow-key handler, and an overlay may need
 * the same node for a focus trap. Writing to both by hand means a handler that
 * assigns one and forgets the other, which fails as "focus escaped" rather than as
 * anything pointing at this function.
 *
 * Memoised on the external ref, because a new function identity every render
 * makes React call `ref(null)` and then `ref(node)` on every single render, which
 * is invisible until it is a dropped focus frame somewhere else.
 */
function useCombinedRef<T>(own: React.RefObject<T | null>, external: React.Ref<T> | undefined) {
  return useCallback(
    (node: T | null) => {
      (own as React.MutableRefObject<T | null>).current = node;
      if (typeof external === "function") external(node);
      else if (external) (external as React.MutableRefObject<T | null>).current = node;
    },
    [own, external],
  );
}

/**
 * One row of tabs that choose between sub-screens of a single page.
 *
 * ## Why this is a component and not markup repeated twice
 *
 * The Tools page wanted the same thing Settings already had: a strip across the
 * top that splits one page into its parts, rather than a page where everything is
 * stacked and you scroll to find it. Written separately the two would have
 * drifted within a release — one gaining a count badge, one gaining a different
 * selected treatment, one of them getting arrow keys and the other not — and the
 * drift is invisible until somebody notices two identical-looking controls
 * behaving differently.
 *
 * So the strip lives here, and the two callers describe their own tabs.
 *
 * ## The rules it keeps
 *
 * - **A selected tab is drawn by its fill, never an outline.** An outlined tab
 *   reads as focused rather than chosen, and the two states get confused the
 *   moment both are on screen.
 * - **One `Hint` for the whole strip, not one per tab.** A tooltip trigger is a
 *   `<button>`, and a button inside a tab button is invalid HTML *and* a click
 *   that fires twice. The open tab's help is the one worth reading, so that is
 *   what the single hint carries.
 * - **Arrow keys move between tabs, and only the selected tab is in the tab
 *   order.** Roving tabindex is what makes a `tablist` a `tablist` rather than a
 *   row of buttons that happen to look like tabs; without it a keyboard user tabs
 *   past the whole strip to reach the content, which is the thing the strip was
 *   added to avoid.
 * - **Tabs are never rendered for a page with nothing to split.** One tab is a
 *   label, and a label where a control should be teaches people to look for
 *   something that is not there.
 */
export type TabStripItem = {
  id: string;
  label: string;
  /** Optional trailing count, e.g. how many tools are in the group. */
  count?: number;
  /** Only the open tab's help is read; the rest is not needed. */
  help?: string;
};

export type TabStripProps = {
  items: TabStripItem[];
  value: string;
  onChange: (id: string) => void;
  /** Names the group for assistive technology. */
  label: string;
  /** Which side the single hint sits on. */
  hintSide?: "top" | "bottom";
  /**
   * Extra class on the strip, for a caller that lays it out differently. The
   * defaults match the settings overlay; the Tools page passes nothing and gets
   * the same control at its own width.
   */
  className?: string;
  /**
   * The `tablist` element, for a caller that needs it.
   *
   * The settings overlay keeps the overlay's focus inside itself and needs to know
   * whether focus is in the strip, which is one `contains` call it cannot make
   * without a handle. A forward ref is cheaper than re-querying by class name,
   * and re-querying by class name is the kind of thing that breaks silently the
   * day the class is renamed.
   */
  ref?: React.Ref<HTMLDivElement>;
};

export function TabStrip({ items, value, onChange, label, hintSide = "bottom", className, ref }: TabStripProps) {
  const ownRef = useRef<HTMLDivElement>(null);
  const assignStrip = useCombinedRef(ownRef, ref);

  // A strip whose selection does not match any tab renders as "nothing is
  // selected", which reads as a broken control. The caller should not be able to
  // get there, but the tab list arrives from Python and the route arrives from
  // navigation state, so the two can disagree at a boundary. Landing on the first
  // tab is the honest recovery.
  const selected = items.some((item) => item.id === value) ? value : items[0]?.id;
  const current = items.find((item) => item.id === selected);

  useEffect(() => {
    // `ownRef`, not the merged ref: this only needs to *read* the node, and the
    // merged value is an assignment function.
    const strip = ownRef.current;
    if (!strip) return;
    const onKeyDown = (event: KeyboardEvent) => {
      const index = items.findIndex((item) => item.id === selected);
      if (index < 0) return;
      const last = items.length - 1;
      // Home and End are the tablist convention, and they are the only keys that
      // can mean "first" or "last" unambiguously. The arrows wrap rather than
      // stop: a strip that dead-ends makes people keep pressing, and the strip is
      // short enough that wrapping costs nothing.
      let next: number | undefined;
      if (event.key === "Home") next = 0;
      else if (event.key === "End") next = last;
      else if (event.key === "ArrowRight") next = (index + 1) % items.length;
      else if (event.key === "ArrowLeft") next = (index - 1 + items.length) % items.length;
      if (next === undefined) return;
      const target = items[next];
      if (!target) return;
      event.preventDefault();
      event.stopPropagation();
      onChange(target.id);
      // Focus follows selection, which is the whole point of a roving tabindex:
      // arrow keys without focus movement leave the keyboard user guessing which
      // tab they are now on.
      // `CSS.escape`, because this value goes into a selector. A tab id with a
      // quote, a `]` or a comma in it ends the attribute term early and the
      // parse throws — inside a focus effect, where nothing catches it.
      strip.querySelector<HTMLButtonElement>(`[data-tab="${CSS.escape(target.id)}"]`)?.focus();
    };
    strip.addEventListener("keydown", onKeyDown);
    return () => strip.removeEventListener("keydown", onKeyDown);
  }, [items, selected, onChange]);

  if (items.length < 2) return null;

  return (
    <div className={"settings-tabs-bar" + (className ? ` ${className}` : "")}>
      <div className="settings-tabs" ref={assignStrip} role="tablist" aria-label={label}>
        {items.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            data-tab={item.id}
            className={item.id === selected ? "is-active" : ""}
            aria-selected={item.id === selected}
            tabIndex={item.id === selected ? 0 : -1}
            onClick={() => onChange(item.id)}
          >
            {item.label}
            {typeof item.count === "number" && <span className="tab-count">{item.count}</span>}
          </button>
        ))}
      </div>
      {current?.help && <Hint side={hintSide}>{current.help}</Hint>}
    </div>
  );
}
