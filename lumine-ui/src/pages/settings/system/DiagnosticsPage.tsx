import { useEffect, useState } from "react";
import type { ProviderCatalog } from "../../../features/settings/aiConfigTypes";
import { getAgentStatus } from "../../../features/settings/aiConfigClient";
import { useAgentRuntime, type AgentStatus } from "../../../lib/agentRuntime";
import { Icon } from "../../home/components/Icon";
import { Hint } from "../../../components/ui/hint";
import { SettingsPageHeader } from "../components/SettingsPageHeader";
import { CapabilityMatrix } from "./CapabilityMatrix";

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
 *
 * ## Both a poll and a listener, on purpose
 *
 * The worker block reads `get_agent_status` once on open and then follows the
 * `agent_*` events. That split is not redundancy — a poll is the right tool for
 * "how is it right now" and useless for "it just broke", because a process that
 * boots and dies between two two-second reads is simply never seen. The event
 * is the only thing that reports the transition. The reverse holds too: events
 * report changes, so a screen opened against a healthy worker would show
 * nothing at all without the initial read.
 */
export function DiagnosticsPage({ catalog, isEnvironmentBacked, validating, diagnostics }: DiagnosticsPageProps) {
  const [polled, setPolled] = useState<AgentStatus | null>(null);
  const [agentError, setAgentError] = useState<string | null>(null);
  const live = useAgentRuntime();

  useEffect(() => {
    let active = true;
    const read = async () => {
      try {
        const status = await getAgentStatus();
        if (active) {
          setPolled(status);
          setAgentError(null);
        }
      } catch (cause) {
        if (active) setAgentError(cause instanceof Error ? cause.message : "Could not read worker status.");
      }
    };
    void read();
    return () => {
      active = false;
    };
  }, []);

  // A live event is newer than anything the read produced, so it wins. Once the
  // worker is back to normal the poll takes over again, which is what stops a
  // stale errored reading from sticking to the screen after a restart.
  const agent = live ?? polled;

  return (
    <div className="settings-page">
      <SettingsPageHeader section="diagnostics" />

      <section className="settings-block">
        <div className="settings-block-head flex items-center gap-1.5">
          <h2>Voice worker</h2>
          <Hint label="The Python process">The process that serves voice sessions. It registers with LiveKit, then waits to be put in a room.</Hint>
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
            {/* Running is not serving. The process can be alive while its LiveKit
                registration is missing, which is precisely the state that made
                the original agent bug invisible, so the two are reported apart
                rather than collapsed into one word. */}
            <p className={agent.connected ? "validation is-ok" : "validation is-pending"}>
              <Icon name={agent.connected ? "check" : "clock"} size={14} />
              {agent.connected
                ? "Registered with LiveKit. A room dispatched to Lumine will reach her."
                : "Not registered with LiveKit. Starting a call will launch the worker first."}
            </p>
            {agent.pid && <small className="field-hint">Process {agent.pid}</small>}
            {agent.error && <p className="validation is-error">{agent.error}</p>}
          </div>
        )}
      </section>

      <section className="settings-block">
        <div className="settings-block-head flex items-center gap-1.5">
          <h2>Configuration source</h2>
          <Hint label="Which layer wins">
            A saved configuration takes precedence. `agent/.env` is read only when
            nothing has been saved from the desktop app.
          </Hint>
        </div>
        <p className="validation is-ok">
          <Icon name="check" size={15} />
          {isEnvironmentBacked
            ? "agent/.env — nothing has been saved from the desktop app yet."
            : "A saved configuration is in charge. agent/.env is the fallback."}
        </p>
      </section>

      <section className="settings-block">
        <div className="settings-block-head flex items-center gap-1.5">
          <h2>What each model can do</h2>
          <Hint label="How to read this">
            A dot means Lumine can hand that model that kind of input on the path it
            is reached by — not that the model could accept it in principle. The
            language stage sends a chat history of strings, so a camera turned on
            beside a pipeline stage delivers frames to nobody; the Live models are
            the only ones with somewhere to put one.
          </Hint>
        </div>
        <CapabilityMatrix catalog={catalog} />
      </section>

      <section className="settings-block">
        <div className="settings-block-head flex items-center gap-1.5">
          <h2>Active configuration checks</h2>
          <Hint label="Who writes these">
            The agent's own rules, run against the profile as it would be started.
            A blocking result here is what stops the voice button working.
          </Hint>
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
