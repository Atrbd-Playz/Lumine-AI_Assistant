import { useEffect, useState } from "react";
import type { ProviderCatalog } from "../../../features/settings/aiConfigTypes";
import { getAgentStatus, type AgentStatus } from "../../../features/settings/aiConfigClient";
import { Icon } from "../../home/components/Icon";

type DiagnosticsPageProps = {
  catalog: ProviderCatalog;
  isEnvironmentBacked: boolean;
  validating: boolean;
  diagnostics: { severity: string; message: string; hint?: string }[];
};

const STATE_COPY: Record<string, string> = {
  sleeping: "The worker is not running. Start a voice session and Tauri will launch it.",
  booting: "The worker is starting and registering with LiveKit.",
  ready: "The worker is registered and waiting for a room.",
  listening: "A voice session is live.",
  thinking: "Lumine is composing a reply.",
  speaking: "Lumine is speaking.",
  error: "The worker reported an error.",
  shutting_down: "The worker is stopping.",
  stopped: "The worker has stopped.",
};

/**
 * Runtime health and configuration checks.
 *
 * Deliberately read-only. Anything here that mutates state belongs in the page
 * that owns it, so there is exactly one place a change can be made.
 */
export function DiagnosticsPage({ catalog, isEnvironmentBacked, validating, diagnostics }: DiagnosticsPageProps) {
  const [agent, setAgent] = useState<AgentStatus | null>(null);
  const [agentError, setAgentError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const poll = async () => {
      try {
        const status = await getAgentStatus();
        if (active) {
          setAgent(status);
          setAgentError(null);
        }
      } catch (cause) {
        if (active) setAgentError(cause instanceof Error ? cause.message : "Could not read worker status.");
      }
    };
    void poll();
    const timer = window.setInterval(poll, 2000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  const modelsByCapability = (capability: string) =>
    catalog.providers.reduce((total, provider) => total + provider.models.filter((model) => model.capability === capability).length, 0);

  const deprecated = catalog.providers.flatMap((provider) =>
    provider.models
      .filter((model) => model.status !== "available")
      .map((model) => ({ provider: provider.label, model: model.label, status: model.status })),
  );

  return (
    <div className="settings-page">
      <header className="settings-page-head">
        <div>
          <p className="eyebrow">System</p>
          <h1>Diagnostics</h1>
          <p>What Lumine currently knows about its own configuration and runtime.</p>
        </div>
      </header>

      <section className="settings-block">
        <div className="settings-block-head">
          <h2>Voice worker</h2>
          <p>The Python process that serves voice sessions.</p>
        </div>
        {agentError ? (
          <p className="validation is-error">{agentError}</p>
        ) : !agent ? (
          <p className="validation is-pending">Checking…</p>
        ) : (
          <div className="runtime-state">
            <span className={`provider-status is-${agent.running ? "ok" : "missing"}`}>
              {agent.state}
            </span>
            <p>{STATE_COPY[agent.state] ?? "Unknown state."}</p>
            {agent.pid && <small className="field-hint">Process {agent.pid}</small>}
            {agent.error && <p className="validation is-error">{agent.error}</p>}
          </div>
        )}
      </section>

      <section className="settings-block">
        <div className="settings-block-head">
          <h2>Configuration source</h2>
          <p>Which layer decides the active voice stack.</p>
        </div>
        <p className="validation is-ok">
          <Icon name="check" size={15} />
          {isEnvironmentBacked
            ? "agent/.env — nothing has been saved from the desktop app yet."
            : "A saved configuration is in charge. agent/.env is the fallback."}
        </p>
      </section>

      <section className="settings-block">
        <div className="settings-block-head">
          <h2>Catalog</h2>
          <p>Models Lumine knows how to configure.</p>
        </div>
        <ul className="fact-list">
          {(["stt", "llm", "tts", "realtime", "vad"] as const).map((capability) => (
            <li key={capability}>
              <span>{capability}</span>
              <strong>{modelsByCapability(capability)}</strong>
            </li>
          ))}
        </ul>
        {deprecated.length > 0 && (
          <div className="deprecated-list">
            <h3>Needs attention</h3>
            {deprecated.map((entry) => (
              <p key={`${entry.provider}-${entry.model}`} className="validation is-warn">
                <Icon name="clock" size={14} />
                {entry.provider} · {entry.model} is {entry.status}.
              </p>
            ))}
          </div>
        )}
      </section>

      <section className="settings-block">
        <div className="settings-block-head">
          <h2>Active configuration checks</h2>
          <p>Produced by the agent's own rules.</p>
        </div>
        {validating && <p className="validation is-pending">Checking…</p>}
        {!validating && diagnostics.length === 0 && (
          <p className="validation is-ok">
            <Icon name="check" size={15} /> Nothing to report.
          </p>
        )}
        {diagnostics.length > 0 && (
          <ul className="validation-list">
            {diagnostics.map((diagnostic, index) => (
              <li key={index} className={`validation is-${diagnostic.severity}`}>
                <Icon name={diagnostic.severity === "error" ? "close" : "clock"} size={14} />
                <span>
                  <strong>{diagnostic.message}</strong>
                  {diagnostic.hint && <em>{diagnostic.hint}</em>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
