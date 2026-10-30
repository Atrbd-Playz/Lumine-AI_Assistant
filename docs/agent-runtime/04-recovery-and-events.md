# Phases 4 and 5 — Recovery and Events

Both are **consolidations**. Each takes behaviour that already exists implicitly
and makes it explicit and testable, without changing what it does.

---

# Phase 4 — `RecoveryEngine`

- **Status:** specified, **not approved.**
- **Depends on:** Phase 3 — the checkpoint-resume rung reads the persistent
  record; no checkpoint store, no resume rung. Phase 4 and Phase 5 are
  **independent consumers of Phase 3**, and neither depends on the other:
  Phase 4 publishes through the existing publisher (`runtime_events`) until
  the bus exists, and this document's Phase 5 is invisible to the ladder.

## 1. The problem

Recovery is scattered across four places:

| Where | What it holds |
|---|---|
| `task_model.RetryPolicy` | `max_attempts`, `retry_safe` |
| `desktop/ops.py::hold_seconds` | how long an unconfirmed click holds a retry |
| `desktop/capture.confirm_target` | freshness re-read before an irreversible act |
| `messaging/whatsapp_ops.py` | the send ledger |

The ladder exists implicitly. Nowhere is it a statement, so nothing can reason
about it, test it end to end, or explain to a user what will happen next.

## 2. The ladder

```text
retry        the failure was transient and nothing left this process
refresh      cached state may be stale; re-read before deciding
re-observe   the world moved; look again before acting on a stale premise
re-plan      the plan's premise no longer holds
fallback     a steadier tool exists for this action
ask user     only a person can resolve this
abort safely stop, leave nothing half-done, and say what is true
```

## 3. Do

- Express the ladder as a module the executor consults.
- `RetryPolicy` and `hold_seconds` stay **exactly** as they are. The engine reads
  them; it does not reimplement them.
- The engine chooses a rung and reports **which** one it chose. That is what makes
  "she retried it" a fact rather than a guess.

## 4. Do not

- **Add a retry loop.** `failure_gate.py` and `llm_errors.py` deliberately have
  none: *"a session that has hit a rate limit and keeps trying spends the quota it
  has left and recovers more slowly."*
- Retry anything where `verification.may_have_happened()` is true. That is how a
  person gets two messages, and it is the single most expensive mistake available
  in this codebase.
- Exceed `RetryPolicy.attempts_allowed ≤ 5`.
- Recover silently. Every rung that runs instead of failing is something the user
  should be able to see, because it costs time.

## 5. The rule that keeps it honest

`verification.may_have_happened()` is checked **before** the ladder is consulted,
not inside it. If an outcome is `uncertain` or `timed_out`, something may exist
outside this process, and the only available rungs are `ask user` and
`abort safely`.

This is the same reason `ops.Operation.irreversible` is a *property* of
`hold_seconds` rather than a separate judgement: one source, no disagreement.

## 6. The recovery responsibility matrix

**Decision (2026-10-30): one owner per failure class, recorded here and
unchanged by anything else in this track.** The matrix is the answer to "who
owns *this* failure," asked once.

| Failure class | Owner | Semantics |
|---|---|---|
| LLM provider error per call | `runtime/llm_errors.py` + `runtime/failure_gate.py` | classify, open the circuit after three failures, one short apology; **no retry loop, deliberately** |
| Action / step failure | Phase 4 (the ladder + hold rules) | rung chosen on evidence, reported |
| Worker restart, task orphaned | **Phase 3** | `interrupted`, listed, cancellable — the store owns the record; recovery never rewrites it |
| Deliberate resume | Phase 4 **reading Phase 3** | only at a verified checkpoint, only at the user's request; auto-resume stays illegal |
| Worker process crash / restart | companion Phase 3 + `agent_manager.rs` | bounded restart policy; the runtime does not respawn itself |
| Delegate orphan process | **Phase 11** (reaping at worker start) | identity + task ownership verified ([ADR-0015](../decisions/0015-coding-delegate.md)); an unverified process is left alive and the task marked `uncertain` |
| Event subscriber failure | Phase 5 | logged and dropped, never propagated |
| Stuck loop | Phase 4 (§8) | evidence inputs, then `re-plan` / `ask user` |
| Permission timeout / denial | `permission_gate` | a decision, not a recovery input; recovery respects it rather than re-asking |

Two clarifications the matrix forces:

- **Memory of a crash is never state-changing.** Phase 3 marks `interrupted`;
  recovery *reads* the record. Recovery never writes a status that belongs to
  the store's vocabulary (`interrupted` ≠ `failed`, and
  [ADR-0012](../decisions/0012-process-vs-companion-state.md) keeps process
  and companion state separate).
