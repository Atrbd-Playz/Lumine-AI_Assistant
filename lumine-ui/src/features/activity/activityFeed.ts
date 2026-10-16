import { useCallback, useEffect, useRef, useState } from "react";
import { useAgentRuntime, type AgentRuntimeEvent } from "../../lib/agentRuntime";

/**
 * The Activity timeline, built on the worker's own `agent_runtime` stream.
 *
 * ## The stream was already there, and nothing read it
 *
 * `useAgentRuntime` has forwarded every `LUMINE_EVENT` record to an `onRuntime`
 * callback since it was written, and `Home.tsx` passed a callback that did
 * nothing with it. Meanwhile the Activity page drew a hardcoded "morning
 * standup", a "3 tasks" row and a "12 notes indexed" row — numbers that were
 * invented in the component, and therefore could not disagree with reality
 * because they were never about reality.
 *
 * So this is not a new event channel. It is the existing one, finally rendered.
 *
 * ## The filter, and why it is a filter
 *
 * The worker emits about twenty record types. Rendering all of them produces a
 * wall of latency marks and speech bookkeeping that nobody reads, and a wall of
 * things nobody reads is worse than nothing: it teaches people that this panel
 * is noise.
 *
 * The rule is **only what the user cannot otherwise see.** That rules out:
 *
 * - `conversation` — the transcript already has it, in the words that were said.
 * - `user_state` — the avatar is already the live display of this. A timeline
 *   row saying "you started speaking" duplicates the face and adds nothing.
 * - `speech_created` — same reason.
 * - `worker_registered` and the other process-level marks — `AgentState` is
 *   already the desktop layer's view of those, and the Diagnostics page is
 *   where that belongs.
 *
 * What survives is the interesting asymmetry: **the things that went wrong, the
 * things that cost time, and the things the app did.** A failure the user cannot
 * perceive is indistinguishable from a broken microphone, which is the exact
 * failure mode that made the original agent bug invisible from the UI.
 *
 * ## Timestamps are unix seconds, and they are floats
 *
 * `ToolEventBridge._publish` does `record.setdefault("timestamp", time.time())`,
 * so a timestamp is seconds since the epoch as a float — not milliseconds, and
 * not an ISO string. `AgentRuntimeEvent.timestamp` is typed `number` for that
 * reason. Treating it as ms renders every row at 1970; treating it as an integer
 * rounds a genuine sub-second resolution to nothing, and a 40 ms stutter and a
 * 400 ms one both read as "instant" if you truncate to seconds.
 *
 * When a record carries no timestamp — and `emit()` does not add one, so
 * `connected`, `error`, `session_started` and the rest all lack it — the local
 * clock is used, because the record arrived now and that is the only honest
 * answer available.
 */

/** What an entry is *about*, which decides its glyph and its colour band. */
export type ActivityKind = "session" | "config" | "tool" | "latency" | "failure" | "note";

/** How much attention an entry deserves. Not severity — the two differ. */
export type ActivityTone = "neutral" | "good" | "warn" | "bad";

export type ActivityEntry = {
  id: string;
  kind: ActivityKind;
  tone: ActivityTone;
  /** One line, plain words, no wire format. */
  title: string;
  /** The part that is actually worth reading. Absent rather than empty. */
  detail?: string;
  /** Milliseconds, for the rows that have a measurement. */
  ms?: number;
  /** Epoch milliseconds. Local clock when the record carried none. */
  at: number;
};

export type ActivityFeed = {
  entries: readonly ActivityEntry[];
  clear: () => void;
};

/**
 * How many entries are kept.
 *
 * Bounded, because this is a live view rather than an audit log — an unbounded
 * array in React state is a leak that only shows up once the window has been open
 * long enough for the user to have stopped noticing.
 *
 * It was 200, and that was the reported bug: *"the activity logs are capped to
 * few logs."* 200 sounds generous until you count what a turn produces. From one
 * real session's `LUMINE_EVENT` stream, a **single** user turn emits ten `latency`
 * records (`agent_state`, `llm_first_token`, `llm_request`, `tool_exec`,
 * `turn_end_to_end`…), a `session_usage`, a `conversation` pair and four `emotion`
 * records. 200 entries is therefore about **twenty turns** — so the Activity page
 * silently lost everything from earlier in the same session, and there was no
 * indication it had.
 *
 * 2,000 is about two hundred turns, which is the whole of a working day on one
 * machine, and it still costs nothing to hold: an entry is a handful of short
 * strings, so the ceiling here is measured in a few megabytes of worst case.
 */
const MAX_ENTRIES = 2000;

