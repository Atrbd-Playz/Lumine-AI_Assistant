import { Slider } from "@base-ui/react/slider";

import { Hint } from "./hint";

/**
 * A continuous setting, as a knob you drag.
 *
 * ## Why sliders for these and not a text box
 *
 * Speed, volume, temperature and top-p are all "how much", and a number field
 * cannot show how much. `0.95` and `1.4` are equally plausible-looking strings; a
 * track with a thumb in it says "slightly below normal" without the user having to
 * know what normal is. The number is still shown, still editable by keyboard, and
 * still the thing that gets saved — the slider is how you get there.
 *
 * ## The range is the provider's
 *
 * `min`/`max`/`step` come from the catalog, which took them from the plugin's own
 * signature. That is the point: sonic-3 refuses a speed below 0.6, and a control
 * that offered 0.1 would be a control that produces a rejected request. A model
 * with no declared range gets a number field instead, because a slider with
 * invented bounds is a lie about what the provider accepts.
 *
 * ## "Default" is a real state, not zero
 *
 * A stage with nothing saved sends nothing, and the provider decides. So the knob
 * has a third position — a dashed rest state at the far left, before the range
 * begins — and a `Reset` that returns to it. Pre-filling 1.0 would mean choosing
 * a value on the user's behalf, and the difference between "1.0" and "unset" is
 * exactly the difference between a rejected request and a slow answer.
 */
export type KnobProps = {
  /** The setting's name, as the catalog spells it. */
  name: string;
  /** What the number currently is, or `""` when the provider should decide. */
  value: string;
  onChange: (value: string) => void;
  minimum: number;
  maximum: number;
  step: number;
  /** Names the two ends in words, e.g. `["Slower", "Faster"]`. */
  ends?: [string, string] | [string, string, string?];
  /** How the number is displayed. Defaults to two decimals. */
  format?: (value: number) => string;
  /** The explanation behind the `?`. */
  help?: string;
  disabled?: boolean;
};

export function Knob({
  name,
  value,
  onChange,
  minimum,
  maximum,
  step,
  ends,
  format = (input) => input.toFixed(2),
  help,
  disabled,
}: KnobProps) {
  const unset = value === "";
  const numeric = Number(value);
  const current = unset || Number.isNaN(numeric) ? minimum : Math.min(maximum, Math.max(minimum, numeric));
  // A value the user cannot see is a value they cannot fix, so anything outside
  // the declared range is shown as-is with a warning rather than silently clamped.
  const outOfRange = !unset && !Number.isNaN(numeric) && (numeric < minimum || numeric > maximum);

  return (
    <div className="min-w-0">
      <div className="flex items-baseline gap-2">
        <span className="text-[12.5px] font-medium text-foreground">{title(name)}</span>
        {help && <Hint>{help}</Hint>}
        <span className="ml-auto flex items-baseline gap-1.5">
          <span
            className={
              "font-mono text-[12px] tabular-nums " +
              (unset ? "text-faint" : outOfRange ? "text-danger" : "text-soft")
            }
          >
            {unset ? "provider default" : format(numeric)}
          </span>
          {!unset && (
            <button
              type="button"
              // The label says which setting goes back, because a bare × on a row of
              // knobs is ambiguous at exactly the moment precision matters.
              aria-label={`Reset ${title(name).toLowerCase()} to the provider default`}
              onClick={() => onChange("")}
              disabled={disabled}
              className="text-[11px] text-faint underline-offset-2 transition-colors hover:text-accent hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:opacity-40"
            >
              Reset
            </button>
          )}
        </span>
      </div>

      <div className="relative mt-2 flex h-5 items-center">
        {/*
          The unset rest state. A dashed segment to the left of the range reads as
          "off", and it is the only place the two states are visibly different, so
          a user can tell "1.0 because I chose it" from "1.0 because the provider
          picked it" at a glance.
        */}
        <span
          aria-hidden="true"
          className={
            "absolute left-0 h-0.5 rounded-full border-l border-dashed transition-opacity " +
            "border-faint " +
            (unset ? "opacity-80" : "opacity-0")
          }
          style={{ width: "14px" }}
        />
        <div className="min-w-0 flex-1 pl-[18px]">
          <Slider.Root
            value={current}
            onValueChange={(next) => onChange(String(Number(next.toFixed(6))))}
            min={minimum}
            max={maximum}
            step={step}
            disabled={disabled}
            aria-label={title(name)}
            className="relative flex w-full touch-none items-center select-none"
          >
            <Slider.Control className="flex w-full items-center py-2">
              <Slider.Track className="relative h-1 w-full rounded-full bg-surface-muted shadow-elev-1">
                {/* Depth as fill: the travelled part is a step lighter than the rest,
                    so progress is legible without a coloured overlay. */}
                <Slider.Indicator className="h-full rounded-full bg-accent/55" />
              </Slider.Track>
              <Slider.Thumb
                className={
                  "size-3.5 rounded-full bg-accent shadow-elev-2 transition-transform " +
                  "hover:scale-110 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
                  "data-[disabled]:bg-faint"
                }
              />
            </Slider.Control>
          </Slider.Root>
        </div>
      </div>

      {ends && (
        <div className="mt-0.5 flex justify-between pl-[18px] text-[10.5px] text-faint">
          <span>{ends[0]}</span>
          {ends[2] && <span>{ends[2]}</span>}
          <span>{ends[1]}</span>
        </div>
      )}

      {outOfRange && (
        <p className="mt-1 text-[11.5px] text-danger">
          This provider accepts {minimum} to {maximum}. The saved value will be rejected.
        </p>
      )}
    </div>
  );
}

/** `min_completion_tokens` → `Min completion tokens`. */
function title(name: string): string {
  const spaced = name.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
