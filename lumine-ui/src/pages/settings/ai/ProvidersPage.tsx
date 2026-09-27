import { useCallback, useEffect, useState } from "react";
import type { ProviderCatalog } from "../../../features/settings/aiConfigTypes";
import {
  deleteCredential,
  getCredentialStatus,
  setCredential,
  testProviderCredential,
  type ProbeOutcome,
} from "../../../features/settings/aiConfigClient";
import { Icon } from "../../home/components/Icon";
import { useNotify } from "../../../features/toast/useNotify";
import { SecretField } from "../components/SecretField";
import { SettingsPageHeader } from "../components/SettingsPageHeader";

/** How each verdict reads, and how much to trust it. */
const VERDICT: Record<ProbeOutcome["verdict"], { label: string; tone: string; help: string }> = {
  valid: {
    label: "Works",
    tone: "ok",
    help: "The provider accepted this key.",
  },
  rejected: {
    label: "Rejected",
    tone: "missing",
    help: "The provider refused this key. It may be wrong, revoked, or expired.",
  },
  inconclusive: {
    label: "Could not tell",
    tone: "unknown",
    help: "The provider could not be asked, or answered something that says nothing about the key. This is not a sign the key is bad.",
  },
  no_secret: {
    label: "No key to test",
    tone: "missing",
    help: "Nothing is set for this provider yet.",
  },
  no_probe: {
    label: "Not testable",
    tone: "unknown",
    help: "No cheap authenticated request is known for this provider, so its key cannot be checked from here.",
  },
};

function omit(record: Record<string, ProbeOutcome>, key: string): Record<string, ProbeOutcome> {
  const { [key]: _removed, ...rest } = record;
  return rest;
}

type ProvidersPageProps = {
  catalog: ProviderCatalog;
  isEnvironmentBacked: boolean;
};

/**
 * What the UI knows about one provider's credential, per variable.
 *
 * `slots` is keyed by environment variable name, so a provider with three values
 * has three independent entries and a partially-filled one is visible as
 * partially-filled. The previous shape was a single status per provider, which is
 * what let a LiveKit install with only an API key read as complete.
 */
type ProviderStatus = {
  /** Set when the OS credential store itself could not be reached. */
  kind: "loading" | "unavailable" | "local" | "ready";
  /** By variable name. A variable absent from this map has no value. */
  slots: Record<string, { present: boolean; last4: string | null }>;
};

const STATUS_LABEL: Record<ProviderStatus["kind"], string> = {
  loading: "Checking…",
  unavailable: "Unavailable",
  local: "On device",
  ready: "Stored",
};

const STATUS_TONE: Record<ProviderStatus["kind"], string> = {
  loading: "pending",
  unavailable: "unknown",
  local: "ok",
  ready: "ok",
};

/**
 * Collapse a provider's per-variable statuses into one headline state.
 *
 * `partial` is the case worth having: a provider needs every one of its values,
 * so two of LiveKit's three is not a credential that can connect. Reporting it as
 * either "Stored" or "Not set" is what made the original bug invisible.
 */
function summarise(status: ProviderStatus, keyEnv: string[]): "stored" | "partial" | "absent" {
  if (status.kind === "local") return "stored";
  if (status.kind !== "ready") return "absent";
  const present = keyEnv.filter((env) => status.slots[env]?.present).length;
  if (present === 0) return "absent";
  return present === keyEnv.length ? "stored" : "partial";
}

const SUMMARY_LABEL = {
  stored: "Stored",
  partial: "Incomplete",
  absent: "Not set",
} as const;

const SUMMARY_TONE = {
  stored: "ok",
  partial: "missing",
  absent: "missing",
} as const;

/**
 * Provider credentials.
 *
 * The contract with the backend is deliberately narrow: a secret can be written
 * or deleted, and its presence can be read. A stored secret can never be read
 * back into the webview, so this page has no code path that could display one.
 */
