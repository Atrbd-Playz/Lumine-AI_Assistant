import { voicesFor, type ProviderCatalog } from "../../../features/settings/aiConfigTypes";
import { Dropdown } from "../../../components/ui/dropdown";
import { Field } from "../components/Field";

/**
 * The voice a synthesis stage speaks with.
 *
 * A dropdown, where this used to be a text box with a `<datalist>` of the voices
 * we had curated. The datalist is a native popup, so it had the same problem the
 * native `<select>` had — a bright system panel in the middle of a warm screen —
 * and it offered no way to see a voice's name next to its id before choosing it.
 *
 * The curated list stays curated, because it is the only part of the provider's
 * catalogue anyone has vetted, and anything outside it is reached through the
 * control's own text row rather than a second widget.
 */
export type VoiceSelectProps = {
  catalog: ProviderCatalog;
  providerId: string;
  value: string | undefined;
  onChange: (voice: string) => void;
  label?: string;
};

export function VoiceSelect({ catalog, providerId, value, onChange, label = "Voice" }: VoiceSelectProps) {
  const voices = voicesFor(catalog, providerId);
  const count = voices.length;

  return (
    <Field
      label={label}
      hint={
        count > 0
          ? `${count} curated for this provider. Anything else can be entered by id.`
          : "This provider publishes no curated voice list, so enter a voice id."
      }
    >
      <Dropdown
        label={label}
        value={value ?? ""}
        onChange={onChange}
        options={voices.map((voice) => ({ value: voice.id, label: voice.label }))}
        placeholder={count > 0 ? "Provider default" : "Enter a voice id"}
        editable="Enter a voice id"
      />
    </Field>
  );
}
