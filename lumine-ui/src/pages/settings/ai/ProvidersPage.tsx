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
 * What the UI knows about one provider's credential. A discriminated shape rather
 * than loose strings, so "stored" can never be confused with a provider that
 * simply needs no key.
 */
type ProviderStatus =
  | { kind: "loading" }
  | { kind: "unavailable" }
  | { kind: "local" }
  | { kind: "stored"; last4: string | null }
  | { kind: "absent" };

const STATUS_LABEL: Record<ProviderStatus["kind"], string> = {
  loading: "Checking…",
  unavailable: "Unavailable",
  local: "On device",
  stored: "Stored",
  absent: "Not set",
};

const STATUS_TONE: Record<ProviderStatus["kind"], string> = {
  loading: "pending",
  unavailable: "unknown",
  local: "ok",
  stored: "ok",
  absent: "missing",
};

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
        if (!provider.requiresKey) return [provider.id, { kind: "local" }];
        try {
          const status = await getCredentialStatus(provider.id);
          return [provider.id, status.present ? { kind: "stored", last4: status.last4 } : { kind: "absent" }];
        } catch {
          return [provider.id, { kind: "unavailable" }];
        }
      }),
    );
    setStatuses(Object.fromEntries(entries));
  }, [catalog]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const store = async (providerId: string, secret: string) => {
    setPending(providerId);
    setFieldError(null);
    try {
      await setCredential(providerId, secret);
      notify({ tone: "success", message: "Key stored in the system credential store." });
      setEditing(null);
      await refresh();
    } catch (cause) {
      const text = cause instanceof Error ? cause.message : "Could not store the key.";
      setFieldError(text);
    } finally {
      setPending(null);
    }
  };

  const remove = async (providerId: string) => {
    setPending(providerId);
    try {
      await deleteCredential(providerId);
      notify({ tone: "success", message: "Key removed." });
      setResults((current) => omit(current, providerId));
      await refresh();
    } catch (cause) {
      notify({
        tone: "error",
        message: cause instanceof Error ? cause.message : "Could not remove the key.",
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
      <SettingsPageHeader
        section="providers"
        description="Each service Lumine can speak through has its own credential, stored once and referenced by every profile."
      />

      {isEnvironmentBacked && (
        <p className="notice">
          Keys set in <code>agent/.env</code> still apply. A key stored here is handed to the worker when it
          starts, and takes over from <code>agent/.env</code> for that provider. Restart the worker after
          changing a key.
        </p>
      )}

      <div className="provider-list">
        {catalog.providers.map((provider) => {
          const status: ProviderStatus = statuses[provider.id] ?? { kind: "loading" };
          const stored = status.kind === "stored";
          return (
            <article className="provider-card" key={provider.id}>
              <header>
                <div>
                  <h2>{provider.label}</h2>
                  {provider.local && <span className="provider-tag">On device</span>}
                </div>
                <span className={`provider-status is-${STATUS_TONE[status.kind]}`}>
                  {STATUS_LABEL[status.kind]}
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
                <footer>
                  <small className="field-hint">
                    {stored && status.last4
                      ? `Key on file, ending in ${status.last4}`
                      : status.kind === "absent"
                        ? `The worker reads ${provider.keyEnv.join(", ")}`
                        : status.kind === "unavailable"
                          ? "The system credential store could not be reached."
                          : "Checking…"}
                  </small>
                  <div className="provider-actions">
                    <button
                      type="button"
                      className="settings-secondary"
                      onClick={() => setEditing(editing === provider.id ? null : provider.id)}
                      disabled={pending === provider.id || status.kind === "unavailable"}
                    >
                      {editing === provider.id ? "Close" : stored ? "Replace key" : "Add key"}
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
                    {stored && (
                      <button
                        type="button"
                        className="settings-quiet"
                        onClick={() => void remove(provider.id)}
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
              ) : (
                <footer>
                  <small className="field-hint">No credential needed. This runs locally on this machine.</small>
                </footer>
              )}

              {editing === provider.id && (
                <SecretField
                  label={`${provider.label} key`}
                  help={
                    provider.setupUrl
                      ? `Create one at ${provider.setupUrl.replace(/^https?:\/\//, "")}, then paste it here. It is stored in your system credential store and never shown again.`
                      : "Paste the key. It is stored in your system credential store and never shown again."
                  }
                  onSubmit={(secret) => store(provider.id, secret)}
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