/**
 * Where the entries survive a reload.
 *
 * **This is the other half of "capped to few logs".** The feed lived in React
 * state only, so closing the app discarded it — and since the worker registers
 * fresh on every launch, reopening the app produced an Activity page with a
 * handful of rows from the last few seconds and no way to ask what had happened
 * before that. A log that vanishes on quit is not a log.
 *
 * `localStorage` rather than a Tauri command on purpose: it is synchronous, so
 * restored rows are on screen in the *first* paint of the Activity page rather
 * than a frame later, and there is no IPC round trip on a page that is meant to
 * be the record of what just happened.
 *
 * Every access is guarded. `localStorage` throws in a private window and when the
 * quota is exhausted, and a telemetry view must not be the thing that takes the
 * app down — the same rule `runtime_events.write_event_line` follows on the other
 * side of the pipe.
 */
const STORAGE_KEY = "lumine.activity.v1";

/** Read the saved rows, or `[]` for anything unreadable. Never throws. */
function loadPersisted(): ActivityEntry[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Filtered rather than trusted: a hand-edited or half-written store should
    // cost the rows it got wrong, not the whole history.
    return parsed.filter(isActivityEntry).slice(-MAX_ENTRIES);
  } catch {
    return [];
  }
}

/** Write the rows, best-effort. A full quota is not this page's problem. */
function persistPersisted(entries: readonly ActivityEntry[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(entries.slice(-MAX_ENTRIES)));
  } catch {
    /* Private window, or quota. The in-memory feed is still correct. */
  }
}

/** Whether a stored value has the shape an entry needs. */
function isActivityEntry(value: unknown): value is ActivityEntry {
  if (typeof value !== "object" || value === null) return false;
  const entry = value as Partial<ActivityEntry>;
  return (
    typeof entry.id === "string" &&
    typeof entry.at === "number" &&
    typeof entry.kind === "string" &&
    typeof entry.title === "string"
  );
}

/** Monotonic, so two records in the same millisecond do not collide on key. */
let sequence = 0;
const nextId = () => `activity-${Date.now().toString(36)}-${(sequence++).toString(36)}`;

/** Reads a field as a trimmed string, or `undefined` when absent or blank. */
function text(event: AgentRuntimeEvent, field: string): string | undefined {
  const value = event[field];
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : undefined;
  }
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return undefined;
}

/** Reads a field as a finite number, or `undefined`. */
function num(event: AgentRuntimeEvent, field: string): number | undefined {
  const value = event[field];
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return undefined;
}

/** Joins the present parts with a middle dot, dropping the ones that are absent. */
function join(...parts: (string | undefined)[]): string | undefined {
  const present = parts.filter((part): part is string => Boolean(part));
  return present.length > 0 ? present.join(" · ") : undefined;
}

/** Milliseconds, from a seconds-based unix timestamp. */
function stampFrom(event: AgentRuntimeEvent): number {
  const raw = num(event, "timestamp");
  if (raw === undefined) return Date.now();
  // Heuristic, and a deliberate one: a value below this cannot be a plausible
  // millisecond timestamp for any date after 1970-01-02, so it is seconds.
  // Anything larger is taken as already being in milliseconds, which keeps a
  // future agent that switches units from rendering every row at 1970.
  return raw < 1e11 ? Math.round(raw * 1000) : Math.round(raw);
}

/**
 * `config_applied` / `config_rejected` — the profile the session actually got.
 *
 * Shown because the desktop app can write a saved profile that the worker then
 * rejects, and it falls back to the environment without saying so in the UI. A
 * user whose carefully chosen voice never applied had no way to find that out.
 */
function classifyConfig(event: AgentRuntimeEvent): ActivityEntry {
  const base = { id: nextId(), kind: "config" as const, at: stampFrom(event) };
  if (event.type === "config_rejected") {
    // `reason` first, then the diagnostics' own messages, then what it fell back
    // to. The reason is the only line that tells somebody what to *do*, and this
    // panel used to render a bare "Saved profile rejected" with a file path: the
    // worker had put the cause in `diagnostics`, which nothing here read, so a
    // profile that was rejected for a missing API key looked identical to one
    // that was rejected for a typo -- and the fallback voice that replaced it was
    // not mentioned at all.
    const diagnostics = Array.isArray(event.diagnostics) ? event.diagnostics : [];
    const causes = diagnostics
      .map((entry) => (entry && typeof entry === "object" ? String(entry.message ?? "") : ""))
      .filter(Boolean);
    const fellBackTo = text(event, "fell_back_to");
    const detail =
      join(text(event, "reason"), ...causes, fellBackTo) ??
      join(text(event, "config_path")) ??
      "Falling back to the environment";
    return {
      ...base,
      tone: "bad",
      title: "Saved profile not used",
      detail: fellBackTo ? `${detail} — using ${fellBackTo} instead` : detail,
    };
  }
  return {
    ...base,
    tone: "neutral",
    title: event.source === "ui" ? "Saved profile applied" : "Profile built from the environment",
    detail: join(text(event, "profile"), text(event, "model"), text(event, "config_path")),
  };
}

