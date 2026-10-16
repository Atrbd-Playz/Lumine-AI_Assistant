# 0008 — Idle is not sleeping

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

Two states that a single boolean would merge, and the merge is a product failure in
both directions:

- **`IDLE`** — the turn is over, the conversation is alive. The next sentence needs
  no wake phrase. The agent is connected and paying for it. This is what makes
  "Open Chrome." → "Go to YouTube." → "Search for relaxing music." feel like one
  session instead of three ceremonies.
- **`SLEEPING`** — no conversation. The agent is disconnected or stopped. Only the
  wake listener is live.

If `IDLE` sleeps too eagerly, the user is made to re-wake her for every second
sentence, which is the two-step failure [0007](0007-one-shot-commands.md) exists to
kill. If `SLEEPING` never happens, the product is a permanently open session —
paying for provider time while doing nothing, which is the thing the vision exists
to avoid.

**Nothing in the repository handles this today.** There is no idle timeout, no
inactivity timer and no in-worker shutdown anywhere in Python or Rust. A session
ends only when the last participant disconnects or LiveKit tears the room down, so
a user who walks away from their desk holds a room, an HTTP client, a permission
gate and possibly a browser alive indefinitely.

The naive timer is also wrong in a way that is easy to ship: a fixed timeout that
fires while she is executing a task, waiting on an approval, speaking, or
recovering from an error. That is not an idle timeout; it is a bug that aborts work.

## Decision

**`IDLE` and `SLEEPING` are distinct states with different costs, and the idle timer
is suspended by anything that is still work.**

1. `IDLE → SLEEPING` after a configurable quiet period. Short by default: this is a
   companion, not a conference call.
2. **The timer suspends** while `THINKING`, `EXECUTING`, `SPEAKING`, while an
   approval is pending, while a task is running, and while a failure is being
   recovered. It resumes when the work ends, and the remaining time is measured
   from that point.
3. **"Sleep, Lumine" / "go to sleep" forces `SLEEPING` immediately.** It does not
   close the full UI if the user opened it deliberately, and it never stops the wake
   listener.
4. **Sleep disconnects or stops the agent** and releases what it held.
5. **`SLEEPING` never implies the process is gone.** The runtime and the wake
   listener stay up. See [0012](0012-process-vs-companion-state.md) — this is
   exactly why process state and companion state cannot share an enum.
6. **Every sleep is explained.** The `idle_transition` record carries the cause
   (timeout, spoken command, user action, room teardown), so "why did she stop
   listening?" is answerable from the log rather than guessed at.

## Why not the alternative

Rejected: one timeout, unconditional. It is four lines of code and it aborts work.

Rejected: never sleeping, and relying on the user to end the session. That is the
"permanently open application" the vision refuses, with a bill attached.

Rejected: making the *worker* own the timeout. It cannot: the worker only exists
during a conversation, so a timer inside it is a timer that stops at exactly the
wrong moment. The timeout belongs to the runtime, above the worker.

## Consequences

- The lifecycle machine needs a suspend/resume concept on the idle timer, and that
  has to be tested against the specific dangerous combinations: timeout during a
  tool, during a pending approval, during a multi-step task, during recovery.
- Sleep must be reachable from three places — the spoken command, the island, and
  the full UI — and all three go through the same intent, so the state machine stays
  the only writer.
- Rust's current `child.kill()` is ungraceful and never runs the worker's
  `close_session`, so `session_ended` does not appear on a Tauri-initiated stop.
  Until Phase 3, "stopped" and "ended cleanly" must not be conflated in the UI or
  the logs.
- The idle period is a setting, because the right value depends on the user, and
  Phase 1 must pick a default that a first-time user experiences as responsive.

## Revisit when

Proactive behaviour (Phase 9) makes "she is awake but not talking to me" a state
worth having — which means `SLEEPING` may need a companion concept such as
*available but not addressed*. Add it then; do not pre-emptively weaken the
distinction now.