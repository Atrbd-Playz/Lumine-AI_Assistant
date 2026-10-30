# Phase 2 — `AgentRuntime` façade

- **Status:** specified, **not approved.** Blocked on Phase 1 **and Phase 1.5**
  — the runtime receives the session-owned
  [capability loader](07-capability-discovery.md#5-the-loader--owns-schema-state-never-lifecycle)
  injected, and cannot be specified without it.

## 1. The problem

The agent lifecycle has **no single owner**. It is assembled across three layers:

| Layer | Owns today |
|---|---|
| the model | decides *what* the user wants, and when a task is needed |
| `tools/task_runner.py` | builds the plan, owns the run, holds the session registry, defines the step implementations |
| `runtime/task_engine.py` | advances the task: dependencies, retries, cancellation |
| `agent.py` | wires it to LiveKit, the room, the publisher, the gate |

Each layer holds state the others cannot see. `run_task` is simultaneously a tool
call, a planner, an executor and a session-scoped registry. That is why "the
lifecycle" has no testable definition, and why the request that opened this track
asked for an `AgentRuntime`.

## 2. The slice

One owner for the lifecycle, and nothing else.

```python
class AgentRuntime:
    def __init__(self, *, registry, planner, loader, gate=None, events=None): ...

    async def handle(self, request: str) -> Outcome: ...
    async def run(self, task: Task) -> Task: ...
    async def cancel(self, task_id: str) -> CancelAck: ...
```

The `loader` is the **session-owned** capability loader from
[Phase 1.5](07-capability-discovery.md). The runtime **injects and calls** it;
it never constructs, owns, or restarts it. The loader owns *schema state*; the
runtime owns *lifecycle*. Nothing else has both halves.

## 3. Do

- New `runtime/agent_runtime.py`.
- `tools/task_runner.py` becomes a **caller**, not the owner. `execute()`'s
  signature and behaviour are preserved so existing callers are untouched.
- Gate, planner, executor, capability registry **and the loader** are imported
  one-way or injected. The runtime knows nothing about Playwright, UI
  Automation, or `subprocess`.
- Promote the manual smoke scenarios to automated tests.

## 4. Do not

- Add a capability, a tool, a permission, or a state file.
- Import LiveKit. It must run with `session=None`.
- Move persona or personality code. **Expression is a separate layer** — the
  runtime controls execution, the personality layer controls expression.
- Move the step implementations out of `task_runner.py` in this phase. They are
  ~600 lines of accumulated measured behaviour; moving them changes the failure
  surface for no gain.
- Own or construct the capability loader. It arrives injected, session-owned;
  the runtime's relationship to it is an injection point and a call, nothing
  more ([ADR-0010](../decisions/0010-layer-ownership.md) — one owner per
  responsibility).
- Treat LiveKit's turn interruption as a work cancellation. They are separate
  scopes (§6) and conflating them is how a barge-in silently deletes a task.

## 5. Complexity routing

What the user speaks determines whether a task exists at all. This must stay
**cheap and deterministic** — a classifier that costs a model call to answer
"is this simple?" is worse than the ceremony it avoids.

| Shape | Example | Behaviour |
|---|---|---|
| **simple** | "What's the weather?" | one tool call, no task |
| **medium** | "Open Notepad." | small plan, execute, verify |
| **complex** | "Open Chrome, search YouTube for X, play it, start a focus timer." | persistent task with steps |

The rule of thumb is the one `tools_policy.py` already teaches: **one job is one
tool.** Two or more *dependent* steps is a task. `task_runner.MAX_STEPS = 8`
already encodes the upper bound, and its comment is the reason:

> *"A voice request that needs more is a request whose intent was not understood,
> and continuing would spend the user's time discovering that."*

## 6. Cancellation — two scopes, and acknowledgement is not termination

The façade is the **single work-cancellation owner**. It shares a vocabulary
with the voice turn, but the two scopes are separate types:

| Scope | What it stops | Owner | Example |
|---|---|---|---|
| **turn** | the generation, the TTS, the barge-in | the LiveKit session | the user starts speaking over her answer |
| **work** | the task, its steps, its tools, the delegate | `AgentRuntime` | the user says *"stop, forget the browser"* |

Rules that are structural, not prompt instructions:

1. **Barge-in never implicitly cancels a running task.** An interruption during
   a tool run is handled deliberately (companion Phase 4 exit 3); the runtime
   treats it as a turn event, not a `cancel()`. A task that survives a barge-in
   is the test.
2. **`cancel()` acknowledges; it does not claim.** It returns a `CancelAck`:
   accepted, scope, target, `termination: pending`. Termination is confirmed
   separately, by evidence — the engine's terminal state, a process exit, the
   delegate's lease going dead. Nothing speaks *"stopped"* on an ack alone,
   and the audit records both the ack and the confirmation.
3. **A cancel during a pending approval resolves the approval as a denial.**
   Not a race; not a grant after death. The gate is told, the card closes, the
   refusal is audited.
4. **Schema changes happen only at a turn boundary.** The loader may apply its
   changes between turns — never mid-generation, never mid-tool, never while
   an approval is pending. On a realtime profile the reconnect cost of
   `update_tools` makes this a correctness rule as much as a UX one.

The operation table covering every async surface is
[§3 of the consistency audit](10-consistency-audit.md#3-cancellation--one-owner-per-scope).

## 7. Tests

`agent/tests/test_agent_runtime.py`:

- complexity routing: simple / medium / complex
- a single-step request creates no `Task`
- the runtime runs with `session=None`
- a cancelled task cannot be resumed by a later call
- **barge-in produces no `task.cancelled`** — 20 interruption fixtures, zero
  implicit cancels
- `cancel()` acknowledges with `termination: pending`; confirmation arrives
  later, on engine evidence
- a cancel while an approval is pending resolves the approval as a denial,
  never a grant, 20/20
- a task with no runnable steps reports a **deadlock**, not a loop
- `handle()` returns an `Outcome` that names what was *uncertain*

## 8. Exit criteria

- `2232+ tests pass`
- no observable behaviour change
- `agent.py` loses orchestration responsibility and gains none
- a reader can find the whole lifecycle in one file
- cancel **ack** p95 ≤ 250 ms *— provisional:* measured from `cancel()` call to
  the acknowledgement event, 50 trials, excludes process termination. The
  number is a target until [§2 of the consistency
  audit](10-consistency-audit.md#2-phase-acceptance-matrix) records feasibility
  on this machine; it is not yet a gate.

## 9. Extensions that land here

From the [expansion reconciliation](09-expansion-reconciliation.md), on the
evidence that cancellation is a property of ownership:

- **Unified cancellation.** §6 above is that property made concrete: one work
  intent, one owner, one ack — with turn interruption fully separate, and
  companion Phase 4's barge-in riding the turn scope without ever becoming a
  cancel.
- **Deterministic routing first.** `router.py` + complexity routing above stay
  cheap and rule-ordered. Model escalation on low confidence is a *next-session*
  decision (handoff) or the existing `realtime_fallback_adapter`, never a
  mid-session silent upgrade on a realtime model.

## 10. Negative scope — must not implement

Adjacent-phase features named so nobody grows into them:

- No new capability, tool, permission switch, or state file (Phases 1 and 3).
- No capability loading or eviction *policy* — the loader is Phase 1.5's, and
  arrives injected; the runtime may call it, never own it.
- No LiveKit import, no new voice path, no `session.say`.
- No EventBus, no trace ids, no dashboard (Phase 5).
- No persistence, no restart semantics, no effect ledger (Phase 3).
- No recovery ladder, no stuck-loop detector (Phase 4).
- Any memory or context layer (Phase 7).
- Mid-session model swap, silent escalation, a second planner.