/**
 * `tool_status` — a tool call started, completed or failed, *or* one step
 * inside a multi-stage call.
 *
 * The step rows are what make a slow call followable. A staged WhatsApp send is
 * six things over several seconds, and without them the panel shows "running
 * send whatsapp" and then nothing — which is the exact shape of a hang. Each step
 * is keyed by `tool.action` so the `started` and `done` records for one step
 * collapse into a single row that ticks off, and `index`/`total` are carried
 * through because a step labelled "3 of 6" answers "how far along is this"
 * without the reader counting.
 *
 * `summary` is the worker's own one-line description of the outcome, with no
 * result text in it — that is what it is for. The `result` field is present in
 * this payload and is deliberately not read: a retrieved page rendered in a
 * timeline reads as though the model had been handed it, which is the shape a
 * prompt injection wants. The payload goes to the toast, which is transient and
 * not rendered.
 */
function classifyTool(event: AgentRuntimeEvent): ActivityEntry {
  const status = text(event, "status") ?? "started";
  const name = text(event, "tool") ?? text(event, "name") ?? "tool";
  const action = text(event, "action");
  const label = (name.replace(/_/g, " ") + (action ? ` · ${action.replace(/_/g, " ")}` : ""));
  const base = { id: nextId(), kind: "tool" as const, at: stampFrom(event) };
  const summary = text(event, "summary");
  const ms = num(event, "duration_ms");
  const index = num(event, "index");
  const total = num(event, "total");
  const position = index && total ? ` (${index} of ${total})` : "";

  // A step, not the call. `status: "done"` is how a finished step closes; the
  // call itself ends with "completed", and conflating the two would make the
  // panel say a tool finished while it was still typing.
  if (action) {
    const message = text(event, "message") ?? summary;
    if (status === "failed") {
      return { ...base, tone: "bad", title: `${label} failed`, detail: summary, ms };
    }
    return {
      ...base,
      tone: ms && ms > 1000 ? "warn" : "neutral",
      title: `${label}${position}`,
      detail: message,
      ms,
    };
  }

  if (status === "started") return { ...base, tone: "neutral", title: `Running ${label}` };
  if (status === "failed") {
    return { ...base, tone: "bad", title: `${label} failed`, detail: summary, ms };
  }
  return { ...base, tone: "good", title: `${label} finished`, detail: summary, ms };
}

/**
 * `latency` — the worker's own stage timings.
 *
 * This is the single most useful row in the panel and it is why the timeline is
 * worth building at all. Before this, a slow answer was indistinguishable from
 * a slow provider: the UI could see the request go out and the reply arrive, and
 * nothing in between.
 */
function classifyLatency(event: AgentRuntimeEvent): ActivityEntry | null {
  const stage = text(event, "stage") ?? "unknown";
  const ms = num(event, "elapsed_ms");
  // `mark` with no stage is a counter, not a duration — `latency.py` also emits
  // marker events, and rendering a marker as "0 ms" invents a measurement.
  if (ms === undefined) return null;
  // Over a second of agent-side work is slow enough that a person would have
  // noticed. The band is the point: a raw number nobody has a reference for is
  // the same as no number.
  const tone: ActivityTone = ms > 1000 ? "warn" : "neutral";
  return {
    id: nextId(),
    kind: "latency",
    tone,
    title: `${stage.replace(/_/g, " ")} took ${ms} ms`,
    detail: join(text(event, "state"), text(event, "profile"), text(event, "model")),
    ms,
    at: stampFrom(event),
  };
}

/** `session_usage` — the token cost of the call, if the shape is one we can read. */
function classifyUsage(event: AgentRuntimeEvent): ActivityEntry | null {
  const raw = event.usage;
  if (!Array.isArray(raw) || raw.length === 0) return null;
  let total = 0;
  let found = false;
  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null) continue;
    const record = entry as Record<string, unknown>;
    const tokens = record.tokens;
    if (typeof tokens === "object" && tokens !== null) {
      const bucket = tokens as Record<string, unknown>;
      for (const value of Object.values(bucket)) {
        if (typeof value === "number" && Number.isFinite(value)) {
          total += value;
          found = true;
        }
      }
    }
  }
  if (!found) return null;
  return {
    id: nextId(),
    kind: "latency",
    tone: "neutral",
    title: `${total.toLocaleString()} tokens used`,
    at: stampFrom(event),
  };
}

