import { useState } from "react";

import type { ConfigDocument, ProfileKind, ProviderCatalog } from "../../../features/settings/aiConfigTypes";
import {
  activateProfile,
  canDeleteProfile,
  createProfile,
  deleteProfile,
  duplicateProfile,
} from "../../../features/settings/profileOps";
import { Icon } from "../../home/components/Icon";

/**
 * The profile list.
 *
 * Sits at the top of Voice & Models rather than on a page of its own: choosing
 * what Lumine runs and configuring what it runs on are one decision, and
 * splitting them put the choice somewhere it could not be made.
 *
 * Every structural change goes through a pure helper in `profileOps`, so the
 * catalog decides what a new profile contains and no provider name appears here.
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

export function ProfileList({
  catalog,
  document,
  isEnvironmentBacked,
  hasUnsavedChanges,
  onMutate,
  onDiscard,
}: ProfileListProps) {
  const [adding, setAdding] = useState<ProfileKind | null>(null);
  const [draftName, setDraftName] = useState("");
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);

  const active = document.profiles.find((profile) => profile.id === document.activeProfileId);
  const anyDeletable = canDeleteProfile(document);

  const submitNew = (event: React.FormEvent) => {
    event.preventDefault();
    const kind = adding;
    if (!kind) return;
    const name = draftName.trim();
    setAdding(null);
    setDraftName("");
    onMutate((current) => createProfile(catalog, current, kind, name));
  };

  return (
    <section className="settings-block">
      <div className="settings-block-head">
        <h2>Profiles</h2>
        <p>
          A profile is one complete way for Lumine to listen, think, and speak. The active profile is what
          the next voice session uses.
        </p>
      </div>

      <ul className="profile-list">
        {document.profiles.map((profile) => {
          const isActive = profile.id === document.activeProfileId;
          const summary =
            profile.kind === "realtime"
              ? (profile.realtime?.model ?? "Realtime")
              : [profile.pipeline?.stt?.model, profile.pipeline?.llm?.model, profile.pipeline?.tts?.model]
                  .filter(Boolean)
                  .join(" · ");
          return (
            <li key={profile.id} className={isActive ? "profile-row is-active" : "profile-row"}>
              <button
                type="button"
                className="profile-select"
                onClick={() => onMutate((current) => activateProfile(current, profile.id))}
                aria-current={isActive ? "true" : undefined}
              >
                <span className="profile-name">
                  {profile.name}
                  {isActive && isEnvironmentBacked && !hasUnsavedChanges && (
                    <span className="profile-tag">From agent/.env</span>
                  )}
                </span>
                <span className="profile-summary">{summary}</span>
              </button>

              <div className="profile-actions">
                <button
                  type="button"
                  className="settings-quiet"
                  title="Copy this profile and switch to the copy"
                  onClick={() => onMutate((current) => duplicateProfile(current, profile.id))}
                >
                  Duplicate
                </button>
                {anyDeletable && (
                  <button
                    type="button"
                    className="settings-quiet"
                    onClick={() => setConfirmingDelete(confirmingDelete === profile.id ? null : profile.id)}
                  >
                    Delete
                  </button>
                )}
              </div>

              {confirmingDelete === profile.id && (
                <div className="profile-confirm">
                  <p>
                    Delete <strong>{profile.name}</strong>?
                    {isActive && " It is active, so another profile will take over."}
                  </p>
                  <div className="profile-confirm-actions">
                    <button
                      type="button"
                      className="settings-secondary"
                      onClick={() => setConfirmingDelete(null)}
                    >
                      Keep it
                    </button>
                    <button
                      type="button"
                      className="settings-danger"
                      onClick={() => {
                        onMutate((current) => deleteProfile(current, profile.id));
                        setConfirmingDelete(null);
                      }}
                    >
                      Delete
                    </button>
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>

      {adding ? (
        <form className="profile-add" onSubmit={submitNew}>
          <label className="field">
            <span className="field-label">Name for the new {adding} profile</span>
            <input
              value={draftName}
              onChange={(event) => setDraftName(event.target.value)}
              placeholder={adding === "realtime" ? "Realtime" : "Pipeline"}
              spellCheck={false}
              autoFocus
            />
            <small className="field-hint">
              It starts from the defaults for a {adding} stack. Change anything afterwards.
            </small>
          </label>
          <div className="profile-confirm-actions">
            <button type="submit" className="settings-primary">
              Create
            </button>
            <button
              type="button"
              className="settings-secondary"
              onClick={() => {
                setAdding(null);
                setDraftName("");
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <div className="profile-add-actions">
          <button type="button" className="settings-secondary" onClick={() => setAdding("pipeline")}>
            <Icon name="plus" size={14} /> New pipeline profile
          </button>
          <button type="button" className="settings-secondary" onClick={() => setAdding("realtime")}>
            <Icon name="plus" size={14} /> New realtime profile
          </button>
        </div>
      )}

      {hasUnsavedChanges && (
        <p className="field-hint">
          Profile changes are part of the draft. Save to make them the active configuration, or{" "}
          <button type="button" className="settings-quiet" onClick={onDiscard}>
            discard
          </button>
          .
        </p>
      )}
      {!active && <p className="notice is-warning">No profile is active. The worker will use agent/.env.</p>}
    </section>
  );
}
