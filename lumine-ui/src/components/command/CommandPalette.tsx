import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Dialog } from "@base-ui/react/dialog";
// `ArrowBendDownLeft`, not `CornerDownLeft`: the glyph this package ships for an
// enter key is the bent arrow, and importing a name it does not export is a
// compile error rather than a missing icon at runtime.
import { ArrowBendDownLeft, ArrowCounterClockwise, CaretRight, Command as CommandGlyph, MagnifyingGlass } from "@phosphor-icons/react";
import { Icon } from "../../pages/home/components/Icon";
import { commandHaystack, type Command, type CommandGroup } from "./commands";
import { fuzzyFilter, highlightRuns } from "./fuzzy";
import { describeBinding } from "./useCommandHotkeys";

/**
 * The command palette.
 *
 * ## Why hand-rolled on base-ui rather than `cmdk`
 *
 * `cmdk` is the obvious dependency here and it is a good library. It is not used
 * for two reasons that have nothing to do with taste:
 *
 * - **It is not installed, and adding it for a search box is a decision that gets
 *   inherited.** Every future command surface would then reach for it too, and
 *   the matching behaviour of the whole app would be a property of a package
 *   version rather than of this repo.
 * - **The app already hand-rolls its composites on base-ui.** `tabstrip.tsx` and
 *   `disclosure.tsx` are both ours, both small, and both exist because base-ui has
 *   the primitive and not the keyboard model. This is the same trade a third
 *   time, and the matching logic itself is already in `fuzzy.ts` where it can be
 *   read.
 *
 * The consequence is that the *accessibility* work is not skimped. This is a
 * combobox driving a listbox: `aria-activedescendant` rather than roving focus,
 * `aria-expanded`, `aria-controls`, live-region result counts, and a `<form
 * role="search">` so a screen reader announces the field correctly. A palette
 * nobody can drive by voice is a palette that is only half a keyboard surface.
 *
 * ## Why it renders a list rather than a menu
 *
 * `role="menu"` would be wrong: these are not actions on the currently focused
 * object, they are a *search over available destinations*, and the difference is
 * what a screen reader announces on open. Listbox is right, and it is why
 * filtering and selection are separate concerns here.
 */

/** Groups, in the order they appear. Never derived from the data. */
const GROUP_ORDER: readonly CommandGroup[] = ["Call", "Navigate", "Appearance", "Session", "App"];

export type CommandPaletteProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  commands: readonly Command[];
  /** Placeholder, so the palette can be specialised without forking the component. */
  placeholder?: string;
  /** Text announced to assistive tech, and rendered as the dialog's label. */
  label?: string;
  /**
   * Called when Enter lands on a row that cannot run.
   *
   * The keyboard path has always had this; Enter in the palette did not, so the
   * same command produced two answers depending on how it was reached — a toast
   * over a chord, silence over a click. The row stays open and highlighted so
   * the refusal lands on the thing that was refused.
   */
  onBlocked?: (command: Command) => void;
};