/**
 * Turn one worker record into a timeline entry, or `null` if it has none.
 *
 * Exported and pure so it can be reasoned about — and tested — without a Tauri
 * IPC channel, a running worker, or a React tree. The hook below is the only
 * part that touches the world.
 */
export function classifyActivity(event: AgentRuntimeEvent): ActivityEntry | null {
  const base = { id: nextId(), at: stampFrom(event) };

  switch (event.type) {
    // --- session lifecycle: the shape of a call, start to finish -------------
    case "connected":
      return { ...base, kind: "session", tone: "neutral", title: "Room connected", detail: text(event, "room") };
    case "session_started":
      return {
        ...base,
        kind: "session",
        tone: "good",
        title: "Session started",
        detail: join(text(event, "profile"), text(event, "model"), text(event, "interruption_mode")),
      };
    case "session_ending":
    case "session_ended":
      return { ...base, kind: "session", tone: "neutral", title: "Session ended", detail: text(event, "reason") };
    case "session_close": {
      // A close is routine when the room ends and worth knowing when it does not.
      const reason = text(event, "reason") ?? "unknown";
      const routine = reason === "normal" || reason === "client_initiated";
      return {
        ...base,
        kind: "session",
        tone: routine ? "neutral" : "warn",
        title: "Session closed",
        detail: reason,
      };
    }

    // --- configuration -----------------------------------------------------
    case "config_applied":
    case "config_rejected":
      return classifyConfig(event);

    // --- tools -------------------------------------------------------------
    case "tool_status":
      return classifyTool(event);

    // --- timing ------------------------------------------------------------
    case "latency":
      return classifyLatency(event);
    case "session_usage":
      return classifyUsage(event);

    // --- failures ----------------------------------------------------------
    case "error":
      return {
        ...base,
        kind: "failure",
        tone: "bad",
        title: text(event, "source") ? `Failure in ${text(event, "source")}` : "Failure",
        detail: text(event, "message"),
      };
    case "retry_after":
      return {
        ...base,
        kind: "failure",
        tone: "warn",
        title: "Backing off before trying again",
        detail: num(event, "seconds") !== undefined ? `${num(event, "seconds")}s` : undefined,
      };

    // --- the ones that are worth a line -------------------------------------
    case "context_trimmed": {
      const before = num(event, "items_before");
      const after = num(event, "items_after");
      return {
        ...base,
        kind: "note",
        tone: "warn",
        title: "Conversation trimmed to fit",
        detail: join(
          before !== undefined && after !== undefined ? `${before} → ${after} items` : undefined,
          num(event, "limit") !== undefined ? `limit ${num(event, "limit")}` : undefined,
        ),
      };
    }
    case "false_interruption":
      return { ...base, kind: "note", tone: "warn", title: "False interruption", detail: event.resumed ? "Resumed" : undefined };
    case "speech_finished":
      // Only the interrupted ones. A completed utterance is the normal case and
      // the transcript heard it; an interrupted one means a barge-in was
      // detected, which is the thing that is worth knowing.
      return event.interrupted === true
        ? { ...base, kind: "note", tone: "neutral", title: "Reply cut short", detail: text(event, "speech_id") }
        : null;

    // --- sight ---------------------------------------------------------------
    // The worker's own account of what it is receiving, and only the rows a
    // person could not have seen for themselves. Two reasons keep most of them
    // out: `frame` is a heartbeat published every few seconds while sharing, and
    // `idle` is the session baseline — a timeline that ticks on its own is a
    // timeline that gets read as noise, which is the same argument the file
    // header makes for filtering the other twenty record types.
    case "visual": {
      const reason = text(event, "reason");
      const source = text(event, "source");
      const label = source === "camera" ? "the camera" : source === "screen" ? "the screen" : "a screen";

      if (reason === "capability") {
        // `can_see: true` is the normal case, once per session, and it is not
        // news. `false` is the whole of a silent failure: every frame is
        // discarded by the provider with no error, so nothing else in this
        // system would ever say so.
        return event.can_see === false
          ? { ...base, kind: "note", tone: "warn", title: "This model cannot read frames" }
          : null;
      }
      if (reason === "error") {
        // A withdrawal also publishes under this reason, with nothing in it.
        // "Capture failed" for a failure that just cleared would be a row
        // asserting something that stopped being true.
        const message = text(event, "error");
        return message ? { ...base, kind: "failure", tone: "bad", title: "Capture failed", detail: message } : null;
      }
      if (reason === "permission") {
        const state = text(event, "permission");
        if (state === "denied") return { ...base, kind: "failure", tone: "bad", title: "Capture permission refused" };
        if (state === "enabled") return { ...base, kind: "note", tone: "good", title: "Capture permission granted" };
        return null;
      }
      if (reason === "started") return { ...base, kind: "session", tone: "good", title: "Sharing started", detail: label };
      if (reason === "source") return { ...base, kind: "session", tone: "good", title: `Now sharing ${label}` };
      if (reason === "stopped") return { ...base, kind: "session", tone: "neutral", title: "Sharing stopped" };
      return null;
    }

    // --- deliberately not rendered -----------------------------------------
    // `conversation`, `user_state`, `speech_created`, `worker_registered`,
    // `emotion`, `plugin_registered` and the rest: already on screen elsewhere,
    // or about the process rather than the conversation. See the file header.
    default:
      return null;
  }
}

