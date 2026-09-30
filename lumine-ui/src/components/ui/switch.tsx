import { useId } from "react";

import { Hint } from "./hint";

/**
 * An on/off setting, in both directions.
 *
 * ## Why this is its own control
 *
 * A boolean used to fall through to the generic text box, which meant writing
 * `true` into a field. That only works if you already know the field wants
 * `true` — and it is the one setting in the product where a typo is not
 * out-of-range but simply *the other setting*. Nothing rejects it: the field
 * accepts `ture`, the worker reads it as neither true nor false, and Lumine does
 * the other thing forever.
 *
 * ## This is not a third picker
 *
 * The settings screen has one chooser, `dropdown.tsx`, and this is not one. A
 * picker asks *which of several*; this asks *whether*. Two rows in a dropdown
 * would be a picker doing a boolean's job, and the sidebar, the appearance
 * dialog and the Avatar Lab already draw switches for exactly this.
 *
 * ## Unset still means "the provider decides"
 *
 * The catalog may declare a `default`, which is the value the worker sends when
 * nothing is saved — so an unset switch has an honest state to display, and it
 * is the default rather than an invented one. Saving writes the explicit value,
 * because there is no third position worth the room: a toggle is on or off, and
 * a dashed middle state would be something to learn before it can be used.
 */
export type SwitchProps = {
  /** The setting's display name, already humanised. */
  label: string;
  /** `"true"` / `"false"` / `""` when the profile saved nothing. */
  value: string;
  onChange: (value: string) => void;
  /** What Lumine does when nothing is saved. The catalog's own default. */
  fallback?: boolean;
  /** The explanation behind the `?`. */
  help?: string;
};

/** What a boolean can be written as, matching the worker's own reading. */
const TRUEY = /^(true|1|yes|on)$/i;
const FALSEY = /^(false|0|no|off)$/i;

export function Switch({ label, value, onChange, fallback = false, help }: SwitchProps) {
  const id = useId();
  const trimmed = value.trim();
  const parsed = trimmed === "" ? null : TRUEY.test(trimmed) ? true : FALSEY.test(trimmed) ? false : null;
  const on = parsed ?? fallback;

  return (
    <div className="flex min-w-0 items-start justify-between gap-3">
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex items-center gap-1.5">
          <span className="text-[12.5px] font-medium text-foreground" id={`${id}-label`}>
            {label}
          </span>
          {help && <Hint>{help}</Hint>}
        </div>
        {/*
          Rendered inline rather than only in the tooltip, for the same reason
          every other caveat in the settings is: the tooltip is invisible to
          touch and absent from a screenshot, and this is the one case where the
          saved value is not a value Lumine is using.
        */}
        {trimmed !== "" && parsed === null && (
          <p className="max-w-[46ch] text-[11.5px] leading-relaxed text-faint">
            Not a recognised value, so the worker leaves it out and the provider decides.
          </p>
        )}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-labelledby={`${id}-label`}
        onClick={() => onChange(on ? "false" : "true")}
        className={
          "relative mt-0.5 h-[22px] w-[38px] shrink-0 rounded-full transition-colors duration-150 " +
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
          (on ? "bg-accent" : "bg-surface-muted shadow-elev-1")
        }
      >
        <span
          aria-hidden="true"
          className={
            "absolute top-[3px] h-4 w-4 rounded-full bg-white shadow-elev-1 transition-all duration-150 " +
            (on ? "left-[19px]" : "left-[3px]")
          }
        />
      </button>
    </div>
  );
}
