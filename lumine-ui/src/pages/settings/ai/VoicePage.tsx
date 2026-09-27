import type { ConfigDocument, ProviderCatalog, VoiceProfile } from "../../../features/settings/aiConfigTypes";
import { SettingsPageHeader } from "../components/SettingsPageHeader";
import { Field } from "../components/Field";
import { AiSaveFooter, type AiSaveFooterProps } from "../components/AiSaveFooter";
import { ActiveCard } from "./ActiveCard";
import { ProfileList } from "./ProfileList";

/**
 * Which profile speaks, and what it is called.
 *
 * ## Why the models moved out
 *
 * This screen used to be the whole speech configuration: the stack type, all four
 * stages, every model setting, interruption, the checks, and the save button. It
 * was one column longer than anyone reads, and the parts of it that answer a
 * question about *how Lumine sounds* — which profile, which voice, what happens
 * if you talk over her — were separated by the parts that answer a question about
 * *which engines are installed*.
 *
 * So the engines moved to Models, one tab per stage, and this screen is the
 * short answer to "what is she going to sound like". The active-configuration
 * card is here for the same reason: the flow diagram is the only place the whole
 * stack is visible at once, and it is a statement about the voice, not about any
 * one setting.
 */
export type VoicePageProps = AiSaveFooterProps & {
  catalog: ProviderCatalog;
  profile: VoiceProfile;
  document: ConfigDocument;
  isEnvironmentBacked: boolean;
  onChange: (profile: VoiceProfile) => void;
  onMutateDocument: (operation: (document: ConfigDocument) => ConfigDocument) => void;
  onDiscard: () => void;
  hasUnsavedChanges: boolean;
};

export function VoicePage({
  catalog,
  profile,
  document,
  isEnvironmentBacked,
  onChange,
  onMutateDocument,
  onDiscard,
  hasUnsavedChanges,
  ...footer
}: VoicePageProps) {
  return (
    <div className="settings-page">
      <SettingsPageHeader section="voice" />

      <ProfileList
        catalog={catalog}
        document={document}
        isEnvironmentBacked={isEnvironmentBacked}
        hasUnsavedChanges={hasUnsavedChanges}
        onMutate={onMutateDocument}
        onDiscard={onDiscard}
      />

      <Field label="Profile name" help="What this configuration is called in the list above.">
        <input
          value={profile.name}
          onChange={(event) => onChange({ ...profile, name: event.target.value })}
          placeholder="Lumine Default"
          spellCheck={false}
        />
      </Field>

      <ActiveCard
        catalog={catalog}
        profile={profile}
        isEnvironmentBacked={isEnvironmentBacked}
        hasUnsavedChanges={hasUnsavedChanges}
      />

      {isEnvironmentBacked && (
        <p className="notice">
          This profile is derived from <code>agent/.env</code>. Change anything and save to take control of the
          stack from the desktop app.
        </p>
      )}

      <AiSaveFooter {...footer} onDiscard={onDiscard} hasUnsavedChanges={hasUnsavedChanges} />
    </div>
  );
}
