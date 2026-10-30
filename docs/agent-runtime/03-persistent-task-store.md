# Phase 3 — Persistent task store

- **Status:** specified, **not approved.** Blocked on Phase 2 — the store is
  written *by* the runtime, not beside it.
- Owns, later: the **central retention service** other phases register
  descriptors with ([§5 of the consistency
  audit](10-consistency-audit.md#5-persistent-state--restart-crash-and-retention)).

> **The highest-value item in this track.** Everything else makes the runtime
> better. This one makes it *trustworthy across a restart*.

## 1. The problem

`Task` is an in-memory dataclass held in `task_runner._TASKS`, keyed by
`id(session)`.

So a worker restart loses an in-flight task, and the user is told **nothing** —
which is the most expensive failure mode in the product, because it looks
exactly like Lumine ignored the request.

- The 90-second `TASK_BUDGET_SECONDS` makes long tasks *nearly* safe, but a crash
  at second 40 is a total loss.
- A task interrupted mid-send is indistinguishable from one never started.

## 2. The slice

```text
.lumine/
  tasks/
    active/
      task_<id>.json
    completed/
    failed/
```

An in-flight task survives a worker restart, is reported to the user, and can be
cancelled — or resumed deliberately, never silently.

## 3. Do

- Add `Task.from_dict()` beside the existing `to_dict()`.
- **Resolve the one real design problem:** `Task.complete_when` is a
  `Callable[[Task], bool]`, which cannot be serialised. Store the criterion's
  **name** and resolve names through a registry.
  **Do not drop the criterion** — a task that ran every step and did not satisfy
  it must still settle as `partial`, because that is the whole of
  `task_model.finish()`.
- Write after every step transition, **atomically** (temp file + rename).
- Read `active/` on worker start.
- `cancel_task` works against a loaded task, not only a live one.

## 4. `interrupted` is not `failed`

A task killed by a restart is **`interrupted`**, not `failed`. A crash is not a
failure of the task's goal, and reporting it as one teaches users that Lumine's
reports are unreliable.

`runtime/verification.py`'s `timed_out` is the same instinct applied to one step:
*"We stopped waiting. The outcome may still be unknown."* The task lifecycle
needs the same third answer.

This adds a **task status** beyond today's `pending` / `running` / `completed` /
`partial` / `failed` / `cancelled` / `awaiting_approval`. It is terminal, and it
is not `failed`.

## 5. The effect ledger is **embedded in the task record**

The codebase already proves the idempotency pattern three times: the WhatsApp
send ledger (confirmed 10 s, uncertain 5 min), the `open_app` launch-dedup
window, and the gate collapsing identical pending cards. Phase 3 makes the
**task-scoped** form of it a property of the task file itself — one file, one
atomic write per transition, no torn pair of a "task" record and a separate
"effects" store:

```json
{
  "task_id": "…",
  "effects": [{
    "effect_id": "…",
    "action_id": "…",
    "capability_id": "communication.send_message",
    "state": "prepared | dispatched | confirmed | uncertain | failed",
    "prepared_at": "…",
    "observed_at": "…",
    "evidence": "…",
    "hold_until": "…",
    "reconciliation": "none | observed | user_denied | abandoned"
  }]
}
```

Rules that carry the existing laws forward, now across a restart:

- **A potentially-executed effect is persisted as `prepared` *before* the
  external invocation.** Not after, not cherry-picked: the write ordering is
  the guarantee. If the process dies after the write and before the call, the
  effect lands on reload as `uncertain` — conservative, because blocking a
  retry that might have been harmless always loses to sending twice.
- **`may_have_happened()` now survives crashes**: it is derivable from the
  persisted `prepared` / `dispatched` / `uncertain` states, not from
  in-process memory.
- A **confirmed** effect blocks a duplicate for its hold window, as today.
- An **uncertain** effect holds longer and is reconciled against a fresh
  observation before any retry — the law, with persisted state.
- **Domain ledgers stay authoritative for their protocol** (the WhatsApp send
  ledger owns its delivery semantics; the launch dedup owns its window). The
  task record links their outcomes; it does not replace them. No effect is
  owned by two stores — the task record owns *task-level* reconciliation, the
  domain ledger owns its protocol. Each existing ledger becomes a consumer as
  Phase 3 proves itself, and is never deleted.

Precedent writes (`apps.py`, `whatsapp_ops`) keep their shapes; this phase adds
the task-level join, not a second authority.

---

## 6. What may be written

| Allowed | Forbidden |
|---|---|
| goal text | tool result payloads |
| step descriptions | message bodies |
| tool names | contact names |
| statuses and verification outcomes | any credential |
| timestamps | anything from an injected prompt |
| effect ids, states, hold windows | the text the user asked to send |

## 7. Retention is centralised here

**Decision (2026-10-30): retention is centralised, configurable, and
bounded.** Phase 3 owns the one sweep — `runtime/retention.py`. Later phases
(event recordings, artefacts, worktrees) **register store descriptors** with
it; they do not invent policies, and there is no second sweeper anywhere in
the runtime.

Rules with no exceptions:

- **Never auto-delete** an `uncertain` task, a task holding an unresolved
  effect, or a task under active review (`reviewed_at` inside the hold
  window). Uncertain evidence is the *only* proof a "did that go out?"
  question can be answered honestly later.
- The sweep is bounded and configurable from the saved document. Defaults are
  proposed in [§5 of the consistency
  audit](10-consistency-audit.md#5-persistent-state--restart-crash-and-retention)
  and stay **provisional** until approved — they are policy numbers, not
  engineering defaults, and they change nothing about what is safe to lose.
- Deletion is a user act or the bounded sweep — never a voice-path timer, and
  a delete is audited like any other effect.
- The sweep never runs on the voice turn path, for the same reason the write
  rule exists.

## 8. Do not

- Write anything on the voice path that could delay a turn. A write per step is
  acceptable; a synchronous fsync per step is not.
- Store a `PermissionRequest` id. Approvals are session-bound by design
  ([`permission_gate`](../../agent/runtime/permission_gate.py): *"Nothing is
  persisted: a restarted worker starts empty"*) and that must stay true.
- Resume a task automatically across a restart. Loading it so it can be
  **inspected, reported and cancelled** is the slice; auto-resume is not.
- Store effect state anywhere but inside the task record. A second
  "effects.json" beside a `task_<id>.json` is a torn write waiting for a crash
  — the whole point of embedding the ledger is that the pair cannot be torn in
  the first place.
- Let retention delete what recovery needs. When they disagree, **recovery
  wins**, and the retention conflict is reported, not resolved silently.

## 9. Tests

- round-trip fidelity for every status
- `interrupted`, not `failed`, on restart
- a corrupt file is **reported, never silently defaulted** — `focus_store.py`
  (removed along with the Focus workspace) set this precedent, and overwriting a
  document with defaults is how a person
  loses a term of history and is never told
- the atomic write leaves no partial file
- a loaded task can be cancelled
- the permission gate is still empty after a restart
- **effect ordering:** fault injection between the `prepared` write and the
  invocation yields `uncertain` on reload, and `may_have_happened()` is true
  from persisted state alone
- 20 forced-kill trials at random step boundaries: zero torn files, zero
  losses the record does not describe
- a `prepared` effect written before nothing but a kill is never `confirmed`
- retention: an `uncertain` task and a task under the review-hold window
  survive a sweep that removes an old `completed` one; the swept deletes are
  audited

## 10. Exit criteria

- kill the worker mid-task, restart, and the task is listed, described, and
  cancellable
- every externally-visible effect a task *may* have executed is described in
  its record, with its state, whether the worker survived it or not
- no secret or message body exists anywhere under `.lumine/`
- step-write p95 ≤ 5 ms *— provisional:* measured over 100 atomic
  temp+rename writes on this machine's normal storage; a teaser number until
  measured, not a gate
- `2232+ tests pass`

## 11. Negative scope — must not implement

- No auto-resume and no "resume prompt" persisted state — inspect, report,
  cancel; `resume deliberately` is a user act, and the deliberate/resume path
  is Phase 4's owner, not the store's.
- No persisted approvals, credentials, or permission requests — restarts stay
  empty and that is load-bearing.
- No recovery ladder, retry decisions, or stuck-loop detector (Phase 4); the
  store answers statuses, never recovery.
- No `filesystem.write` — the store's filesystem is its own, bounded, and not
  a user capability ([ADR-0016](../decisions/0016-filesystem-write-scoped-worktrees.md)).
- No `interrupted` → `failed` conflation and no `interrupted` → `SLEEPING`
  ([ADR-0012](../decisions/0012-process-vs-companion-state.md)).
- No database, no server, no daemon — file rows are enough until a phase that
  needs more is approved.
- Retention that deletes evidence on a timer without a user or a bounded,
  configured sweep.
