# Phases 6, 7 and 8 — Capability and Context

The first three phases in this track that add **user-visible** capability. Each
is deliberately narrow, and each is scoped by an existing boundary rather than a
new one.

---

# Phase 6 — Read-only filesystem

- **Status:** specified, **not approved.**
- **Negative-adjacency, recorded:** `filesystem.write` is **not** a Phase 6
  feature, now or later. Any write authority enters the tree
  [ADR-0016](../decisions/0016-filesystem-write-scoped-worktrees.md)-scoped and
  Phase-11-bound — a worktree the delegate owns, never a user path the model
  may edit. This phase cannot be cited by Phase 11 as satisfying a write need;
  the ADR is the write authority.

## 1. The slice

```text
filesystem.list      filesystem.search      filesystem.read
```

Lumine can read a file the user names. That is the whole slice.

## 2. Why read-only

The media tool's docstring states the product decision plainly:

> *"She never moves, renames or deletes a file, and there is no verb that would."*

`filesystem.delete` is exactly what this codebase decided not to have. It can be
added later, with its own permission class and its own approval wording — but not
as a side effect of adding reads.

## 3. Do

- Roots: `LUMINE_FS_ROOTS`, defaulting to the same Downloads/Pictures/Desktop set
  the attachment check already uses
  ([`whatsapp_files.py`](../../agent/tools/messaging/whatsapp_files.py)).
- **Resolve first, then compare.** `library.resolve_track` and
  `whatsapp_files.py` are two existing consumers of this exact shape, and one
  omission of it made `scan(root=…)` return nothing at all for a valid library,
  silently. `..`, an absolute path and a symlink pointing out must all land in
  the same refusal.
- Every result through `tool_results.wrap_untrusted`. **A `.env` on disk is a
  prompt-injection vector**, and a file is exactly as untrusted as a web page.
- Cap file size on read, and clip list output.
- Register the rows in the Phase 1 registry, which already reserved the ids.

## 4. None of these reach the permission gate

All three are `read`. That is correct, not an oversight: **the gate governs
effects, and these have none.** A tool that cannot change anything does not need
permission to change nothing.

This is worth stating explicitly because it looks like a gap and is not.

## 5. Do not

- Add `write`, `move`, or `delete`.
- Read outside the allowlisted roots by any route.
- Load a large file whole.
- Follow a symlink out of the roots, even to a file the user can obviously see.

## 6. Negative scope — must not implement

- No `filesystem.write` row of any kind — not "as a config option", not "for
  Phase 11's convenience": [ADR-0016](../decisions/0016-filesystem-write-scoped-worktrees.md)
  is the write authority and it is worktree-only.
- No rename, move, delete, or trash verbs.
- No `.lumine/` reads that expose the task store's internals to the model as
  files — the model already gets summarized, bounded context; the raw task
  evidence is not a filesystem read away.
- No network fetches, no package installs, no `open_app` — the family is one
  read family, by decision, and growing it is growing authority.

## 7. Tests

- `..`, absolute, and symlink-out all refuse, each by name
- a resolved short `8.3` path inside the root is accepted (the `scan(root=…)`
  regression)
- contents are wrapped as untrusted
- an oversized file is clipped, not truncated mid-encoding
- the boundary is identical to the attachment check's, asserted against it

### 7.1 Acceptance criteria (provisional)

- **0 escapes** across the fixture corpus (`..`, absolute, symlink-out,
  junction) — asserted per name
- **0 unwrapped contents** — every result through `tool_results.wrap_untrusted`
- oversized reads clamp at the cap; a clipped read is never mid-encoding
- `tool_catalog.py --check` unchanged, cards byte-identical
- `2232+ tests pass`

---

# Phase 7 — Selective context retrieval

- **Status:** specified, **not approved.**
- **Depends on:** Phase 1.5 (the budget counts the *loaded* tool set, not a
  static one) and Phase 5 (tool/runtime state events feed layer 5). Phase 6
  precedes it in numbering only — the sequence, not a dependency.

## 1. The five layers

| Layer | Status |
|---|---|
| 1 — current conversation | ✅ `context_trim.py` |
| 2 — current task | Phase 7 |
| 3 — project context | **rejected** — see §3 |
| 4 — user preferences | Phase 7 |
| 5 — tool/runtime state | Phase 7 |

Only the trimmer exists, and it is a **backstop, not a strategy** — its own
docstring says so.

