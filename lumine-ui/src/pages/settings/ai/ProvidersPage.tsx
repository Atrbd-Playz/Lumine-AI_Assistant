import { useCallback, useEffect, useState } from "react";
import type { ProviderCatalog } from "../../../features/settings/aiConfigTypes";
import { deleteCredential, getCredentialStatus, setCredential } from "../../../features/settings/aiConfigClient";
import { Icon } from "../../home/components/Icon";
import { SecretField } from "../components/SecretField";

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
  const [message, setMessage] = useState<{ tone: "success" | "error"; text: string } | null>(null);
  // Which provider's secret field is open. At most one, so two masked fields
  // never sit on screen together with the user unsure which is which.
  const [editing, setEditing] = useState<string | null>(null);

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
    setMessage(null);
    try {
      await setCredential(providerId, secret);
      setMessage({ tone: "success", text: "Key stored in the system credential store." });
      setEditing(null);
      await refresh();
    } catch (cause) {
      setMessage({ tone: "error", text: cause instanceof Error ? cause.message : "Could not store the key." });
    } finally {
      setPending(null);
    }
  };

  const remove = async (providerId: string) => {
    setPending(providerId);
    setMessage(null);
    try {
      await deleteCredential(providerId);
      setMessage({ tone: "success", text: "Key removed." });
      await refresh();
    } catch (cause) {
      setMessage({ tone: "error", text: cause instanceof Error ? cause.message : "Could not remove the key." });
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="settings-page">
      <header className="settings-page-head">
        <div>
          <p className="eyebrow">AI</p>
          <h1>Providers</h1>
          <p>Each service Lumine can speak through has its own credential, stored once and referenced by every profile.</p>
        </div>
      </header>

      {isEnvironmentBacked && (
        <p className="notice">
          Keys set in <code>agent/.env</code> still apply. A key stored here is handed to the worker when it
          starts, and takes over from <code>agent/.env</code> for that provider. Restart the worker after
          changing a key.
        </p>
      )}

      {message && (
        <p className={message.tone === "error" ? "validation is-error" : "validation is-ok"}>
          {message.text}
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
                  onCancel={() => setEditing(null)}
                  busy={pending === provider.id}
                  error={pending === provider.id ? null : (message?.tone === "error" ? message.text : null)}
                />
              )}
            </article>
          );
        })}
      </div>
    </div>
  );
}
