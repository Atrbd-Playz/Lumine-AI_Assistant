import { useId } from "react";

import type { CatalogModel, CatalogOption } from "../../../features/settings/aiConfigTypes";

/**
 * Renders the settings a selected model declares it accepts.
 *
 * Nothing here names a provider, a model, or a setting. The backend sends the
 * list with the values each one allows, and this draws it — so selecting a
 * different model shows that model's settings without a line of new code, and
 * a value the model would reject is never offered in the first place.
 *
 * An empty option means the provider applies its own default, which is what a
 * stage with no saved settings should send. That is why the control starts empty
 * rather than pre-filled: pre-filling would mean choosing a value on the user's
 * behalf, and a wrong one is a rejected request rather than a slow answer.
 */
export type ModelOptionsProps = {
  model: CatalogModel | undefined;
  values: Record<string, string>;
  onChange: (values: Record<string, string>) => void;
  /** Show the list even when the model declares nothing, to explain the absence. */
  emptyHint?: string;
};

export function ModelOptions({ model, values, onChange, emptyHint }: ModelOptionsProps) {
  if (!model || model.options.length === 0) {
    return emptyHint ? <small className="field-hint">{emptyHint}</small> : null;
  }

  const set = (name: string, next: string) => {
    const merged = { ...values };
    if (next === "") {
      delete merged[name];
    } else {
      merged[name] = next;
    }
    onChange(merged);
  };

  return (
    <div className="model-options">
      {model.options.map((option) => (
        <OptionRow key={option.name} option={option} value={values[option.name] ?? ""} onChange={set} />
      ))}
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

  return (
    <label className="field" htmlFor={id}>
      <span className="field-label">{label(option.name)}</span>
      {option.values.length > 0 ? (
        <select
          id={id}
          value={value}
          aria-describedby={option.notes ? hintId : undefined}
          onChange={(event) => onChange(option.name, event.target.value)}
        >
          {/* Empty is meaningful: it is "let the provider decide", which is the
              only choice guaranteed to be valid for whichever model is selected. */}
          <option value="">Model default</option>
          {option.values.map((candidate) => (
            <option key={candidate} value={candidate}>
              {candidate}
            </option>
          ))}
        </select>
      ) : (
        <input
          id={id}
          type="text"
          inputMode="decimal"
          value={value}
          aria-describedby={option.notes ? hintId : undefined}
          onChange={(event) => onChange(option.name, event.target.value)}
          spellCheck={false}
        />
      )}
      {option.notes && (
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
