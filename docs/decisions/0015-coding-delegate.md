# 0015 — Lumine delegates engineering work to external coding agents

- **Status:** Proposed — **not accepted.** Acceptance is bound to Phase 11
  approval; until then [08-coding-delegate.md](../agent-runtime/08-coding-delegate.md)
  is a specification, not permission to build.

## Context

Two recorded positions bear directly:

- *"Lumine is not a coding agent"* ([runtime README](../agent-runtime/README.md),
  GSD table) — still intended true. She delegates; she does not become one. The
  distinction the original record protected — her *shape* as a voice companion,
  not a repo tool — is preserved by every structural rule of Phase 11.
- *"Thin orchestrator + specialists … do not extend to subagents"* (same table),
  and Phase 9's *"do not add subagents as a general mechanism"* — that refusal
  was about **fresh-context workers inside the agent process**. A Phase 11
  delegate is a different creature: an external process the user granted,
  watched on the task's events, never holding a permission request of its own.

What made this a decision rather than a feature: giving Lumine the ability to
write code includes the ability to write *her own* code — her gate, her policy
keys, her audit log. A code edit is the "click on your own window" refusal in
slower motion. So the boundary is not what she may edit; it is **who merges**.

## Decision

Lumine delegates to user-approved external coding agents:

- **Worktree-isolated.** One git worktree + branch per task; nothing outside a
  worktree is writable ([ADR-0016](0016-filesystem-write-scoped-worktrees.md)).
- **Propose-only.** The delegate's diff is a proposal. Nothing merges without
  the user acting in the audit trail.
- **Human-merge-only.** No auto-merge on green tests. If a person-controlled
  lane is ever revisited, it is with its own measurement section.
- **No hot reload.** The worker never reloads itself; the merge-and-restart is
  the existing restart path.
- **Credential sanitization** derived from `agent/settings/providers.py` — the
  child environment inherits no `LUMINE_*` value and no catalog slot name.
- **`code.delegate` is `destructive`.** Its approval is the one grant; per-file
  writes inside the worktree do not raise separate cards, because the grant is
  the delegation.
- **Bounded loop.** Two repair attempts inside the same worktree, then a report.
- **Orphan reaping by identity and ownership, never by pid alone.** A lease
  records task id, run id, pid, process creation time, executable, worktree
  path. A reaper kills only what matches all of it; an unverified process is
  left alive, the task marked `uncertain`, and the user asked.

## Why not the alternative

**Rejected: she edits her own source directly.** The running worker rewriting
its own authority is the exact structure the own-window refusal prevents in
desktop control.

**Rejected: gate on green tests (auto-merge).** Tests measure code, not
authority. The merge is where a human sees the intent, and it stays a human act.

**Rejected: a general subagent mechanism.** Phase 9's refusal stands; this is a
tool, not a spawn.

## Consequences

- The `code.*` family enters the registry through the normal door; the Tools
  page and every card behave as `tool_catalog.py` already asserts.
- Phase 11 cannot be approved without [ADR-0016](0016-filesystem-write-scoped-worktrees.md).
- Retention: worktrees are retained on `uncertain`/timeout/cancel and pruned
  only after the artefact is written and completion confirmed, under the
  central retention service.
- A wrong project, a missing test command, or an unregistered project is its
  own refusal sentence — never a guess at a repo.

## Revisit when

The delegate pattern is shown unsafe — a credential leak, an out-of-worktree
write, or a merge without evidence in the audit trail. Reversal is stopping
`code.*` at the registry; no data migration, no user-visible permission state
to undo.
