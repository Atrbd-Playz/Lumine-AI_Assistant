# Phase 3 — Persistent task store

- **Status:** specified, **not approved.**

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

## 5. What may be written

| Allowed | Forbidden |
|---|---|
| goal text | tool result payloads |
| step descriptions | message bodies |
| tool names | contact names |
| statuses and verification outcomes | any credential |
| timestamps | anything from an injected prompt |

## 6. Do not

- Write anything on the voice path that could delay a turn. A write per step is
  acceptable; a synchronous fsync per step is not.
- Store a `PermissionRequest` id. Approvals are session-bound by design
  ([`permission_gate`](../../agent/runtime/permission_gate.py): *"Nothing is
  persisted: a restarted worker starts empty"*) and that must stay true.
- Resume a task automatically across a restart. Loading it so it can be
  **inspected, reported and cancelled** is the slice; auto-resume is not.

## 7. Tests

- round-trip fidelity for every status
- `interrupted`, not `failed`, on restart
- a corrupt file is **reported, never silently defaulted** — `focus_store.py`
  (removed along with the Focus workspace) set this precedent, and overwriting a
  document with defaults is how a person
  loses a term of history and is never told
- the atomic write leaves no partial file
- a loaded task can be cancelled
- the permission gate is still empty after a restart

## 8. Exit criteria

- kill the worker mid-task, restart, and the task is listed, described, and
  cancellable
- no secret or message body exists anywhere under `.lumine/`
- `2232+ tests pass`
