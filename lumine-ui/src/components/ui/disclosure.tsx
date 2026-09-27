import { useId, useState } from "react";
import { CaretRight } from "@phosphor-icons/react";

/**
 * A group of controls that is out of the way until asked for.
 *
 * ## Why this exists
 *
 * A stage lists every setting the selected model accepts, and the list is longer
 * than the stage is interesting. Speed, voice and thinking level are what a
 * person comes to change. Temperature, a token cap and a top-p are not — they are
 * correct at the provider's default and wrong in ways that are hard to trace back
 * to a slider, so leaving them on the first screen invites exactly the mistake
 * that hides them.
 *
 * ## The count
 *
 * A disclosure that silently contains a value somebody set is a value they cannot
 * find again. So the trigger says how many are set, and it is a real count rather
 * than a dot: "3 set" is answerable, "something is in here" is not. Which options
 * count as set is decided by the caller, because only the caller knows whether
 * this is about a profile, a palette or a model.
 */
export type DisclosureProps = {
  /** The trigger's text. A noun phrase, not a sentence. */
  label: string;
  /** How many things inside are currently set. Shown as a badge. */
  count?: number;
  /** Start open. For a disclosure whose contents are the reason the page exists. */
  defaultOpen?: boolean;
  /**
   * Controls the panel instead of `defaultOpen`.
   *
   * Needed when something *inside* can fail and the user has to be shown the
   * failure. `defaultOpen` is read once, so a disclosure that closed, then
   * reported "that JSON is missing accent", would keep its failure to itself —
   * which is the same class of bug as the count badge, and the reason a
   * disclosure that validates its own contents cannot be uncontrolled.
   */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  children: React.ReactNode;
  className?: string;
};

export function Disclosure({ label, count = 0, defaultOpen = false, open: controlledOpen, onOpenChange, children, className }: DisclosureProps) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);
  const open = controlledOpen ?? uncontrolledOpen;
  const panelId = useId();
  const set = count > 0;

  const toggle = () => {
    const next = !open;
    if (controlledOpen === undefined) setUncontrolledOpen(next);
    onOpenChange?.(next);
  };

  return (
    <div className={className}>
      <button
        type="button"
        className="group flex w-full items-center gap-2 rounded-sm bg-surface-muted px-2.5 py-2 text-left shadow-elev-1 transition-shadow hover:shadow-elev-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={toggle}
      >
        {/* Rotation, not a swap between two glyphs: the caret is the same mark
            pointing the way the panel will move. */}
        <CaretRight
          size={12}
          weight="bold"
          aria-hidden="true"
          className="shrink-0 text-faint transition-transform duration-150 group-aria-expanded:rotate-90"
        />
        <span className="flex-1 text-[12.5px] font-medium text-soft group-hover:text-foreground">{label}</span>
        {set && (
          <span className="rounded-[6px] bg-accent/12 px-1.5 py-0.5 text-[10.5px] font-medium text-accent">
            {count} set
          </span>
        )}
      </button>
      {/* Hidden rather than unmounted, so an in-progress edit survives a
          re-render from the parent and the browser restores the fields on
          reopen. */}
      <div id={panelId} hidden={!open} className="mt-2.5 flex flex-col gap-3.5">
        {children}
      </div>
    </div>
  );
}
