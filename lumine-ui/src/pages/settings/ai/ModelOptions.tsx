import { useId } from "react";

import type { CatalogModel, CatalogOption } from "../../../features/settings/aiConfigTypes";
import { Dropdown } from "../../../components/ui/dropdown";
import { Disclosure } from "../../../components/ui/disclosure";
import { Knob } from "../../../components/ui/knob";
import { Hint } from "../../../components/ui/hint";

/**
 * Renders the settings a selected model declares it accepts.
 *
 * Nothing here names a provider, a model, or a setting. The backend sends the list
 * with the values each one allows and the widget each one wants, and this draws
 * it — so selecting a different model shows that model's settings without a line of
 * new code, and a value the model would reject is never offered in the first
 * place.
 *
 * ## An empty option means the provider decides
 *
 * The control starts empty rather than pre-filled. Pre-filling would mean choosing
 * a value on the user's behalf, and the difference between "1.0 because they
 * wanted 1.0" and "1.0 because the widget had to show something" is the
 * difference between a rejected request and a slow answer. So every control here
 * has a real unset state and a way back to it.
 *
 * ## The widget comes from the catalog
 *
 * `control` and the numeric bounds are the backend's declaration, taken from each
 * plugin's own signature. A setting with a range gets a slider that stops at the
 * provider's limits; a closed set gets a dropdown; anything else gets a text box.
 * Guessing here instead would put a slider around a range the provider will
 * refuse, which in a voice session looks exactly like a muted microphone.
 *
 * ## Advanced
 *
 * The catalog marks each option advanced or not, and the judgement is made there
 * rather than here: a `temperature` is ordinary on one model and meaningless on
 * another, and a name-based guess in the frontend would be wrong in both
 * directions. What lands behind the disclosure is the settings whose default is
 * already a good answer.
 */
export type ModelOptionsProps = {
  model: CatalogModel | undefined;
  values: Record<string, string>;
  onChange: (values: Record<string, string>) => void;
  /** Shown when the model declares no options at all. */
  emptyHint?: string;
};

export function ModelOptions({ model, values, onChange, emptyHint }: ModelOptionsProps) {
  if (!model || model.options.length === 0) {
    return emptyHint ? <small className="field-hint">{emptyHint}</small> : null;
  }

  const set = (name: string, next: string) => {
    const merged = { ...values };
    // Deleted rather than set to "". An empty string is a value, and most
    // providers will reject it; an absent key is the documented way to say "you
    // decide".
    if (next === "") {
      delete merged[name];
    } else {
      merged[name] = next;
    }
    onChange(merged);
  };

  // Hidden first, and for a different reason than the other two splits. An
  // `api_key` on a provider that has no key is not a setting: it is an argument
  // the plugin requires and the server ignores, and drawing it as a box somebody
  // has to fill in correctly would present a non-problem as a setup step.
  const drawable = model.options.filter((option) => !option.hidden);
  const visible = drawable.filter((option) => !option.advanced);
  const advanced = drawable.filter((option) => option.advanced);
  // Only options actually carrying a value count. A saved profile that once had a
  // temperature and no longer does should not keep claiming to have one.
  const setCount = advanced.filter((option) => (values[option.name] ?? "") !== "").length;

  return (
    <div className="model-options">
      {visible.map((option) => (
        <OptionRow key={option.name} option={option} value={values[option.name] ?? ""} onChange={set} />
      ))}
      {advanced.length > 0 && (
        <Disclosure label="Advanced" count={setCount}>
          {advanced.map((option) => (
            <OptionRow key={option.name} option={option} value={values[option.name] ?? ""} onChange={set} />
          ))}
        </Disclosure>
      )}
    </div>
  );
}

function OptionRow({
  option,
  value,
  onChange,
}: {
  option: CatalogOption;
  value: string;
  onChange: (name: string, value: string) => void;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const isChoice = option.values.length > 0;
  const isRange = option.minimum !== null && option.maximum !== null;

  // A slider, when the provider published its bounds. A closed set, when the
  // provider enumerated its values. A text box only when it did neither — which is
  // the honest outcome, because there is nothing to bound or enumerate with.
  if (option.control === "slider" && isRange) {
    return (
      <Knob
        name={option.name}
        value={value}
        onChange={(next) => onChange(option.name, next)}
        minimum={option.minimum!}
        maximum={option.maximum!}
        step={option.step ?? 0.01}
        help={option.notes}
      />
    );
  }

  if (option.control === "select" || option.control === "combobox") {
    return (
      <div className="field">
        <div className="field-row" style={{ alignItems: "center" }}>
          <span className="field-label" id={`${id}-label`}>
            {label(option.name)}
          </span>
          {option.notes && <Hint>{option.notes}</Hint>}
        </div>
        <Dropdown
          id={id}
          label={label(option.name)}
          value={value}
          onChange={(next) => onChange(option.name, next)}
          placeholder="Model default"
          options={option.values.map((candidate) => ({ value: candidate, label: candidate }))}
          // An enumerated provider-side id is worth reaching for by hand — a
          // cloned voice, a model the plugin added after this build.
          editable="Enter a value"
        />
      </div>
    );
  }

  return (
    <label className="field" htmlFor={id}>
      <span className="field-label">{label(option.name)}</span>
      {/* An enumerated setting is a dropdown even in the fallback path. The same
          argument as everywhere else in the settings: one chooser, drawn once, so
          the app does not change idiom halfway down a page. */}
      {isChoice ? (
        <Dropdown
          id={id}
          label={label(option.name)}
          value={value}
          onChange={(next) => onChange(option.name, next)}
          placeholder="Model default"
          options={option.values.map((candidate) => ({ value: candidate, label: candidate }))}
        />
      ) : (
        <input
          id={id}
          type="text"
          inputMode="decimal"
          value={value}
          placeholder="Model default"
          aria-describedby={option.notes ? hintId : undefined}
          onChange={(event) => onChange(option.name, event.target.value.trim())}
          spellCheck={false}
        />
      )}
      {/*
        Rendered inline, not behind the `?` above. A tooltip is invisible to touch,
        absent from a screenshot, and easy to miss entirely — so a caveat that
        exists nowhere else stays here. The `?` is for the repeatable part.
      */}
      {option.notes && !isChoice && (
        <small className="field-hint" id={hintId}>
          {option.notes}
        </small>
      )}
    </label>
  );
}

/** A readable name for a setting, derived from its key. */
function label(name: string): string {
  const spaced = name.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
