import { useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import { SecretField } from "../settings/components/SecretField";
import { Hint } from "../../components/ui/hint";
import {
  setCredential,
  type CredentialSlotStatus,
  type RequiredCredential,
} from "../../features/settings/aiConfigClient";
import {
  credentialSlots,
  describeAvailability,
  describeRole,
  missingCredentials,
  slotProgress,
  type SetupState,
} from "../../features/settings/useSetupStatus";
import { useNotify } from "../../features/toast/useNotify";

/**
 * What has to be true before Lumine can hear anything.
 *
 * ## Why this is a screen and not a dialog
 *
 * Everything on it is also a setting. If setup were a modal, the one thing a user
 * cannot do — reach the screen where a key is wrong — would be behind the thing
 * blocking them. So this is a section of the home space, and the sidebar keeps
 * working: Diagnostics, the Avatar Lab and Appearance are all reachable, and the
 * voice button is disabled rather than the app being frozen.
 *
 * ## Why it lists only what this profile uses
 *
 * Asking for all six providers on a first run is how a wizard gets abandoned. The
 * list comes from the active profile plus LiveKit, so switching to a local
 * realtime model with no TTS genuinely removes a row rather than pretending it
 * does not matter.
 */
export type SetupGateProps = {
  setup: SetupState;
  /** Opens the Providers settings page, which is the deeper version of this. */
  onOpenProviders: () => void;
  /** Opens Diagnostics, for a configuration that is wrong rather than absent. */
  onOpenDiagnostics: () => void;
};

export function SetupGate({ setup, onOpenProviders, onOpenDiagnostics }: SetupGateProps) {
  const { status, error, loading, refresh } = setup;
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const notify = useNotify();

  if (loading && !status) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-[13px] text-faint">Checking what Lumine needs…</p>
      </div>
    );
  }

  /**
   * The catalog could not be read.
   *
   * The gate does not open on an unanswered question, so voice stays off — but
   * this is a broken local install, not a user mistake, and it says so instead of
   * blaming them for keys they may well have entered correctly.
   */
  if (error && !status) {
    return (
      <div className="mx-auto flex h-full w-full max-w-2xl flex-col gap-3">
        <div className="rounded-lg bg-surface p-4 shadow-elev-2">
          <h2 className="text-[14.5px] font-medium text-foreground">Lumine could not check its own setup</h2>
          <p className="mt-1.5 text-[13px] leading-relaxed text-soft">{error}</p>
          <p className="mt-1.5 text-[12px] leading-relaxed text-faint">
            This is about the local install, not your keys. Diagnostics will say which piece is missing.
          </p>
        </div>
        <div className="mt-auto flex gap-2">
          <PrimaryAction onClick={() => void refresh()}>Try again</PrimaryAction>
          <QuietAction onClick={onOpenDiagnostics}>Open Diagnostics</QuietAction>
        </div>
      </div>
    );
  }

  if (!status) return null;

  const missing = missingCredentials(status);

  if (status.ready) {
    return null;
  }

  const store = async (provider: RequiredCredential, slot: string, label: string, secret: string) => {
    setBusy(provider.id);
    setFieldError(null);
    try {
      await setCredential(provider.id, secret, slot);
      // The list is re-read rather than patched locally. A keyring write can
      // succeed while the worker still cannot see the value, and a gate that
      // believes its own optimism is worse than no gate.
      await refresh();
      // The panel is deliberately *not* closed. One of three LiveKit variables
      // was just stored, and closing the group made that read as "the credential
      // is done" — the exact impression the per-slot ticks now exist to correct.
      // Leaving it open puts the two fields that are still empty right there.
      notify({ tone: "success", message: `${label} stored.`, title: "Saved" });
    } catch (cause) {
      setFieldError(cause instanceof Error ? cause.message : `Could not store the ${label}.`);
    } finally {
      setBusy(null);
    }
  };

  // Values, not providers.
  //
  // The old line counted providers, so an install that was three of LiveKit's
  // values short said "one credential is not set", which is true in the same way
  // a note saying "3 of your 4 wheels are missing" is true and equally useless.
  // People count the work they are about to do, and the work is pasting keys.
  const owed = missing.reduce((total, provider) => total + provider.missing.length, 0);
  const have = missing.reduce(
    (total, provider) => total + Math.max(0, credentialSlots(provider).length - provider.missing.length),
    0,
  );
  const total = have + owed;

  return (
    <section className="mx-auto flex h-full min-h-0 w-full max-w-2xl flex-col gap-3 overflow-y-auto py-1 pr-1">
      <header>
        <p className="eyebrow">Setup</p>
        <h1 className="text-[20px] leading-tight text-foreground">
          {owed === 0
            ? "Everything is stored"
            : owed === 1
              ? "One more key to go"
              : `${owed} more keys to go`}
        </h1>
        <p className="mt-1.5 max-w-prose text-[13px] leading-relaxed text-soft">
          {have > 0 && (
            <>
              {have} of {total} {total === 1 ? "value is" : "values are"} already stored for{" "}
            </>
          )}
          <span className="text-foreground">
            {status.activeProfile ? `the ${status.activeProfile} profile` : "this profile"}
          </span>
          . Keys go into your system credential store — the app never writes them to a file, and never shows one back
          to you. Each box below is one value, and a saved one says so.
        </p>
        {total > 1 && (
          <div
            className="setup-progress"
            role="progressbar"
            aria-valuenow={have}
            aria-valuemin={0}
            aria-valuemax={total}
            aria-label="Setup progress"
          >
            <span className="setup-progress-fill" style={{ width: `${(have / total) * 100}%` }} />
          </div>
        )}
      </header>

      {status.blocking.length > 0 && (
        <div className="rounded-lg bg-danger-soft/40 p-4 shadow-elev-1">
          <div className="flex items-start gap-2">
            <h2 className="text-[13.5px] font-medium text-danger">The configuration itself needs a change</h2>
            <Hint label="Why this is separate from missing keys">
              Missing keys are something you can paste. These are Lumine's own opinions about the selected models —
              a retired model, a stage that needs something it was not given. Editing them is in Voice &amp; Models.
            </Hint>
          </div>
          <ul className="mt-2 flex flex-col gap-1.5">
            {status.blocking.map((problem) => (
              <li key={problem} className="text-[12.5px] leading-relaxed text-soft">
                {problem}
              </li>
            ))}
          </ul>
          <div className="mt-3">
            <QuietAction onClick={onOpenProviders}>Fix in Voice &amp; Models</QuietAction>
          </div>
        </div>
      )}

      <ul className="flex flex-col gap-2.5">
        {status.required.map((provider) => {
          // `missing` is the authoritative per-variable answer from Rust, so a
          // provider holding two of three values is neither satisfied nor absent.
          const satisfied = provider.local || provider.missing.length === 0;
          const isOpen = editing === provider.id;
          // Only outstanding providers open by default. A row the user has already
          // dealt with is one less thing between them and the button they want.
          const expanded = isOpen || !satisfied;
          const slots = credentialSlots(provider);
          const progress = slotProgress(slots);
          // The first field that still needs a value is the one that takes focus.
          // Every field competing for it meant the last one won, which put people
          // in the API secret box when the gate had asked for a server address.
          const focusEnv =
            slots.find((slot) => !slot.stored && !slot.inEnv)?.env ??
            slots[0]?.env ??
            null;

          return (
            <li
              key={provider.id}
              className={
                // Depth, not a border: a satisfied row sits back, an outstanding
                // one comes forward. That is the whole visual language here.
                "rounded-lg p-3.5 transition-shadow " +
                (satisfied ? "bg-surface-muted/60 shadow-elev-1" : "bg-surface shadow-elev-2")
              }
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <h3 className="text-[13.5px] font-medium text-foreground">{provider.label}</h3>
                    <StatusPill satisfied={satisfied} local={provider.local} progress={progress} />
                  </div>
                  <p className="mt-0.5 text-[12px] text-faint">{describeRole(provider)}</p>
                  {expanded && !provider.local && (
                    <p className="mt-1 text-[12px] text-soft">{describeAvailability(provider)}</p>
                  )}
                  {provider.keyEnv.length > 0 && (
                    <p className="mt-1 font-mono text-[11px] text-faint">
                      or set {provider.keyEnv.join(" / ")} in agent/.env
                    </p>
                  )}
                </div>

                <div className="flex shrink-0 items-center gap-1.5">
                  {provider.setupUrl && (
                    <QuietAction
                      onClick={() => {
                        // A `target="_blank"` anchor is unreliable in a webview,
                        // and the one button a first-run wizard cannot afford is a
                        // "get a key" button that does nothing.
                        void openUrl(provider.setupUrl!).catch(() => {
                          notify({ tone: "error", message: "Could not open the browser." });
                        });
                      }}
                    >
                      Get a key
                    </QuietAction>
                  )}
                  {!provider.local && slots.length > 0 && (
                    // Not gated on `slots.length > 1`. A provider that is already
                    // satisfied still has to be replaceable from here, and before
                    // this row existed every non-local provider carried an Add /
                    // Replace button. Gating the toggle on the multi-slot case
                    // quietly removed the ability to change a finished single-key
                    // credential without a trip to the settings overlay.
                    <QuietAction onClick={() => setEditing(isOpen ? null : provider.id)}>
                      {isOpen ? "Hide fields" : slots.length > 1 ? "Show fields" : "Replace key"}
                    </QuietAction>
                  )}
                </div>
              </div>

              {/*
                A multi-variable provider is open by default, and each card
                carries its own state.

                The previous version hid all of LiveKit's three fields behind one
                "Add key" button and closed the group after the first save. Two
                things went wrong from that, and both were reported as one bug:
                the save looked like it had stored the whole credential, and
                reopening the group showed three blank boxes because nothing on
                the page could say one of them was already filled.

                A provider that is already complete shows the same list compactly
                rather than as three empty inputs — the page is a wizard for what
                is missing, and a row of blank fields for a credential that is
                already there is noise that hides the row that is not.
              */}
              {!provider.local && (slots.length > 1 || isOpen || !satisfied) && (
                satisfied && !isOpen ? (
                  <SlotSummary slots={slots} />
                ) : (
                  <div className="setup-slots">
                    {slots.map((slot) => (
                      <SecretField
                        key={slot.env}
                        label={slot.label}
                        kind={slot.kind}
                        stored={slot.stored}
                        storedLast4={slot.storedLast4}
                        inEnv={slot.inEnv}
                        autoFocus={slot.env === focusEnv}
                        help={
                          slot.help ||
                          (provider.setupUrl
                            ? `Create one at ${provider.setupUrl.replace(/^https?:\/\//, "")}, then paste it here.`
                            : `Stored as ${slot.env} in your system credential store.`)
                        }
                        submitLabel={`Save ${slot.label}`}
                        busy={busy === provider.id}
                        error={busy === provider.id ? fieldError : null}
                        onCancel={() => {
                          setEditing(null);
                          setFieldError(null);
                        }}
                        onSubmit={(secret) => store(provider, slot.env, slot.label, secret)}
                      />
                    ))}
                  </div>
                )
              )}
            </li>
          );
        })}
      </ul>

      <footer className="mt-auto flex flex-wrap items-center gap-2 pt-1">
        <QuietAction onClick={onOpenProviders}>Open all settings</QuietAction>
        <span className="ml-auto flex items-center gap-2 text-[11.5px] text-faint">
          {owed === 0
            ? "This screen closes itself once every value is stored."
            : "Checking runs on its own after each save — this is only for a value set outside the app."}
          {loading && <span className="voice-spinner" style={{ width: 12, height: 12 }} />}
        </span>
      </footer>
    </section>
  );
}

