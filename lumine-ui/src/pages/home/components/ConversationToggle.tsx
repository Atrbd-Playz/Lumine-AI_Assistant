import { Icon } from "./Icon";
import type { IconName } from "../types";

/**
 * The stage topbar's panel switches.
 *
 * Two buttons of exactly one shape, because they do exactly one thing each: they
 * put a right-hand aside on the screen or take it off. Rendering them as one
 * component with a glyph and a name rather than two components keeps the pressed
 * state, the accessible name and the accent wash in one place — three things that
 * drift the first time somebody restyles one of the pair.
 *
 * `aria-expanded` and `aria-controls` are the contract, not decoration: a toggle
 * that reports neither is a button whose effect a screen reader cannot follow,
 * and these two are the only way to reach either surface without the pointer.
 */
export function StageToggle({
  name,
  icon,
  label,
  open,
  onClick,
}: {
  /** The `id` of the surface it controls, for `aria-controls`. */
  name: string;
  icon: IconName;
  /** The surface's name, in the user's words. Used for the accessible label. */
  label: string;
  open: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={`stage-toggle${open ? " is-open" : ""}`}
      onClick={onClick}
      aria-expanded={open}
      aria-controls={name}
      aria-label={open ? `Hide ${label}` : `Show ${label}`}
      title={open ? `Hide ${label}` : `Show ${label}`}
    >
      <Icon name={icon} size={17} />
    </button>
  );
}

/** The transcript's switch. Kept under its own name because callers read it as one. */
export function ConversationToggle({ open, onClick }: { open: boolean; onClick: () => void }) {
  return <StageToggle name="conversation-panel" icon="chat" label="the transcript" open={open} onClick={onClick} />;
}

/** The notes switch, and the twin of the one above. */
export function NotesToggle({ open, onClick }: { open: boolean; onClick: () => void }) {
  return <StageToggle name="notes-panel" icon="note" label="notes" open={open} onClick={onClick} />;
}