- **A cancel is not a recovery failure.** Cancellation has its own owner
  ([Phase 2 §6](02-agent-runtime.md#6-cancellation--two-scopes-and-acknowledgement-is-not-termination))
  and its own ack/termination vocabulary; the ladder is never consulted on a
  deliberate stop.

## 7. Tests

- each rung in isolation
- a `may_have_happened` outcome reaches `ask` or `abort`, never `retry`
- the ladder terminates on every input
- a fallback tool is genuinely steadier (the assertion that matters: `browse`
  before `desktop_control` on a page, per `ops.INTEGRATED_WINDOWS`)
- every rung taken is reported

## 8. The stuck-loop detector — an extension that lands here

From the [expansion reconciliation](09-expansion-reconciliation.md): the ladder
answers "retry only when safe"; what it lacks is **recognising being stuck**
before an ambiguous rung is chosen. The engine gains evidence inputs, not a new
policy:

- **repeated identical failure** — the same action, same args, same class of
  failure N times (N=3: the same bound `failure_gate.py` chose for LLM
  failures) escalates to `re-plan` or `ask user`;
- **unchanged page state** — the browser's snapshot generation and the desktop's
  capture generation can both *measure* that nothing moved, so "unchanged" is a
  fact and not a feeling;
- **stale observation** — acting on a capture older than the action it decided
  is `refresh`/`re-observe`, already a rung; the detector is what notices the
  staleness without being told.

Every detection is published on the event stream — a loop the user must watch
detach again is a failure of the same kind the Phase 5 docstring's dead-pipe
story teaches: invisible making things worse. The existing "no retry may run
while `may_have_happened()` is true" rule binds the detector exactly as it
binds the ladder.

The threshold is **N = 3** — provisional until measured: it is the
bound `failure_gate.py` chose for LLM failures, and it is the cheapest evidence
that a loop is not a coincidence, but the number is confirmed on this
machine's real corpora before it is trusted.

### 8.1 Detector exit criteria

- fires on the third identical failure — not the second, not the fourth
- never fires when page/generation state measurably moved between attempts
- every firing publishes an event **and is visible to the user** before the
  recovery rung is chosen
- never suppresses a rung the ladder would have chosen on evidence alone

## 9. Negative scope — must not implement

- **No retry loop.** `failure_gate.py` and `llm_errors.py` deliberately have
  none; the ladder does not become one. A `may_have_happened` outcome reaches
  `ask user` or `abort safely`, never `retry`.
- No knowledge of persistence beyond reading it — the store owns
  `interrupted`/record semantics; recovery writes nothing into them
  (`may_have_happened` evidence flows the other way: Phase 3 → recovery).
- No EventBus and no trace-minting — the ladder publishes through the existing
  publisher; Phase 5's bus consumes it later, unchanged.
- No authority-class floor changes, no new tools, no capability edits (Phase
  1); the ladder chooses a rung, never a looser class.
- No autonomous second-plan approval — `re-plan` re-runs validation; it never
  grants itself new authority.

---

# Phase 5 — `EventBus`

- **Status:** specified, **not approved.**
- **Owns:** **trace identity** for the whole runtime — `trace_id`,
  `parent_trace_id`, `event_id`. No other module mints these
  ([§6 of the consistency audit](10-consistency-audit.md#6-trace-identity)).
- **Depends on:** Phase 3 only — the bus records task events; Phase 4 and
  Phase 5 are independent consumers of Phase 3 and neither relies on the
  other. Phase 4's publications ride the existing publisher until the bus
  exists; when the bus lands it consumes them unchanged.

## 1. The problem

`permission_gate`, `progress.py` and `runtime_events` each publish directly. Every
new consumer — the island, a log, a voice line — means another wiring site, and
each one is a place where a telemetry failure can become a session failure.

The precedent is already expensive: `write_event_line` catches
`OSError`/`ValueError` because an unguarded `print` to a dead pipe **killed every
session on its first line**.

## 2. The slice

Subscribers over the existing event stream.

## 3. The vocabulary

Already partly in use, and consistent with what the UI consumes today:

```text
task.created              task.started            task.finished
task.step.started         task.step.completed     task.step.failed
task.step.retried         task.step.skipped       task.step.cancelled
permission.requested      permission.granted      permission.denied
tool.started              tool.completed          tool.failed
observation.updated       verification.failed
task.completed            task.failed
```

## 4. Do

- **Additive.** Existing direct publishing stays until the bus proves itself.
- One failure domain: a subscriber that raises is logged and dropped, never
  propagated.
- Reuse `emit_and_publish`, so the stdout record and the LiveKit topic cannot
  disagree.

## 5. Do not

- **Rename existing event types.** `agent_started`, `agent_state_changed`,
  `agent_stopped`, `agent_error`, `agent_runtime` are consumed by
  [`lumine-ui/src/lib/agentRuntime.ts`](../../lumine-ui/src/lib/agentRuntime.ts).
- Rename `LUMINE_EVENT <json>` on stdout. `agent_manager.rs` re-emits it and
  `broken_stdout_check.py` reproduces the failure it prevents.
- Let a subscriber failure propagate, for the reason above.
- Redesign the UI. **This phase opens the contract; the UI consumes it when it is
  ready.**

## 6. Tests

- a subscriber that raises does not stop the publisher or the task
- `emit_and_publish` writes both surfaces with the same payload
- the event names above are the ones emitted, asserted as a closed set
- `broken_stdout_check.py` still reproduces

## 7. Trace identity — Phase 5 owns it, nobody else mints it

The vocabulary, bound once:

| Id | Lifetime | Minted by | Riding |
|---|---|---|---|
| `task_id` | stable for the whole task | Phase 2/3, at task creation | every record about the task |
| `trace_id` | unique per **execution turn** | Phase 5, at the turn boundary | router → planner → tools → permission → result |
| `parent_trace_id` | optional, when work is spawned by another trace (research worker, delegate) | Phase 5 | spawned traces |
| `event_id` | unique per event; the dedupe key for recording and replay | Phase 5 | every event |

Rules:

- **One owner.** A tool, a router, a loader or a gate that mints a
  `trace_id` is a second lifecycle of the same failure as a second authority.
  A test enumerates the closed event set and refuses any record minted
  elsewhere.
- **Turn-scoped, not task-scoped.** One turn may execute many steps of one
  task; one trace covers the whole execution of that turn. A task checkpointed
  and resumed in a later turn carries the *task's* `task_id` and gets a fresh
  `trace_id`.
- **Idempotency of the record:** `event_id` dedupes; an event re-published
  under the same `event_id` is the same event, not a second one. Replaying a
  recorded stream never duplicates.
- **Phases 1.5 and 4 do not mint.** Before Phase 5 exists they publish as they
  do today; when the bus lands, its ids attach at the boundary and those
  records gain them without code changes.

## 8. Recording, replay, and the allowlist

**Decision (2026-10-30): replay reads an explicit field allowlist, redacted to
the audit log's standard.** Recording is append-only JSONL in the retention
service's care:

| Kept in the record | Never in the record |
|---|---|
| `event_id`, `trace_id`, `parent_trace_id`, `task_id` | message bodies, contact names, search queries |
| event type, timestamp, duration | tool result payloads (hash + size only) |
| capability id, action class, verification state | credentials, tokens, any `.env` value |
| error class (never the raw error text) | file paths outside the worktree/allowlisted roots |
| actor (gate, engine, loader, delegate run) | the prompt text the user spoke (hash only) |

- The allowlist is a **field list**, not a filter heuristic: a payload field
  not on the list is dropped, not "sanitised as best we can."
- Replaying a recording needs no LiveKit, no providers, no browser — the same
  discipline the suite's fake trees and `broken_stdout_check.py` already use.
- Retention: the recording registers with the central retention service
  ([Phase 3 §7](03-persistent-task-store.md#7-retention-is-centralised-here));
  the stream never grows unbounded, and nothing here is ever voiced.

## 9. Exit criteria

- `permission_gate`, `progress.py` and the runtime all publish through one path
- 100% of the closed event set carries `trace_id` + `event_id` — asserted by
  enumeration, not by sampling
- a subscriber that raises is dropped, and the task that raised it survives
- a secret fixture never appears in the recording — allowlist holds
- `2232+ tests pass`; no UI change

## 10. Negative scope — must not implement

- No renaming of existing event types (`agent_started`, `agent_state_changed`,
  `agent_stopped`, `agent_error`, `agent_runtime`) and no changes to the
  stdout `LUMINE_EVENT <json>` contract.
- No trace minting outside Phase 5. No trace ids on records that predate it —
  they arrive without them, and the bus is not rewritten to backfill.
- No UI, no dashboard, no replay *tool* — the contract opens here; consuming
  it is later work, on another surface.
- No second telemetry system; a "simpler" parallel publisher is the failure
  this phase exists to prevent.
- No secrets, no message bodies, no third-party text in the recording or in
  any replay artefact.