/**
 * The state of a provider, in one word, with the count when there is a count.
 *
 * `2 of 3 set` next to "Needed" is the whole fix for the reported confusion: a
 * single word cannot distinguish "nothing stored" from "most of it stored", and
 * those two need very different amounts of work. The word answers *whether*; the
 * count answers *how much is left*.
 */
function StatusPill({
  satisfied,
  local,
  progress,
}: {
  satisfied: boolean;
  local: boolean;
  progress: { total: number; label: string };
}) {
  if (local) {
    return <Pill>On this device</Pill>;
  }
  return satisfied ? (
    progress.total > 1 ? (
      <Pill tone="ok">
        {progress.label}
      </Pill>
    ) : (
      <Pill tone="ok">Ready</Pill>
    )
  ) : progress.total > 1 ? (
    <Pill tone="warn">{progress.label}</Pill>
  ) : (
    <Pill tone="warn">Needed</Pill>
  );
}

/**
 * A completed provider's variables, as a row of ticks rather than as inputs.
 *
 * The inputs are write-only, so a stored value cannot be shown in one — but its
 * *name* and whether it is there can be, and that is the part somebody checking
 * "did my LiveKit key actually save?" is looking for. Three empty boxes in a
 * wizard are the wrong answer; three labelled ticks are the right one.
 */
