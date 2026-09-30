import { useState } from "react";

import type { ConfigDocument, ProviderCatalog, VoiceProfile } from "../../../features/settings/aiConfigTypes";
import {
  activateProfile,
  canDeleteProfile,
  createProfile,
  deleteProfile,
  duplicateProfile,
} from "../../../features/settings/profileOps";
import { Dropdown } from "../../../components/ui/dropdown";
import { Hint } from "../../../components/ui/hint";

/**
 * Which profile is active, as a dropdown.
 *
 * ## Why not a list
 *
 * This used to render every profile as a row, each with its own Duplicate and
 * Delete buttons. Two problems, both about attention rather than capacity: a
 * document with four profiles put four rows of controls on a screen that has
 * sections below it competing for the same eye, and the controls on a row that is
 * *not* active are the ones nobody wanted — you are editing the active profile, so
 * the other three rows' buttons are decoration.
 *
 * A dropdown puts the choice in one place and keeps the actions for the thing
 * being edited. The profiles are not hidden, though: the row under the dropdown
 * names the active one and shows the models it uses, and the dropdown's own
 * trigger does the same.
 *
 * ## Every structural change still goes through a pure helper
 *
 * `profileOps` decides what a new profile contains, and no provider name appears
 * in this file.
 */
export type ProfileListProps = {
  catalog: ProviderCatalog;
  document: ConfigDocument;
  /** True when the active profile still mirrors agent/.env rather than a save. */
  isEnvironmentBacked: boolean;
  hasUnsavedChanges: boolean;
  onMutate: (operation: (document: ConfigDocument) => ConfigDocument) => void;
  onDiscard: () => void;
};

const NEW_PIPELINE = " new:pipeline";
const NEW_REALTIME = " new:realtime";

export function ProfileList({
  catalog,
  document,
  isEnvironmentBacked,
  hasUnsavedChanges,
  onMutate,
  onDiscard,
}: ProfileListProps) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const active = document.profiles.find((profile) => profile.id === document.activeProfileId);
  const anyDeletable = canDeleteProfile(document);

  return (
    <section className="settings-block">
      <div className="settings-block-head flex items-center gap-1.5">
        <h2>Profile</h2>
        <Hint label="What a profile is">
          A profile is one complete way for Lumine to listen, think, and speak — a stack of models plus the
          settings they take. The active profile is what the next voice session uses, so switching here
          changes the next conversation and not the one in progress.
        </Hint>
      </div>

      <div className="flex min-w-0 flex-col gap-2">
        <Dropdown
          label="Active profile"
          value={active?.id ?? ""}
          onChange={(id) => {
            if (id === "" || id === active?.id) return;
            onMutate((current) => activateProfile(current, id));
            setConfirmingDelete(false);
          }}
          placeholder="No profile is active"
          options={document.profiles.map((profile) => ({
            value: profile.id,
            label: profile.name,
            hint: describeProfile(profile),
          }))}
        />

        {active && (
          <p className="text-[11.5px] leading-relaxed text-faint">
            {describeProfile(active)}
            {isEnvironmentBacked && !hasUnsavedChanges && " · read from agent/.env"}
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Dropdown
          label="Add a profile"
          // A sentinel value rather than a control that opens a naming form: the
          // name is editable on the profile itself a few lines down, so a second
          // field to fill in before anything exists is a step with no purpose.
          value=""
          onChange={(choice) => {
            if (choice === "") return;
            const kind = choice === NEW_REALTIME ? "realtime" : "pipeline";
            onMutate((current) => createProfile(catalog, current, kind, ""));
          }}
          placeholder="Add a profile…"
          options={[
            { value: NEW_PIPELINE, label: "Pipeline", hint: "Speech, thinking and voice chosen separately" },
            { value: NEW_REALTIME, label: "Realtime", hint: "One model for all three" },
          ]}
        />

        {active && (
          <>
            <button
              type="button"
              className="settings-quiet py-[9px] px-1.5 text-soft bg-transparent text-[12px] cursor-pointer"
              title="Copy this profile and switch to the copy"
              onClick={() => onMutate((current) => duplicateProfile(current, active.id))}
            >
              Duplicate
            </button>
            {anyDeletable && (
              <button
                type="button"
                className="settings-quiet py-[9px] px-1.5 text-soft bg-transparent text-[12px] cursor-pointer"
                onClick={() => setConfirmingDelete((open) => !open)}
              >
                Delete
              </button>
            )}
          </>
        )}
      </div>

      {confirmingDelete && active && (
        <div className="profile-confirm flex flex-col gap-2 p-3 rounded-sm bg-[var(--color-stage)] shadow-elev-2">
          <p>
            Delete <strong>{active.name}</strong>?
            {active.id === document.activeProfileId && " Another profile will take over."}
          </p>
          <div className="profile-confirm-actions flex gap-1.5 flex-wrap">
            <button type="button" className="settings-secondary" onClick={() => setConfirmingDelete(false)}>
              Keep it
            </button>
            <button
              type="button"
              className="settings-danger"
              onClick={() => {
                onMutate((current) => deleteProfile(current, active.id));
                setConfirmingDelete(false);
              }}
            >
              Delete
            </button>
          </div>
        </div>
      )}

      {hasUnsavedChanges && (
        <p className="field-hint text-faint text-[11.5px] leading-[1.5]">
          Changes are part of the draft. Save to make them active, or{" "}
          <button type="button" className="settings-quiet py-[9px] px-1.5 text-soft bg-transparent text-[12px] cursor-pointer" onClick={onDiscard}>
            discard
          </button>
          .
        </p>
      )}
      {!active && <p className="notice is-warning">No profile is active. The worker will use agent/.env.</p>}
    </section>
  );
}

/**
 * One line naming what a profile runs, for the dropdown's rows and the caption.
 *
 * A realtime profile is one model, so that is all it has. A pipeline has three,
 * and listing them is the only way to tell two pipeline profiles apart when both
 * are called "Default".
 */
export function describeProfile(profile: VoiceProfile): string {
  if (profile.kind === "realtime") return profile.realtime?.model ?? "Realtime";
  const stages = [profile.pipeline?.stt?.model, profile.pipeline?.llm?.model, profile.pipeline?.tts?.model].filter(
    Boolean,
  );
  return stages.length > 0 ? stages.join(" · ") : "Pipeline";
}