/**
 * Subscribe to the worker and keep a bounded window of classified entries.
 *
 * ## Entries arrive newest-last, and the array is a ring
 *
 * Prepending is what the UI wants to render (a timeline reads downward from now)
 * but it is O(n) per event, and events arrive in bursts. So the hook appends and
 * reverses for the view instead. Same cost per read, no per-event reallocation.
 *
 * ## It starts from the last session's rows, not from nothing
 *
 * The initial state is read from `localStorage` in the useState *initialiser*,
 * which is what makes restored rows appear in the first paint rather than a
 * render later. This is the difference between a log you can consult about
 * yesterday and one that begins when the window opens.
 *
 * ## The event handler is stable, so this re-renders nothing it does not have to
 *
 * `useAgentRuntime` already keeps its callbacks in refs, so passing an inline
 * `onRuntime` here does not re-subscribe. But it *does* re-render this hook on
 * every parent render, and the setState is what matters: `ingest` is a
 * `useCallback` with no dependencies, so the subscription is mounted once.
 *
 * ## Writing is debounced, because events arrive in bursts
 *
 * A turn emits ten-plus records within a couple of seconds, and
 * `JSON.stringify` + a synchronous write per record is work on the main thread
 * for something the reader will not look at for minutes. So the write is trailing
 * and at most once a second, plus one final write when the window goes away.
 */
export function useActivityFeed(): ActivityFeed {
  const [entries, setEntries] = useState<readonly ActivityEntry[]>(loadPersisted);
  // `number | null` rather than `ReturnType<typeof setTimeout>`: the DOM and
  // Node types disagree about what a timer handle is, and this ref is only ever
  // compared with `null` and handed back to `clearTimeout`.
  const pending = useRef<number | null>(null);

  const schedule = useCallback((current: readonly ActivityEntry[]) => {
    if (pending.current !== null) window.clearTimeout(pending.current);
    pending.current = window.setTimeout(() => {
      pending.current = null;
      persistPersisted(current);
    }, 1000);
  }, []);

  const ingest = useCallback(
    (event: AgentRuntimeEvent) => {
      const entry = classifyActivity(event);
      if (!entry) return;
      setEntries((current) => {
        const next = [...current, entry];
        const bounded = next.length > MAX_ENTRIES ? next.slice(next.length - MAX_ENTRIES) : next;
        schedule(bounded);
        return bounded;
      });
    },
    [schedule],
  );

  // Mounted in `Home.tsx` and not in the Activity page, so navigating to Tools
  // and back does not lose the record of what happened. `useAgentRuntime` holds
  // `onRuntime` in a ref, so a stable `ingest` means the subscription is opened
  // once and torn down with the app rather than with the current route.
  useAgentRuntime({ onRuntime: ingest });

  /**
   * The last write, flushed on the way out.
   *
   * A debounced write that never fires is a log that loses its final minute on
   * every quit — which is exactly the part somebody closed the app to read.
   */
  useEffect(() => {
    const flush = () => {
      if (pending.current === null) return;
      window.clearTimeout(pending.current);
      pending.current = null;
      setEntries((current) => {
        persistPersisted(current);
        return current;
      });
    };
    window.addEventListener("pagehide", flush);
    return () => {
      flush();
      window.removeEventListener("pagehide", flush);
    };
  }, []);

  /** Clears the screen **and** the saved copy. A "Clear" that leaves the rows on
   * disk would bring them straight back on the next launch. */
  const clear = useCallback(() => {
    if (pending.current !== null) {
      window.clearTimeout(pending.current);
      pending.current = null;
    }
    setEntries([]);
    persistPersisted([]);
  }, []);

  return { entries, clear };
}