export function ProvidersPage({ catalog, isEnvironmentBacked }: ProvidersPageProps) {
  const [statuses, setStatuses] = useState<Record<string, ProviderStatus>>({});
  const [pending, setPending] = useState<string | null>(null);
  // Only the open secret field's failure lives here, because it has to sit next
  // to the input that caused it. Everything else is a transient outcome and goes
  // to the toast queue, where one result cannot overwrite another.
  const [fieldError, setFieldError] = useState<string | null>(null);
  const notify = useNotify();
  // At most one provider's secret field is open, so two masked fields never sit
  // on screen together with the user unsure which is which.
  const [editing, setEditing] = useState<string | null>(null);
  const [probing, setProbing] = useState<string | null>(null);
  // Connectivity results, kept per provider so a verdict stays visible next to
  // the key it is about.
  const [results, setResults] = useState<Record<string, ProbeOutcome>>({});

  const refresh = useCallback(async () => {
    const entries = await Promise.all(
      catalog.providers.map(async (provider): Promise<[string, ProviderStatus]> => {
        if (!provider.requiresKey) return [provider.id, { kind: "local", slots: {} }];
        try {
          // One read per variable. A provider with three values is asked about
          // each of them separately, which is the only way to tell "all three are
          // stored" from "one is, and it is the wrong one".
          const pairs = await Promise.all(
            provider.keySlots.map(async (slot): Promise<[string, { present: boolean; last4: string | null }]> => {
              const status = await getCredentialStatus(provider.id, slot.env);
              return [slot.env, { present: status.present, last4: status.last4 }];
            }),
          );
          return [provider.id, { kind: "ready", slots: Object.fromEntries(pairs) }];
        } catch {
          return [provider.id, { kind: "unavailable", slots: {} }];
        }
      }),
    );
    setStatuses(Object.fromEntries(entries));
  }, [catalog]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const store = async (providerId: string, secret: string, slot: string, label: string) => {
    setPending(providerId);
    setFieldError(null);
    try {
      await setCredential(providerId, secret, slot);
      notify({ tone: "success", message: `${label} stored in the system credential store.` });
      setEditing(null);
      await refresh();
    } catch (cause) {
      const text = cause instanceof Error ? cause.message : "Could not store the value.";
      setFieldError(text);
    } finally {
      setPending(null);
    }
  };

  const remove = async (providerId: string, slot: string, label: string) => {
    setPending(providerId);
    try {
      await deleteCredential(providerId, slot);
      notify({ tone: "success", message: `${label} removed.` });
      // A probe verdict was about the credential as a whole. Once one part is gone
      // the verdict describes something that no longer exists, so it is cleared
      // rather than left sitting next to a now-incomplete provider.
      setResults((current) => omit(current, providerId));
      await refresh();
    } catch (cause) {
      notify({
        tone: "error",
        message: cause instanceof Error ? cause.message : "Could not remove the value.",
      });
    } finally {
      setPending(null);
    }
  };

  /**
   * Make one authenticated request to see whether a key works.
   *
   * "Stored" comes from the OS keyring and says nothing about validity, which is
   * why this exists. The result is kept per provider so it survives a re-render,
   * and a re-test replaces rather than appends.
   */
  const test = async (providerId: string) => {
    setProbing(providerId);
    try {
      const outcome = await testProviderCredential(providerId);
      setResults((current) => ({ ...current, [providerId]: outcome }));
    } catch (cause) {
      notify({
        tone: "error",
        message: cause instanceof Error ? cause.message : "Could not run the check.",
      });
    } finally {
      setProbing(null);
    }
  };

  return (
    <div className="settings-page">
      <SettingsPageHeader section="providers" />

      {isEnvironmentBacked && (
        <p className="notice">
          Keys set in <code>agent/.env</code> still apply. A key stored here is handed to the worker when it
          starts, and takes over from <code>agent/.env</code> for that provider. Restart the worker after
          changing a key.
        </p>
      )}

      <div className="provider-list">
        {catalog.providers.map((provider) => {
          const status: ProviderStatus = statuses[provider.id] ?? { kind: "loading", slots: {} };
          const summary = summarise(status, provider.keyEnv);
          const multiple = provider.keySlots.length > 1;
          return (
            <article className="provider-card" key={provider.id}>
              <header>
                <div>
                  <h2>{provider.label}</h2>
                  {provider.local && <span className="provider-tag">On device</span>}
                </div>
                <span
                  className={`provider-status is-${status.kind === "ready" ? SUMMARY_TONE[summary] : STATUS_TONE[status.kind]}`}
                >
                  {status.kind === "ready" ? SUMMARY_LABEL[summary] : STATUS_LABEL[status.kind]}
                </span>
              </header>

              {provider.capabilities.length > 0 && (
                <div className="provider-caps">
                  {provider.capabilities.map((capability) => (
                    <span key={capability} className="provider-cap">
                      {capability}
                    </span>
                  ))}
                </div>
              )}

              {provider.notes && <p className="provider-note">{provider.notes}</p>}

              {provider.requiresKey ? (
                <>
                  {/*
                    A provider with several values gets a row per value. This is
                    the shape the original bug needed and did not have: one field
                    named "LiveKit key" cannot hold a URL, a key and a secret, and
                    the worker used to receive the same value in all three.
                  */}
                  {multiple ? (
                    <ul className="key-slots">
                      {provider.keySlots.map((slot) => {
                        const slotStatus = status.slots[slot.env];
                        return (
                          <li key={slot.env} className="key-slot">
                            <div className="key-slot-text">
                              <span className="field-label">{slot.label}</span>
                              <small className="field-hint">
                                {status.kind === "unavailable"
                                  ? "The system credential store could not be reached."
                                  : status.kind === "loading"
                                    ? "Checking…"
                                    : slotStatus?.present
                                      ? `Set${slotStatus.last4 ? `, ending in ${slotStatus.last4}` : ""}`
                                      : `Not set — reads ${slot.env}`}
                              </small>
                            </div>
                            <div className="provider-actions">
                              <button
                                type="button"
                                className="settings-secondary"
                                onClick={() =>
                                  setEditing(editing === slotKey(provider.id, slot.env) ? null : slotKey(provider.id, slot.env))
                                }
                                disabled={pending === provider.id || status.kind === "unavailable"}
                              >
                                {editing === slotKey(provider.id, slot.env)
                                  ? "Close"
                                  : slotStatus?.present
                                    ? "Replace"
                                    : "Add"}
                              </button>
                              {slotStatus?.present && (
                                <button
                                  type="button"
                                  className="settings-quiet"
                                  onClick={() => void remove(provider.id, slot.env, slot.label)}
                                  disabled={pending === provider.id}
                                >
                                  Remove
                                </button>
                              )}
                            </div>
                            {editing === slotKey(provider.id, slot.env) && (
                              <SecretField
                                label={slot.label}
                                kind={slot.kind}
                                // Stated per variable, so this row and the
                                // quickstart row for the same value cannot
                                // disagree about whether it is already there.
                                stored={slotStatus?.present ?? false}
                                storedLast4={slotStatus?.last4 ?? null}
                                help={
                                  slot.help ||
                                  (provider.setupUrl
                                    ? `From ${provider.setupUrl.replace(/^https?:\/\//, "")}. Stored in your system credential store and never shown again.`
                                    : "Stored in your system credential store and never shown again.")
                                }
                                onSubmit={(secret) => store(provider.id, secret, slot.env, slot.label)}
                                onCancel={() => {
                                  setEditing(null);
                                  setFieldError(null);
                                }}
                                busy={pending === provider.id}
                                error={pending === provider.id ? null : fieldError}
                              />
                            )}
                          </li>
                        );
                      })}
                    </ul>
                  ) : (
                    <footer>
                      <small className="field-hint">
                        {status.kind === "unavailable"
                          ? "The system credential store could not be reached."
                          : status.kind === "loading"
                            ? "Checking…"
                            : status.slots[provider.keyEnv[0]]?.present
                              ? `Key on file, ending in ${status.slots[provider.keyEnv[0]]?.last4 ?? ""}`
                              : `The worker reads ${provider.keyEnv.join(", ")}`}
                      </small>
                      <div className="provider-actions">
                        <button
                          type="button"
                          className="settings-secondary"
                          onClick={() => setEditing(editing === provider.id ? null : provider.id)}
                          disabled={pending === provider.id || status.kind === "unavailable"}
                        >
                          {editing === provider.id ? "Close" : summary === "stored" ? "Replace key" : "Add key"}
                        </button>
                        {provider.probe && (
                          <button
                            type="button"
                            className="settings-secondary"
                            onClick={() => void test(provider.id)}
                            disabled={probing === provider.id}
                            title="Make one authenticated request to check this key works"
                          >
                            {probing === provider.id ? "Testing…" : "Test"}
                          </button>
                        )}
                        {summary !== "absent" && (
                          <button
                            type="button"
                            className="settings-quiet"
                            onClick={() => void remove(provider.id, provider.keyEnv[0], `${provider.label} key`)}
                            disabled={pending === provider.id}
                          >
                            Remove
                          </button>
                        )}
                        {provider.setupUrl && (
                          <a className="provider-link" href={provider.setupUrl} target="_blank" rel="noreferrer">
                            Get a key
                            <Icon name="spark" size={12} />
                          </a>
                        )}
                      </div>
                      {results[provider.id] && <ProbeResult outcome={results[provider.id]} />}
                    </footer>
                  )}

                  {/*
                    Test and Get-a-key belong to the credential as a whole, so they
                    sit below the rows rather than inside one. For a single-value
                    provider they stay in the footer above.
                  */}
                  {multiple && (
                    <footer>
                      <div className="provider-actions">
                        {provider.probe && (
                          <button
                            type="button"
                            className="settings-secondary"
                            onClick={() => void test(provider.id)}
                            disabled={probing === provider.id}
                            title="Check all of this provider's values together"
                          >
                            {probing === provider.id ? "Testing…" : "Test all values"}
                          </button>
                        )}
                        {provider.setupUrl && (
                          <a className="provider-link" href={provider.setupUrl} target="_blank" rel="noreferrer">
                            Get a key
                            <Icon name="spark" size={12} />
                          </a>
                        )}
                      </div>
                      {results[provider.id] && <ProbeResult outcome={results[provider.id]} />}
                    </footer>
                  )}
                </>
              ) : (
                <footer>
                  <small className="field-hint">No credential needed. This runs locally on this machine.</small>
                </footer>
              )}

              {!multiple && editing === provider.id && provider.requiresKey && (
                <SecretField
                  label={`${provider.label} key`}
                  help={
                    provider.setupUrl
                      ? `Create one at ${provider.setupUrl.replace(/^https?:\/\//, "")}, then paste it here. It is stored in your system credential store and never shown again.`
                      : "Paste the key. It is stored in your system credential store and never shown again."
                  }
                  onSubmit={(secret) => store(provider.id, secret, provider.keyEnv[0], `${provider.label} key`)}
                  onCancel={() => {
                    setEditing(null);
                    setFieldError(null);
                  }}
                  busy={pending === provider.id}
                  error={pending === provider.id ? null : fieldError}
                />
              )}
            </article>
          );
        })}
      </div>
    </div>
  );
}

/**
 * Identity of one open credential field.
 *
 * A provider with three values needs three independent fields, and at most one is
 * open at a time so two masked inputs never sit on screen with the user unsure
 * which is which. The slot name is part of the key for exactly that reason.
 */
function slotKey(providerId: string, env: string): string {
  return `${providerId}:${env}`;
}

/** One connectivity verdict, next to the key it is about. */
function ProbeResult({ outcome }: { outcome: ProbeOutcome }) {
  const verdict = VERDICT[outcome.verdict] ?? VERDICT.inconclusive;
  return (
    <div className={`probe-result is-${verdict.tone}`}>
      <span className="probe-verdict">{verdict.label}</span>
      <span className="field-hint">
        {outcome.detail ? `${verdict.help} ${outcome.detail}` : verdict.help}
        {outcome.latencyMs !== undefined && ` (${outcome.latencyMs} ms)`}
        {outcome.status !== undefined && ` · HTTP ${outcome.status}`}
      </span>
    </div>
  );
}