export function CommandPalette({ open, onOpenChange, commands, placeholder = "Type a command…", label = "Command palette", onBlocked }: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const activeRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const inputId = useId();

  const results = useMemo(() => fuzzyFilter(query, commands, commandHaystack), [query, commands]);

  /**
   * Grouped, and each group's order fixed by `GROUP_ORDER`.
   *
   * The groups are declared rather than inferred from first appearance, because
   * a ranked result list is *not* in group order — the top hit for "s" might be
   * a navigation command and the second might be a call control. Sorting by score
   * and then bucketing is the only way the sections stay put while the ranking
   * still means something. A palette whose sections reshuffle as you type reads
   * as a list that cannot decide what it is showing.
   */
  const sections = useMemo(() => {
    const byGroup = new Map<CommandGroup, typeof results>();
    for (const group of GROUP_ORDER) byGroup.set(group, []);
    for (const result of results) {
      byGroup.get(result.item.group)?.push(result);
    }
    return GROUP_ORDER.map((group) => ({ group, results: byGroup.get(group) ?? [] })).filter((section) => section.results.length > 0);
  }, [results]);

  /** Flat position → command, so arrow keys move through sections without caring. */
  const ordered = useMemo(() => sections.flatMap((section) => section.results), [sections]);

  /**
   * Every open resets the query.
   *
   * A palette that remembers its last query looks like a search box with a
   * mind of its own: reopening it shows a filtered list of a query the user
   * cannot see the origin of, and the top hit is somebody else's typo. Opening
   * always shows everything, in group order.
   */
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
  }, [open]);

  // Clamp rather than reset, so a query that narrows the list while the user is
  // arrowing through it lands somewhere legal instead of on a stale index that no
  // longer exists. Resetting to 0 would be simpler and worse: the list would jump
  // under the arrow key.
  useEffect(() => {
    if (active >= ordered.length) setActive(Math.max(0, ordered.length - 1));
  }, [active, ordered.length]);

  useEffect(() => {
    if (!open) return;
    activeRef.current?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const close = useCallback(() => onOpenChange(false), [onOpenChange]);

  const runActive = useCallback(() => {
    const chosen = ordered[active];
    if (!chosen) return;
    /* Decided before the close, because a command that cannot run should not
       take the palette with it: the row stays where it was, the caller says
       why, and the user can arrow to something that works. The chord path has
       always behaved this way — Enter was the one route that swallowed the
       refusal, closed, and left the screen looking like it had worked. */
    if (chosen.item.available === false) {
      onBlocked?.(chosen.item);
      return;
    }
    // Closed first. A command that opens a dialog would otherwise leave two
    // modals stacked, and the palette's own focus trap would fight the new one's
    // for the keyboard.
    close();
    chosen.item.run();
  }, [active, close, onBlocked, ordered]);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLInputElement>) => {
      if (ordered.length === 0 && event.key !== "Escape") return;
      switch (event.key) {
        case "ArrowDown":
          event.preventDefault();
          // Wraps, because a palette that stops at the last row makes the list
          // feel like a dead end and hides the wrap-around affordance entirely.
          setActive((current) => (current + 1) % ordered.length);
          break;
        case "ArrowUp":
          event.preventDefault();
          setActive((current) => (current - 1 + ordered.length) % ordered.length);
          break;
        case "Home":
          event.preventDefault();
          setActive(0);
          break;
        case "End":
          event.preventDefault();
          setActive(ordered.length - 1);
          break;
        case "Enter":
          event.preventDefault();
          runActive();
          break;
        case "Escape":
          // Handled by the dialog as well, but only when the palette is the only
          // thing that can close. A `⌘K`-toggled palette opened from inside the
          // settings overlay closes the *overlay* first, which is what the user
          // pressing Escape inside a nested dialog expects.
          event.preventDefault();
          event.stopPropagation();
          close();
          break;
        default:
          break;
      }
    },
    [close, ordered.length, runActive],
  );

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange} modal="trap-focus" disablePointerDismissal={false}>
      <Dialog.Portal>
        <Dialog.Backdrop
          className={
            // The same scrim the settings overlay uses, so a dialog opened from
            // either route dims the app identically. Hard-coded rather than
            // tokenised because a translucent *black* scrim is what a warm light
            // palette needs to push the panel forward, and a token would be
            // re-tinted per theme and end up a grey veil in one of them.
            "fixed inset-0 z-40 bg-[rgba(24,21,18,0.46)] backdrop-blur-[10px] " +
            "transition-opacity duration-150 data-[ending-style]:opacity-0"
          }
        />
        <Dialog.Popup
          // Focus goes straight to the field, not to the first row. A palette
          // that opens with the list focused still needs two keystrokes to start
          // searching, and the whole point of the surface is that one starts
          // typing.
          initialFocus={inputRef}
          className={
            // A centred, short panel rather than a tall one. The palette is a
            // momentary surface: it should cover the minimum that makes the list
            // readable, because anything it covers is content the user was
            // looking at when they summoned it.
            "fixed left-1/2 top-[14vh] z-50 w-[min(640px,calc(100vw-2rem))] -translate-x-1/2 " +
            "overflow-hidden rounded-[var(--radius-md)] bg-popover text-popover-foreground shadow-elev-3 " +
            "transition-[opacity,transform] duration-150 ease-out " +
            "data-[starting-style]:opacity-0 data-[starting-style]:-translate-y-1 data-[starting-style]:scale-[0.98] " +
            "data-[ending-style]:opacity-0 data-[ending-style]:scale-[0.98]"
          }
        >
          <Dialog.Title className="sr-only">{label}</Dialog.Title>

          <form
            role="search"
            onSubmit={(event) => {
              event.preventDefault();
              runActive();
            }}
            className="flex items-center gap-2.5 border-b border-[var(--color-border)] px-4"
          >
            <MagnifyingGlass size={16} weight="regular" className="shrink-0 text-faint" aria-hidden="true" />
            <label htmlFor={inputId} className="sr-only">
              {label}
            </label>
            <input
              id={inputId}
              ref={inputRef}
              // `aria-expanded` and `aria-controls` are what make this a combobox
              // rather than a plain field, and they are asserted by the
              // verify-command-palette script along with `aria-activedescendant`.
              role="combobox"
              aria-expanded={ordered.length > 0}
              aria-controls={listId}
              aria-activedescendant={ordered.length > 0 ? `command-${ordered[active]?.item.id}` : undefined}
              aria-autocomplete="list"
              autoComplete="off"
              spellCheck={false}
              value={query}
              placeholder={placeholder}
              onChange={(event) => {
                setQuery(event.target.value);
                // Always to the top: a narrowed list is a new list, and keeping
                // the old index would leave the highlight on a different row than
                // the first one the user is looking at.
                setActive(0);
              }}
              onKeyDown={onKeyDown}
              className="h-12 w-full bg-transparent text-[14px] text-popover-foreground outline-none placeholder:text-faint"
            />
            {query.length > 0 && (
              <button
                type="button"
                aria-label="Clear search"
                onClick={() => {
                  setQuery("");
                  setActive(0);
                  inputRef.current?.focus();
                }}
                className="shrink-0 rounded-[4px] p-1 text-faint outline-none transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent"
              >
                <ArrowCounterClockwise size={14} aria-hidden="true" />
              </button>
            )}
          </form>

          <div
            ref={listRef}
            id={listId}
            role="listbox"
            aria-label="Commands"
            className="max-h-[min(360px,44vh)] overflow-y-auto overscroll-contain p-1.5"
          >
            {ordered.length === 0 ? (
              <EmptyState query={query} />
            ) : (
              sections.map((section) => (
                <div key={section.group} role="group" aria-label={section.group}>
                  <div className="px-2.5 pb-1 pt-2 text-[10px] font-medium uppercase tracking-[0.07em] text-faint first:pt-1">
                    {section.group}
                  </div>
                  {section.results.map((result) => {
                    const position = ordered.indexOf(result);
                    const selected = position === active;
                    const unavailable = result.item.available === false;
                    return (
                      <div
                        // `id` here is what `aria-activedescendant` points at, so
                        // the DOM id and the accessibility id cannot drift apart.
                        id={`command-${result.item.id}`}
                        key={result.item.id}
                        ref={selected ? activeRef : undefined}
                        role="option"
                        aria-selected={selected}
                        aria-disabled={unavailable}
                        // `onMouseMove` rather than `onMouseOver`: moving the
                        // pointer *within* a row must not re-fire, and `over`
                        // plus a hover-highlight makes the highlight flicker
                        // between the two sub-elements a row is built from.
                        onMouseMove={() => setActive(position)}
                        onClick={() => {
                          setActive(position);
                          close();
                          if (!unavailable) result.item.run();
                        }}
                        className={
                          "flex cursor-default items-center gap-3 rounded-[6px] px-2.5 py-2 outline-none " +
                          (selected ? "bg-accent/12" : "") +
                          (unavailable ? " opacity-45" : "")
                        }
                      >
                        <span
                          className={
                            "flex size-7 shrink-0 items-center justify-center rounded-[6px] " +
                            (selected ? "text-accent" : "text-faint")
                          }
                        >
                          <Icon name={result.item.icon} size={15} />
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-[13.5px] text-popover-foreground">
                            <Marked text={result.item.label} indices={result.indices} />
                          </span>
                          {(result.item.hint || result.item.unavailableReason) && (
                            <span className="mt-0.5 block truncate text-[11.5px] leading-snug text-faint">
                              {result.item.unavailableReason ?? result.item.hint}
                            </span>
                          )}
                        </span>
                        {result.item.shortcut && (
                          <kbd className="shrink-0 rounded-[4px] bg-surface-muted px-1.5 py-0.5 text-[10.5px] font-medium text-faint shadow-elev-1">
                            {result.item.shortcut}
                          </kbd>
                        )}
                      </div>
                    );
                  })}
                </div>
              ))
            )}
          </div>

          {/* The status line is not decoration. Without a result count announced
              politely, a screen-reader user types three characters and is told
              nothing at all — the list changed and no one said so. */}
          <p aria-live="polite" aria-atomic="true" className="sr-only">
            {ordered.length === 0
              ? `No commands match ${query}`
              : `${ordered.length} ${ordered.length === 1 ? "command" : "commands"} available. ${ordered[active]?.item.label ?? ""} highlighted.`}
          </p>

          <Footer />
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * Renders a label with the matched characters marked.
 *
 * `Marked` splits on normalised indices and maps back, so the marks land on the
 * characters that actually matched. A single `<mark>` per command rather than
 * per character: the run-splitting is what makes a five-character match look
 * deliberate, and per-character marks give a striped, noisy label.
 */
