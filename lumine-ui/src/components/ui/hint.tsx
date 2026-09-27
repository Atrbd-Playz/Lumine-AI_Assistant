import { Tooltip } from "@base-ui/react/tooltip";

/**
 * A short explanation, hidden until asked for.
 *
 * ## Why this exists
 *
 * The settings screens grew paragraphs under every control. They were accurate
 * and they were unreadable: six of them on one screen is a wall, and a wall is
 * something people learn to skip, which means the one line that *would* have
 * saved them gets skipped too. So the prose moved here, behind a `?`, and the
 * control says only what it is.
 *
 * ## Why the text is still in the DOM
 *
 * A tooltip is not a place to put the only copy of something. It is invisible to
 * touch, it does not survive a screenshot, and it cannot be read by a screen
 * reader that does not fire hover events. So this is a *second* copy of
 * information that is already stated somewhere the user can reach — the
 * control's own accessible name, the value beside it, or the page. Where the
 * tooltip carries a caveat that exists nowhere else, the caveat is also rendered
 * inline; this component is for the part that repeats.
 *
 * ## Accessibility
 *
 * The trigger is a real `<button>`, so it is reachable by Tab and opens on focus
 * as well as hover. Escape closes it (Base UI's behaviour), and it carries
 * `aria-describedby` for the text. `?` is an abbreviation, not an icon, so the
 * trigger is labelled for assistive technology regardless of what glyph shows.
 */
export type HintProps = {
  /** The explanation. One or two short sentences. */
  children: React.ReactNode;
  /** Where the popup sits relative to the trigger. */
  side?: "top" | "right" | "bottom" | "left";
  /** Overrides the accessible label when the tooltip text is not a label. */
  label?: string;
  className?: string;
};

export function Hint({ children, side = "top", label, className }: HintProps) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger
        className={
          "inline-flex size-[18px] shrink-0 items-center justify-center rounded-full " +
          "text-[11px] font-medium leading-none text-faint " +
          "transition-colors hover:text-soft focus-visible:text-accent " +
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        }
        aria-label={label ?? "More about this"}
      >
        ?
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Positioner
          side={side}
          sideOffset={8}
          // Long help can run off the right edge of a 800px window otherwise, and
          // base-ui would happily position it where nobody can read it.
          align="center"
          className="z-50 max-w-[19rem] transition-[opacity,transform] duration-150"
        >
          <Tooltip.Popup
            className={
              // Depth 3: the popup floats above every surface, which is the one
              // place a border would be honest but still unnecessary.
              `rounded-sm bg-popover px-3 py-2 text-[12.5px] leading-snug text-popover-foreground shadow-elev-3 ${className ?? ""}`
            }
          >
            {children}
          </Tooltip.Popup>
        </Tooltip.Positioner>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

/**
 * Mount once, near the root.
 *
 * The delay is shared so a row of hints does not strobe when the pointer passes
 * over it, and so the *second* tooltip in a row opens instantly once the first
 * has shown — which is how people actually read a group of them.
 */
export function HintProvider({ children }: { children: React.ReactNode }) {
  return (
    <Tooltip.Provider delay={250} closeDelay={80} timeout={500}>
      {children}
    </Tooltip.Provider>
  );
}
