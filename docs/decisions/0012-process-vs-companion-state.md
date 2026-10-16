# 0012 — Process state and companion state are different types with different names

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

`agent_manager.rs` declares a nine-variant enum whose name is the source of the
problem:

```rust
pub enum AgentState {
    Booting, Ready, Sleeping, Listening, Thinking, Speaking, Error, ShuttingDown, Stopped,
}
```

It describes **the worker process**. Only five of the nine variants are ever
constructed — `Listening`, `Thinking`, `Speaking` and `Stopped` are declarations
that nothing reaches — and the reachable set is
`Sleeping → Booting → Ready → (ShuttingDown → Sleeping)`, plus `Error`.

Meanwhile the vision's companion lifecycle is
`SLEEPING, WAKING, LISTENING, THINKING, EXECUTING, SPEAKING, IDLE, ERROR`.

The two overlap on six names and mean different things by one of them:

> **`Sleeping` today means "there is no worker process."**
> **In the companion lifecycle, `SLEEPING` means "the user has not asked for
> anything, and the wake listener is all that is running."**

Those are opposite conditions. In the target, `SLEEPING` is the *healthy,
running, normal* state — it is what a companion is for most of the day. A boolean
field, a log line, or a UI string reading "sleeping" when the worker is dead will
be read as "she is resting" by a human debugging at 2am, and will be read as
"something crashed" by the person who meant the opposite.

There is a third vocabulary too: `LumineState` in `types.ts`, seven values, set by
the webview from room events, with `"thinking"` declared and never assigned.
[0005](0005-single-lifecycle-authority.md) covers why the authority must be single;
this record is about the naming.

## Decision

**Process lifecycle and companion lifecycle are separate types, in separate layers,
with names that cannot be confused. `Sleeping` is a companion state only.**

1. **`ProcessState`** — owned by `agent_manager.rs`. Describes the child: `Stopped`,
  `Starting`, `Registered`, `Stopping`, `Failed`. It may keep `Stopped` and must not
   keep `Sleeping`.
2. **`CompanionState`** — owned by the runtime's lifecycle machine. The eight states
   from the vision.
3. **The two never share a name.** A test asserts the sets are disjoint; adding a
   variant to one that collides with the other fails the build.
4. **Every payload carries which it is.** `agent_state_changed` becomes
   `process_state_changed`, and the new `lifecycle_state` record is unambiguously the
   companion. Renaming the Tauri event is a breaking change, so it is additive: the
   old event keeps its meaning and its name changes are listed as migration work in
   the Phase 1 plan.
5. **The webview's `LumineState` becomes a derived view** of `CompanionState`,
   keeping only the values it actually renders.

## Why not the alternative

Rejected: reuse `AgentState` and add the missing states to it. It is the smallest
diff and it is how the two meanings became entangled in the first place — the enum
is named after the agent and describes the process.

Rejected: keep `Sleeping` in the process enum and rely on documentation. The word is
used in log lines, event names and UI strings; a docstring does not survive being
copied into a string literal.

## Consequences

- Touches the Tauri event surface, which consumers subscribe to by name. It is
  additive by construction: new events, old events preserved, with a deprecation
  window rather than a rename-and-fix-everything.
- Four dead variants either get their meaning restored (`Stopped` becomes real once
  stop is distinguishable from sleep) or are deleted. Leaving them is what made the
  enum misleading in the first place.
- Any diagnostic that prints a state must print which state it is. `state=Sleeping`
  must never appear without its kind.

## Revisit when

Never, unless the product stops running a separate worker process — which
[0001](0001-companion-not-application.md) rules out anyway.