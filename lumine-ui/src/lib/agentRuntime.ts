import { useEffect, useRef, useState } from "react";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

/**
 * The desktop worker's event contract, and a hook that subscribes to it.
 *
 * ## This file was wrong, and being unreferenced is why
 *
 * It declared the events as `agent.started` / `agent.state_changed` and wrapped
 * each payload in `{ type, payload }`. Rust emits `agent_started` and friends
 * with the `AgentStatus` object *as* the payload. So every name was wrong and
 * every shape was wrong, and nothing noticed for one reason: no component
 * imported it. A type file that names an event nobody listens for is
 * indistinguishable from a type file nobody needs.
 *
 * Which is the argument for wiring it up rather than deleting it. Deleting the
 * file would have removed the only description of the contract, and the gap it
 * documents is real — the worker emits these and the app listened to none of
 * them, so a worker that died between two status polls was invisible.
 *
 * ## Two streams, not one
 *
 * * `agent_*` is the **desktop layer's** view: one child process, its lifecycle,
 *   and whether it is registered. Emitted by `agent_manager.rs`.
 * * `agent_runtime` is the **worker's own** `LUMINE_EVENT` stream, forwarded
 *   verbatim. Per-room and per-turn: the session connected, a tool ran, a turn
 *   failed. This is the stream that describes the conversation; the other one
 *   describes the process.
 *
 * Keeping them apart is what stops "the worker is running" from being read as
 * "Lumine is listening", which is the confusion that made the original agent
 * bug so hard to see from the UI.
 */

/** The states `agent_manager.rs` can report. Mirrors its `AgentState` enum. */
export type AgentState =
  | "booting"
  | "ready"
  | "sleeping"
  | "listening"
  | "thinking"
  | "speaking"
  | "error"
  | "shutting_down"
  | "stopped";

/** One worker's status, as both `get_agent_status` and every `agent_*` event carry it. */
export type AgentStatus = {
  state: AgentState | string;
  running: boolean;
  connected: boolean;
  pid: number | null;
  error: string | null;
};

/** Desktop-layer events. The payload *is* the `AgentStatus`. */
export const AGENT_EVENTS = [
  "agent_started",
  "agent_state_changed",
  "agent_stopped",
  "agent_error",
] as const;

/**
 * Worker events the UI acts on, out of the `LUMINE_EVENT` stream.
 *
 * Not the whole stream — the worker emits roughly twenty record types, and a
 * union that claims to be all of them would be wrong the next time one is added.
 * These are the ones with a consequence in the interface, and anything else
 * arrives as `AgentRuntimeEvent`'s open case.
 */
export type AgentRuntimeEvent = {
  /** `error`, `worker_registered`, `connected`, `user_state`, `tool_status`, … */
  type: string;
  /** Only on `error`. Never a secret. */
  message?: string;
  /** Where the error came from: `pipeline`, `session`, `config`, … */
  source?: string;
  timestamp?: number;
  [field: string]: unknown;
};

/**
 * Whether a state means the worker can serve a room.
 *
 * `connected` is the honest test rather than `running`: a process that is up but
 * has not registered with LiveKit cannot answer, and reporting that as ready is
 * exactly the misreport that made the original bug invisible.
 */
export function canServeRooms(status: AgentStatus | null): boolean {
  if (!status) return false;
  return status.running && status.connected && !status.error;
}

export type UseAgentRuntimeOptions = {
  /** Called for every `agent_error`, with the worker's own message. */
  onError?: (status: AgentStatus) => void;
  /** Called for every `agent_runtime` record. The whole stream, unfiltered. */
  onRuntime?: (event: AgentRuntimeEvent) => void;
};

/**
 * Subscribe to the worker's lifecycle.
 *
 * Returns the latest `AgentStatus`, or `null` until an event arrives. `null` is
 * the honest answer rather than a guess at "stopped": the app is showing an
 * empty state, and calling that `stopped` would claim a fact nobody has
 * established yet.
 *
 * ## Events report changes, not state
 *
 * A hook mounted against an already-healthy worker sees nothing at all, because
 * nothing has changed since it subscribed. So this is for *transitions* — the
 * crash that happens between two polls, the worker that stops on its own. A
 * screen that needs the current state on open reads it with `getAgentStatus`
 * and uses this for what happens next. Splitting the two is why the Diagnostics
 * page still polls: a poll is right for "how is it now" and useless for "it
 * just broke".
 *
 * Events are only registered once the app is running under Tauri. In a browser
 * there is no IPC and `listen` rejects, so a dev session in a plain tab would
 * otherwise log an unhandled rejection on every mount.
 *
 * ## Callbacks go in a ref, on purpose
 *
 * A caller writing `onError={(s) => notify(...)}` inline produces a new function
 * on every render. If the callbacks were effect dependencies, that would
 * re-subscribe five times a second and — worse — re-fire the error toast on
 * every render for as long as the worker stayed errored. Holding them in refs
 * means the subscription is mounted once and the callbacks are read at the
 * moment they are needed, which is the same seam `voice-manager.ts` uses.
 */
export function useAgentRuntime(options: UseAgentRuntimeOptions = {}): AgentStatus | null {
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const { onError, onRuntime } = options;

  const onErrorRef = useRef(onError);
  const onRuntimeRef = useRef(onRuntime);
  useEffect(() => {
    onErrorRef.current = onError;
    onRuntimeRef.current = onRuntime;
  }, [onError, onRuntime]);

  useEffect(() => {
    if (typeof window === "undefined" || !("__TAURI_INTERNALS__" in window)) return;

    let active = true;
    const unlisteners: UnlistenFn[] = [];

    const subscribe = async () => {
      try {
        // `agent_state_changed` carries every transition including start and
        // stop, so listening to it alone would be enough. The others are
        // registered anyway, so a first render after mounting mid-session is a
        // hole in the timeline rather than a gap in coverage.
        const subscriptions = await Promise.all([
          ...AGENT_EVENTS.map((event) =>
            listen<AgentStatus>(event, (received) => {
              if (active) setStatus(received.payload);
            }),
          ),
          listen<AgentRuntimeEvent>("agent_runtime", (received) => {
            if (active) onRuntimeRef.current?.(received.payload);
          }),
        ]);
        if (!active) {
          // Unmounted while the promises were in flight. Without this the
          // listeners stay attached to a hook nobody holds, which in dev tools
          // looks like the app still reacting to the worker.
          subscriptions.forEach((stop) => stop());
          return;
        }
        unlisteners.push(...subscriptions);
      } catch {
        // No IPC, or a command this build does not register. A screen that wants
        // the current state reads it with `getAgentStatus`; a missing listener is
        // not a crash, and throwing here would take down whichever screen mounted
        // the hook.
      }
    };

    void subscribe();
    return () => {
      active = false;
      unlisteners.forEach((stop) => stop());
    };
  }, []);

  useEffect(() => {
    // Only on a *change* of the error text. The same failure arrives as
    // `agent_error` and then again as `agent_state_changed`, so keying on the
    // message rather than on the presence of one shows it once — and an error
    // that persists across a re-render stays on screen instead of re-announcing
    // itself every frame.
    if (status?.error) onErrorRef.current?.(status);
  }, [status?.error]);

  return status;
}
