import { useEffect, useId, useRef, useState } from "react";

/**
 * A field for entering a secret.
 *
 * Built once, here, rather than inline on the first page that needs one. Three
 * behaviours are not optional for a secret field and are easy to forget:
 *
 * - **The value is masked by default.** A credential pasted in a plain input is
 *   visible to anyone looking at the screen and ends up in a screenshot.
 * - **Autocomplete is suppressed.** A browser filling a credential field from its
 *   own saved-password store can put a *different* account's key in it, and
 *   nothing in the UI would show the difference.
 * - **Revealing is deliberate.** The toggle is a separate control, and it resets
 *   itself, so a key cannot be left on screen by someone who stepped away.
 *
 * The value is held in component state only while the field is open, and is
 * cleared on unmount. It is never persisted anywhere in the frontend; storing it
 * is the desktop layer's job, through a write-only command.
 */
export type SecretFieldProps = {
  /** Shown above the input. */
  label: string;
  /** Where the secret comes from, e.g. a provider's setup page. */
  help?: string;
  /** Called with the trimmed secret. Rejecting surfaces `error` to the user. */
  onSubmit: (secret: string) => Promise<void>;
  submitLabel?: string;
  onCancel?: () => void;
  busy?: boolean;
  /** Set when the attempt failed, to explain why without a generic message. */
  error?: string | null;
};

export function SecretField({
  label,
  help,
  onSubmit,
  submitLabel = "Store key",
  onCancel,
  busy = false,
  error,
}: SecretFieldProps) {
  const [value, setValue] = useState("");
  const [revealed, setRevealed] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const fieldId = useId();

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Leaving a revealed field open is how a secret ends up in a screenshot.
  useEffect(() => {
    if (!revealed) return;
    const timer = window.setTimeout(() => setRevealed(false), 30_000);
    return () => window.clearTimeout(timer);
  }, [revealed]);

  const trimmed = value.trim();

  return (
    <form
      className="secret-field"
      onSubmit={(event) => {
        event.preventDefault();
        if (trimmed && !busy) void onSubmit(trimmed);
      }}
    >
      <label className="field" htmlFor={fieldId}>
        <span className="field-label">{label}</span>
        <div className="secret-input">
          <input
            id={fieldId}
            ref={inputRef}
            type={revealed ? "text" : "password"}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="Paste the key"
            spellCheck={false}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            // Keeps the value out of the browser's password-manager heuristics
            // without affecting paste, which is the expected way to enter a key.
            data-1p-ignore
            data-lpignore="true"
            data-bwignore
          />
          <button
            type="button"
            className="secret-reveal"
            onClick={() => setRevealed((current) => !current)}
            aria-pressed={revealed}
            // Not a tooltip-only control: the label says what it does.
            title={revealed ? "Hide the key" : "Show the key"}
          >
            {revealed ? "Hide" : "Show"}
          </button>
        </div>
        {help && <small className="field-hint">{help}</small>}
      </label>

      {error && <p className="notice is-error">{error}</p>}

      <div className="secret-actions">
        <button type="submit" className="settings-primary" disabled={!trimmed || busy}>
          {busy ? "Storing…" : submitLabel}
        </button>
        {onCancel && (
          <button type="button" className="settings-secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
