# 0016 — `filesystem.write` is scoped to Phase 11 worktrees, and nowhere else

- **Status:** Proposed — **not accepted.** Acceptance is bound to Phase 11
  approval; until then there is **no** `filesystem.write` in this codebase, and
  [Phase 6](../agent-runtime/05-capabilities-and-context.md#phase-6--read-only-filesystem)
  remains read-only.

## Context

The media tool's docstring states the standing product decision plainly:
*"She never moves, renames or deletes a file, and there is no verb that
would."* Phase 6 (`filesystem.list` / `search` / `read`) is the instantiation:
every row is `read`, none reaches the permission gate, because the gate governs
effects and reads have none.

Phase 11 needs a genuine write capability — but the obvious version of it (a
model-callable `filesystem.write(path, text)`) is the one authority a voice
companion must never carry: a writable path is a mutable authority, and
prompt-injection defence cannot make one safe.

## Decision

**`filesystem.write` exists as a capability row, and never as a model-facing
tool.** Its entire authority is transferred to the Phase 11 delegate run.

1. **Non-selectable.** The row carries `selectable: False` — the Tools page and
   the catalog never show it as a capability the model can invoke.
2. **Scoped to one worktree.** The grant is the `code.delegate` approval
   (`destructive`), bound to a single task's worktree path. Per-file writes do
   not raise separate cards: the delegation *is* the grant.
3. **Resolved-then-compared path discipline**, the shape `whatsapp_files.py`
   and `library.resolve_track` already proved: resolve first, compare after, so
   `..`, an absolute path and a symlink pointing out all land in the same
   refusal. The main checkout is read-only to everything in Phase 11.
4. **No move, rename, or delete.** Idempotent content writes and git
   operations inside the worktree; nothing outside it. Deletion of the
   worktree is the retention service's job, never the model's.
5. **Phase 6 stays read-only.** Adding writes as a side effect of reads was
   never the deal, and this record does not change Phase 6.

## Why not the alternative

**Rejected: a model-callable `filesystem.write` tool.** It would make Lumine
one prompt injection away from editing her own gate through a file on disk —
the same structural hole the own-window refusal closes for the desktop, and
the one `wrap_untrusted` exists to keep third-party text out of.

**Rejected: extending Phase 6 with a `write` verb.** Phase 6 is read-only by
decision, and an authority shoe-horned into a phase that did not need it would
be invisible in the approval wording.

## Consequences

- Phase 11's dependency on Phase 6 is *path discipline*, not write authority;
  the write authority is this record.
- The registry reserves the id and the gate enforces the class; a static test
  asserts the row is non-selectable and no surface resolves to it.
- Worktree writes are audited as delegate operations carrying the same
  lease/run id, so the audit trail answers "who wrote this file" with the run,
  not with "the model."

## Revisit when

A workload needs model-invoked file writes that are *not* coding delegation.
That is a new record, not an extension of this one — and it will need its own
approval wording, because the injection boundary is the cache of the decision.
