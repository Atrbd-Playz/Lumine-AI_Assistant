# Phases 6, 7 and 8 — Capability and Context

The first three phases in this track that add **user-visible** capability. Each
is deliberately narrow, and each is scoped by an existing boundary rather than a
new one.

---

# Phase 6 — Read-only filesystem

- **Status:** specified, **not approved.**

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

## 6. Tests

- `..`, absolute, and symlink-out all refuse, each by name
- a resolved short `8.3` path inside the root is accepted (the `scan(root=…)`
  regression)
- contents are wrapped as untrusted
- an oversized file is clipped, not truncated mid-encoding
- the boundary is identical to the attachment check's, asserted against it

---

# Phase 7 — Selective context retrieval

- **Status:** specified, **not approved.**

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

## 2. Do

Layers 2, 4 and 5. Each must **justify its tokens at the point it is added**,
against the principle the request itself stated:

> *Give each reasoning call the smallest amount of context necessary to make a
> correct decision.*

The cost that governs this is measured: `tools_policy.py` records the whole
instruction set at **~4.7k tokens per completion**. A tool result is re-sent on
every later turn, so a context layer that grows without bound is a per-minute
bill.

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

---

# Phase 8 — LLM planner fallback

- **Status:** specified, **not approved.**

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
