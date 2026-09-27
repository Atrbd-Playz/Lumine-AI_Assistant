import {
  findModel,
  findProvider,
  modelsFor,
  providersFor,
  type Capability,
  type ModelRef,
  type ProviderCatalog,
} from "../../../features/settings/aiConfigTypes";
import { Dropdown } from "../../../components/ui/dropdown";
import { Field } from "../components/Field";

/**
 * One control for "which model serves this stage".
 *
 * ## One list, not two
 *
 * This was a pair of native `<select>` elements — provider, then model — and that
 * shape asks two questions when only the second one is a real decision. Almost
 * always there is one provider worth using, and the act of choosing between
 * providers and models was a step that had to be taken before the interesting one
 * could be. So the models are listed once, grouped under their provider, and the
 * answer to "which one" is one click.
 *
 * The grouping is not decoration: base-ui's `Select.Group` is what makes the arrow
 * keys and typeahead move through a provider's models as a family, and it keeps a
 * family together when the list scrolls.
 *
 * ## The value is a pair
 *
 * Encoded as `provider/model`, split on the *first* slash. Model ids contain
 * slashes of their own — Groq serves `openai/gpt-oss-20b` — so a naive split on
 * the last one would attribute the model to a provider called `openai`. Provider
 * ids have no slash, which is what makes the first one the separator.
 */

/** Provider ids never contain a slash; model ids routinely do. */
const SEP = "/";

function encode(provider: string, model: string): string {
  return `${provider}${SEP}${model}`;
}

function decode(value: string): ModelRef {
  const at = value.indexOf(SEP);
  // A value that arrived from somewhere other than this control is still worth
  // showing rather than throwing away, so an unparseable one becomes a bare model
  // under a provider that cannot be identified.
  if (at < 0) return { provider: "", model: value };
  return { provider: value.slice(0, at), model: value.slice(at + 1) };
}

function statusHint(status: string, replaces: string | null, isDefault: boolean): string | undefined {
  if (status === "retired") return "Retired by the provider";
  if (status === "deprecated") return replaces ? `Deprecated — use ${replaces}` : "Deprecated";
  return isDefault ? "Recommended" : undefined;
}

export function modelOptionLabel(model: { label: string; status: string; default: boolean }): string {
  if (model.status === "retired") return `${model.label} · retired`;
  if (model.status === "deprecated") return `${model.label} · deprecated`;
  if (model.default) return `${model.label} · recommended`;
  return model.label;
}

export type StageSelectProps = {
  catalog: ProviderCatalog;
  label: string;
  capability: Capability;
  value: ModelRef | undefined;
  onChange: (next: ModelRef) => void;
  hint?: string;
};

export function StageSelect({ catalog, label, capability, value, onChange, hint }: StageSelectProps) {
  const providers = providersFor(catalog, capability);

  const options = providers.flatMap((provider) => {
    // A provider with nothing for this capability contributes no caption and no
    // rows; an empty section in the list is a gap the user has to scroll past.
    const list = modelsFor(catalog, provider.id, capability);
    if (list.length === 0) return [];
    return list.map((model) => ({
      value: encode(provider.id, model.id),
      label: modelOptionLabel(model),
      hint: statusHint(model.status, model.replaces, model.default),
      group: provider.local ? `${provider.label} · on this computer` : provider.label,
    }));
  });

  const changeModel = (next: string) => {
    const picked = decode(next);
    if (!picked.provider || !picked.model) return;
    // The previous model's settings are dropped rather than carried over: what one
    // model accepts, another may reject, and a stale value is a request the
    // provider refuses. The language is kept, because every model in a given
    // provider speaks the same set and the user did not ask to change it.
    onChange({
      provider: picked.provider,
      model: picked.model,
      ...(value?.language ? { language: value.language } : {}),
    });
  };

  const selected = value?.provider && value?.model ? encode(value.provider, value.model) : "";

  return (
    <Field
      label={label}
      hint={
        hint ??
        (options.length === 0
          ? "No provider offers a model for this stage."
          : // Naming the model that answers today turns this control from a list
            // into a status readout as well as a chooser.
            selected
            ? `Currently ${findProvider(catalog, value!.provider)?.label ?? value!.provider} · ${findModel(catalog, value!.provider, value!.model, capability)?.label ?? value!.model}`
            : undefined)
      }
    >
      <Dropdown
        label={label}
        value={selected}
        onChange={changeModel}
        options={options}
        placeholder={options.length === 0 ? "No models available" : "Not set"}
        disabled={options.length === 0}
      />
    </Field>
  );
}