**Where the 2026-10-30 "context manager" proposal lands:** here, as this phase's
layers 2/4/5 — relevance tagging, summarising old turns, keeping durable facts
separate from task context, removing redundant tool outputs. Its one hard rule
is the separation of systems: **conversation history is not persistent
memory** ([companion Phase 7 owns the durable side](../../ROADMAP.md#phase-7--memory-context--personality)),
and *memory never influences a permission decision*. Summarisation must be
measured before it is ranked against the Phase 9 verdict that GSD's context-rot
cures were refused for voice turns — bounded, local, charged against the
session, never a subagent. Full mapping: [expansion reconciliation §A](09-expansion-reconciliation.md).

## 2. Do

Layers 2, 4 and 5. Each must **justify its tokens at the point it is added**,
against the principle the request itself stated:

> *Give each reasoning call the smallest amount of context necessary to make a
> correct decision.*

The cost that governs this is measured: `tools_policy.py` records the whole
instruction set at **~4.7k tokens per completion**. *(That figure predates the
registry at 17 tools and the measured ~8.5k tokens counted in
[Phase 1.5 §1](07-capability-discovery.md#1-the-measured-problem) — the
correction stands, and it makes the rule stricter, not looser.)* A tool result
is re-sent on every later turn, so a context layer that grows without bound
is a per-minute bill.

### 2.1 Budgets (provisional, pending measurement)

| Layer | Cap per turn | Note |
|---|---|---|
| 2 — current task | ≤ 300 tokens | task goal + step state, no tool payloads |
| 4 — user preferences | ≤ 200 tokens | durable facts only |
| 5 — tool/runtime state | ≤ 300 tokens | what the gate/engine *needs*, not what happened |
| **layers 2/4/5 combined** | **≤ 800 tokens p95** | measured over a 50-turn session |
| summaries | ≤ 200 tokens each | written once, read many |

What "measured" means here: `estimate_tokens` at injection time, persona
excluded, the same way [Phase 1.5](07-capability-discovery.md) measures its
catalog — and the caps are revised **downward**, never upward, without a
per-phase note explaining what new evidence forced the change.

## 3. Do not implement `.lumine/projects/`

The request proposed:

```text
.lumine/projects/<name>/{context.md, goals.md, state.md}
```

**Rejected.** Those describe a *repository*. GSD is a coding agent, and its
project layout encodes a repo's identity, goals and state. Lumine's projects are
**the user's** — a browser automation effort, a study plan, a side project. A
`.md` file with a `goals.md` beside it imports a coding-agent concept into a
companion, and the user would have to learn a vocabulary to ask for their own
work back.

If this is ever wanted, it should be designed from what a *person* would call a
thing they are working on — not from GSD's layout.

## 4. Do not

- Inject every layer on every request. That is the failure the request named.
- Put memory into a permission decision. Memory may never influence whether an
  action is allowed — it is context, not consent.
- Keep a transcript. Memory retrieval must be selective, bounded, and
  user-visible and deletable.

### 4.1 Acceptance criteria (provisional)

- layered-injection p95 across a 50-turn session ≤ 800 tokens combined
  (definition in §2.1)
- each layer's tokens *justified at the point added*: the injection records
  what it added and why, and a test fails on a bus "always add" drift
- summaries ≤ 200 tokens, ≤ 1 per N turns, written once and read many —
  never regenerated per turn
- 0 permission-decision code paths read a memory store — asserted by source
  check, not by intention
- on the baseline corpus, token cost reduced ≥ 20% and task success
  **non-inferior** — the A/B is the gate, the token number alone is not

## 5. Negative scope — must not implement

- **No `.lumine/projects/`** — see §3, rejected with reasons.
- No subagent summariser — the Phase 9 verdict stands; summarisation is
  bounded and local, charged against the session.
- No memory influence on permission decisions — the gate reads policy, never
  preference.
- No voice replay of old turns — the model may see history; the user does not
  re-hear it.
- No unbounded context layer; every layer has a cap or it is not shipped.
- No second context system beside `context_trim.py` — this phase replaces the
  backstop *assumption* with a strategy; the trimmer stays as the backstop.

---

# Phase 8 — LLM planner fallback

- **Status:** specified, **not approved.**
- **Depends on:** Phase 2 (the runtime/planner seam), Phase 5 (events and
  trace), Phase 7 (the context discipline the fallback inherits).

## 1. The problem

`task_runner.plan_for` is a deterministic scenario table. A route matching no
scenario returns `Plan(note="nothing to do for that")`, which the tool turns into
a question.

That is the **right default** — `plan_for`'s docstring gives the reason: *"the
failure mode of a general planner is a plan nobody can read"* — and it is also a
hard ceiling. Some requests simply are not the scenarios anyone anticipated.

## 2. The slice

- The deterministic table **remains authoritative** for every scenario it matches.
- The LLM planner runs **only** when the table matches nothing.
- Its output is validated by the same `task_model.validate_plan` **before**
  execution, so a cycle or a dangling dependency is refused at construction rather
  than hanging the engine.
- Capped by the existing `MAX_STEPS = 8`.

## 3. Do not

- Replace `plan_for`.
- Let the model author an `action_class`. `STEP_ACTION_CLASS` exists so a plan
  cannot describe a Delete click as an ordinary one.
- Emit a step whose name is not in `STEP_IMPLEMENTATIONS`. A missing step must
  fail validation, not at run time — *"the difference between 'that plan cannot
  run' and 'the third step failed'"*.
- Let a fallback plan run more steps than the deterministic one would.

## 4. Tests

- every existing scenario produces the **same** plan as before, byte for byte
- an unmatched route now produces a plan rather than a question
- a malformed LLM plan is refused by validation, not executed
- `MAX_STEPS` is enforced
- a fallback plan cannot carry a lighter action class than the call site enforces

### 4.1 Acceptance criteria (provisional)

- 100% of the existing scenario corpus returns the identical plan it returns
  today (golden file)
- every malformed LLM plan is refused at validation, 100% — never at run time
- fallback `MAX_STEPS` respected in every case
- 0 model-authorable authority-class downgrades (property test)
- the fallback's own latency stays on the cascade path (never a realtime
  reconnect) — measured, provisional ceiling 2 s p95

## 5. Negative scope — must not implement

- No replacing of `plan_for` — the deterministic table stays authoritative
  for every scenario it matches, forever in this phase.
- No model-authored authority class; no "hints" that can lighten a class.
- No step names outside `STEP_IMPLEMENTATIONS` — a plan that can't run must
  fail at construction, never mid-execution.
- No planner that runs when the deterministic table matches — escalation only
  on unmatched routes.
- No second plan vocabulary; the fallback writes into the existing plan shape
  or it fails validation.
