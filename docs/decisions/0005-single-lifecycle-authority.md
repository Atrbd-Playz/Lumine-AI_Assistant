# 0005 — One lifecycle state machine is the only authority

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

The vision requires an explicit lifecycle — `SLEEPING`, `WAKING`, `LISTENING`,
`THINKING`, `EXECUTING`, `SPEAKING`, `IDLE`, `ERROR` — and explicitly forbids
scattered booleans like `isListening` / `isSpeaking` / `isAwake` / `isProcessing`.

The reason is not tidiness. With independent booleans there is no answer to "what
is Lumine doing right now", only an answer to a list of related questions whose
combinations include states nobody intended — `listening && speaking &&
processing` is reachable, and each surface will pick a different one of the six
flags to render. Three surfaces plus two state vocabularies is how a companion ends
up showing a thoughtful face while her audio is blocked.

**What exists today** is three vocabularies and no machine:

| Where | Type | Notes |
| --- | --- | --- |
| `agent_manager.rs` | `AgentState` (9 variants) | Describes the **process**. `Listening`, `Thinking`, `Speaking`, `Stopped` are never constructed. `Sleeping` means "no worker". |
| `agent.py` | LiveKit's `AgentState` | Subscribed, recorded as a latency mark, **never published**. Only `user_state` is published. |
| `types.ts` | `LumineState` (7 values) | Set **by the webview** from room events. `"thinking"` is declared and never assigned. |

## Decision

**There is exactly one lifecycle state machine. It lives in the runtime, it is the
only thing that may set the companion state, and it publishes every transition as
one typed event that every surface renders.**

1. The runtime owns the machine. The webview never infers a state from room
   events — it renders what it is told.
2. Every transition is published once, in one shape, carrying `from`, `to`, and
   `cause`.
3. The transition table is **data**, and it is tested as a table: an illegal
   transition fails a unit test rather than showing up in a demo.
4. No boolean combination is ever published as truth. If a surface needs "am I
   busy?", it asks the machine.
5. Surfaces do not command states directly. They raise intents (`request_sleep`,
   `request_wake`, `request_end`), and the machine decides whether the transition is
   legal — which is what stops a wake word from abandoning a task mid-flight.

## Why not the alternative

Rejected: keep three vocabularies and translate between them at the surfaces. It
is the least code, and it is exactly how the current state was reached — three
partial translations and no authority. Translating between vocabularies also means
deciding, per surface, what an untranslatable state means, and those decisions
diverge silently.

Rejected: let LiveKit's `agent_state_changed` be the lifecycle. It has four states,
knows nothing about `SLEEPING` or `EXECUTING`, and is scoped to a room that does
not exist while she sleeps — so it cannot describe the states that matter most.

## Consequences

- `LumineState` and the published `agent_state_changed` payload become derived
  views, not sources. The existing `Record<LumineState, …>` tables in
  `Presence.tsx` must cover every state the machine can publish, and adding a state
  then fails the build — which is the property to preserve.
- The webview's room-setup `setState("connecting")` calls have nowhere to go; their
  meaning becomes `WAKING`.
- The machine is pure enough to test exhaustively, which is the point: the states
  that are expensive to observe live (a wake during an approval, a timeout during a
  task) are cheap to test here.

## Revisit when

Never, in the sense that any change should be a superset of states, never a
replacement of the authority. See [0012](0012-process-vs-companion-state.md) for
the one legitimate split.