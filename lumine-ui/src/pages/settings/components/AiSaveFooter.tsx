/**
 * The Save / Discard bar for anything that edits the AI configuration.
 *
 * ## Why it is one component
 *
 * Two settings screens edit the same document, and a save button is the one
 * control whose absence is ambiguous: without it, is the page read-only, is
 * saving automatic, or did the button scroll away? One component means the answer
 * is the same shape on every screen that can change a profile.
 *
 * The bar is sticky rather than parked under the last control. The stages screen
 * is a scrolling column of pickers and sliders, and a Save that has scrolled out
 * of reach on a page of settings is a Save nobody finds. The fill matches the pane
 * behind it, so the settings passing underneath are hidden.
 */
export type AiSaveFooterProps = {
  onSave: () => void;
  onDiscard: () => void;
  canSave: boolean;
  saving: boolean;
  hasUnsavedChanges: boolean;
  saveError: string | null;
  /** What is being saved, so the button says what it acts on. */
  what?: string;
};

export function AiSaveFooter({
  onSave,
  onDiscard,
  canSave,
  saving,
  hasUnsavedChanges,
  saveError,
  what = "configuration",
}: AiSaveFooterProps) {
  return (
    <footer className="settings-actions">
      <button type="button" className="settings-secondary" onClick={onDiscard} disabled={!hasUnsavedChanges}>
        Discard
      </button>
      <button type="button" className="settings-primary" onClick={onSave} disabled={!canSave}>
        {saving ? "Saving…" : `Save ${what}`}
      </button>
      {saveError && <span className="settings-action-error">{saveError}</span>}
    </footer>
  );
}