function Marked({ text, indices }: { text: string; indices: readonly number[] }) {
  return (
    <>
      {highlightRuns(text, indices).map((run, index) =>
        run.match ? (
          <mark key={index} className="bg-transparent font-semibold text-accent">
            {run.text}
          </mark>
        ) : (
          <span key={index}>{run.text}</span>
        ),
      )}
    </>
  );
}

/**
 * What the palette says when nothing matched.
 *
 * It names the query, because "No results" is a message about the *app* and this
 * is a message about the *search* — and showing what was searched for is the only
 * way to notice that the field lost a character.
 */
function EmptyState({ query }: { query: string }) {
  return (
    <div className="flex flex-col items-center gap-1.5 px-4 py-9 text-center">
      <CommandGlyph size={20} className="text-faint" aria-hidden="true" />
      <p className="text-[13px] text-foreground">No command matches “{query.trim()}”</p>
      <p className="text-[11.5px] text-faint">Try fewer characters — the palette matches letters in order, not just prefixes.</p>
    </div>
  );
}

/**
 * The footer, which is the palette's teaching surface.
 *
 * A palette nobody knows about is a palette nobody opens, so the shortcuts are
 * printed where the user is already looking. This is also the only place in the
 * app that states the `mod` convention, which is otherwise invisible to anyone
 * who has not used a Mac.
 */
function Footer() {
  return (
    <div className="flex items-center justify-between gap-3 border-t border-[var(--color-border)] bg-surface-muted/60 px-4 py-2 text-[11px] text-faint">
      <span className="flex items-center gap-3">
        <span className="flex items-center gap-1">
          <CaretRight size={10} weight="bold" aria-hidden="true" />
          <span>move</span>
        </span>
        <span className="flex items-center gap-1">
          <ArrowBendDownLeft size={10} weight="bold" aria-hidden="true" />
          <span>run</span>
        </span>
        <span className="flex items-center gap-1">
          <kbd className="rounded-[3px] bg-popover px-1 py-px shadow-elev-1">esc</kbd>
          <span>close</span>
        </span>
      </span>
      <span className="truncate">
        {/* Resolved, never a literal. `describeBinding` is exactly the function
            whose doc says "writing ⌘K as a literal is the bug this exists to
            prevent" — and the footer was the one place that did it, so every
            Windows and Linux machine was shown a chord it does not have. */}
        <kbd className="rounded-[3px] bg-popover px-1 py-px shadow-elev-1">{describeBinding("mod+k")}</kbd> anywhere
      </span>
    </div>
  );
}
