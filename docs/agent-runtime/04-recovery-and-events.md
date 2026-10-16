# Phases 4 and 5 — Recovery and Events

Both are **consolidations**. Each takes behaviour that already exists implicitly
and makes it explicit and testable, without changing what it does.

---

# Phase 4 — `RecoveryEngine`

- **Status:** specified, **not approved.**

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

## 6. Tests

- each rung in isolation
- a `may_have_happened` outcome reaches `ask` or `abort`, never `retry`
- the ladder terminates on every input
- a fallback tool is genuinely steadier (the assertion that matters: `browse`
  before `desktop_control` on a page, per `ops.INTEGRATED_WINDOWS`)
- every rung taken is reported

---

# Phase 5 — `EventBus`

- **Status:** specified, **not approved.**

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

## 7. Exit criteria

- `permission_gate`, `progress.py` and the runtime all publish through one path
- `2232+ tests pass`
- no UI change
