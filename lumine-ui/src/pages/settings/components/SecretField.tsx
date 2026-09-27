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
  /**
   * What kind of value this is. Defaults to `secret`.
   *
   * `url` and `text` are for the parts of a credential that are not secret: a
   * server address, a project name. They are drawn in the clear on purpose. A
   * masked hostname is a typo nobody can see, and the mistake behind the original
   * LiveKit bug -- an API key pasted into the server URL -- is exactly the one a
   * plain-text field makes obvious and a password field hides. The masking,
   * autocomplete suppression and timed reveal below apply to `secret` only.
   */
  kind?: "secret" | "url" | "text";
  placeholder?: string;
  /**
   * Whether a value is already held for this exact variable, and the last four
   * characters of it.
   *
   * The field itself is always empty — a credential is write-only and there is
   * deliberately no command that reads one back. That makes an empty box next to
   * a stored value genuinely ambiguous, so the state is stated: "Stored ····3f2a"
   * above the input, and the button reads Replace rather than Save.
   *
   * `inEnv` says the value is in the environment instead, which is a *different*
   * answer from stored and one the user may not know about: `agent/.env` can be
   * configured long before anybody opens this screen.
   */
  stored?: boolean;
  storedLast4?: string | null;
  inEnv?: boolean;
  /**
   * Whether this field takes focus when it mounts. Default true.
   *
   * A panel holding three of these mounted them all at once, and each one focused
   * itself, so the last one won and the user landed in the API secret field
   * having been asked for a server address. A caller that draws several fields
   * together turns this off and focuses only the one that still needs a value.
   */
  autoFocus?: boolean;
};

export function SecretField({
  label,
  help,
  onSubmit,
  submitLabel = "Store key",
  onCancel,
  busy = false,
  error,
  kind = "secret",
  placeholder,
  stored = false,
  storedLast4 = null,
  inEnv = false,
  autoFocus = true,
}: SecretFieldProps) {
  const [value, setValue] = useState("");
  const [revealed, setRevealed] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const fieldId = useId();

  const masked = kind === "secret";

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);

  // Leaving a revealed field open is how a secret ends up in a screenshot.
  useEffect(() => {
    if (!masked || !revealed) return;
    const timer = window.setTimeout(() => setRevealed(false), 30_000);
    return () => window.clearTimeout(timer);
  }, [revealed, masked]);

  const trimmed = value.trim();

  return (
    <form
      className="secret-field"
      onSubmit={(event) => {
        event.preventDefault();
        if (trimmed && !busy) void onSubmit(trimmed);
      }}
    >
      {(stored || inEnv) && (
        /*
          The state of *this* variable, not of the provider. A provider can be
          two thirds filled, and a single sentence about the provider cannot say
          which two — so the one already saved shows as a tick here and the two
          that are not show as empty, which is the only arrangement in which the
          next thing to do is obvious.
        */
        <p className={"secret-state" + (inEnv && !stored ? " is-env" : "")}>
          <span className="secret-state-dot" aria-hidden="true" />
          {stored
            ? storedLast4
              ? `Stored ····${storedLast4}`
              : "Stored"
            : "Set in agent/.env"}
        </p>
      )}
      <label className="field" htmlFor={fieldId}>
        <span className="field-label">{label}</span>
        <div className="secret-input">
          <input
            id={fieldId}
            ref={inputRef}
            type={masked ? (revealed ? "text" : "password") : "text"}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder={
              stored || inEnv
                ? "Leave empty to keep what is stored"
                : placeholder ?? (kind === "url" ? "https://your-project.livekit.cloud" : "Paste the key")
            }
            spellCheck={false}
            // Suppressed on every field. For a secret it keeps a browser from
            // substituting a different account's saved key; for an address it
            // stops it from rewriting what was pasted.
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            // Keeps the value out of the browser's password-manager heuristics
            // without affecting paste, which is the expected way to enter a key.
            data-1p-ignore
            data-lpignore="true"
            data-bwignore
          />
          {masked && (
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
          )}
        </div>
        {help && <small className="field-hint">{help}</small>}
      </label>

      {error && <p className="notice is-error">{error}</p>}

      <div className="secret-actions">
        <button type="submit" className="settings-primary" disabled={!trimmed || busy}>
          {/* No `.toLowerCase()`. The label is a proper noun more often than not --
              "API key", not "api key" -- and a button that mangles the name of the
              thing it is about to store is worse than one that is slightly long. */}
          {busy ? "Storing…" : stored || inEnv ? `Replace ${label}` : submitLabel}
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