function SlotSummary({ slots }: { slots: CredentialSlotStatus[] }) {
  return (
    <ul className="setup-slot-summary">
      {slots.map((slot) => {
        const done = slot.stored || slot.inEnv;
        return (
          <li key={slot.env} className={done ? "is-done" : "is-owed"}>
            <span className="setup-slot-mark w-1.5 h-1.5 shrink-0 rounded-full bg-accent" aria-hidden="true" />
            <span className="setup-slot-label text-soft">{slot.label}</span>
            <span className="setup-slot-state">
              {slot.stored
                ? slot.storedLast4
                  ? `Stored ····${slot.storedLast4}`
                  : "Stored"
                : slot.inEnv
                  ? "In agent/.env"
                  : "Needed"}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function Pill({ children, tone = "plain" }: { children: React.ReactNode; tone?: "plain" | "ok" | "warn" }) {
  const tones = {
    plain: "bg-surface-muted text-soft",
    ok: "bg-accent/12 text-accent",
    warn: "bg-danger-soft text-danger",
  } as const;
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[11px] font-medium tracking-wide ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

function PrimaryAction({
  children,
  onClick,
  disabled,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-sm bg-accent px-3.5 py-2 text-[13px] font-medium text-accent-foreground shadow-elev-1 transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:opacity-50"
    >
      {children}
    </button>
  );
}

function QuietAction({ children, onClick }: { children: React.ReactNode; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-sm bg-surface-muted px-3 py-2 text-[12.5px] font-medium text-soft shadow-elev-1 transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
    >
      {children}
    </button>
  );
}
