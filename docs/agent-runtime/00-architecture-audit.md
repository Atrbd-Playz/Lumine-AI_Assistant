# Phase 0 — Architecture Audit

- **Status:** Complete (2026-10-07)
- **Baseline recorded:** `2232 tests passing, 3 skipped`
- **Command:** `python -m unittest discover -s agent\tests -p "test_*.py"`

This document exists so that no later phase re-derives what was already found.
Every claim here was verified against the source, not inferred from prose.

---

## 1. The finding

The request that opened this track described Lumine as *"LLM + function tools"*
and asked for conversion into a structured agent runtime.

**That premise is out of date.** The verification ladder, the task model, the
deterministic executor, the permission manager, the router, and the freshness
guard all exist, are tested, and in several cases are stricter than the request
asked for.

The consequence for planning: this is an **additive** track. Phases 1, 2 and 5
consolidate authority without changing behaviour. Building parallel machinery
because the brief asked for it would have been the failure mode, not the fix.

---

## 2. Verified inventory

### Complete and good

| Component | Location | Assessment |
|---|---|---|
| Verification vocabulary | `runtime/verification.py` | `requested`/`dispatched`/`accepted`/`observed`/`failed`/`timed_out`/`uncertain`. The ladder ascends only on evidence; `observed` implies the three below it and nothing implies `observed`. `uncertain` is load-bearing: it is the only value that makes a retry a *decision* |
| `Task` / `TaskStep` | `runtime/task_model.py` | Dependencies **gate** rather than order. Cycles refused at construction, not detected at runtime. Sticky cancellation. `RetryPolicy` separates `max_attempts` from `retry_safe` |
| `Executor` | `runtime/task_engine.py` | Checkpoint after every step: classify, update, re-check the next step's preconditions. Cancellation checked before *and* immediately before the call. Bounded read concurrency; anything that acts runs alone |
| `PermissionManager` | `runtime/permission_gate.py` | Voice **and** UI channels. Session-bound, never persisted. Hedges resolve nothing. Duplicates refused. Six distinct fail-closed paths. SHA-256-digested JSONL audit |
| Intent routing | `runtime/router.py` | Pure: request + machine facts → environment. Five environments. Ordered by evidence strength. Ambiguity produces a question, not a coin flip |
| Observation | `tools/desktop/capture.py`, `tools/browser/state.py` | Generations per tab, `confirm_target` re-walks the tree, full UIA tree, `candidate_windows` filters for usefulness rather than z-order |
| observe→act→observe→verify | `task_runner` step deps, generation checks | `desktop_smoke.py` found the vacuous `check_generation` that every unit test had fed a number from `_LAST` |
| Structured results | `tools/tool_results.py` | Budget applied to the **structure**, not a rendered string. Injection guard |
| Tool registry | `tools/tools_registry.py` | 17 tools, `TOOL_CARDS`, 5 categories, cheapest-first |
| Tool ≠ permission | `tools/tools_permission.py` | Two switches failing in **opposite** directions, deliberately |
| Config precedence | `settings/config_store.py` | saved document > `.env` > defaults, resolved per key |
| Persona split | `prompts/persona_core.md`, `persona.md` | Always-sent core, on-demand full. `test_persona_split.py` guards the ratio |

### Partial

| Component | Location | Gap |
|---|---|---|
| Planner | `tools/task_runner.py::plan_for` | A deterministic scenario table. A route matching no scenario returns `Plan(note=…)` → a question to the user. Correct default; a hard ceiling |
| Capability metadata | `desktop/ops.py`, `messaging/whatsapp_ops.py`, `tools_permission.TOOL_ACTION_CLASS`, `task_runner.STEP_ACTION_CLASS` | **Four sources.** Two already compute rather than declare |
| Recovery | `RetryPolicy`, `ops.hold_seconds`, `capture.confirm_target`, WhatsApp ledger | The ladder exists implicitly and nowhere as a statement |
| Event bus | `runtime/runtime_events.py` | An emitter, not a bus. No subscribers |
| Context management | `runtime/context_trim.py` | One of five layers, and it is a **backstop**, not a strategy |

### Absent

| Component | Why it matters |
|---|---|
| `AgentRuntime` | The lifecycle is assembled across three layers with no single owner |
| Persistent task state | `Task` is an in-memory dataclass. A worker restart loses an in-flight task, and the user is told nothing |
| Memory / project context | `focus_store.py` (removed with the Focus workspace) was a *reader* of one file. There is no `.lumine/` |
| Worker abstraction | No subagent concept at all |
| `IntentManager` / complexity classifier | The model decides implicitly |
| Filesystem capabilities | — |
| Risk axis (`low`/`medium`/`high`/`critical`) | `read`/`act`/`sensitive`/`destructive` are **authority**, not risk. A different axis |

---

## 3. Three class-resolution models (the finding that drives Phase 1)

The most important technical result of the audit. Capability class is resolved
**three different ways** today, and a registry that assumed one model would have
been a silent regression.

### 3.1 Static per-verb

The class is fixed. `computer.press` is `sensitive` because Enter is a submit in
a chat box, a search with autosubmit, and most settings dialogs — not because of
the name of what it happens to hit.

→ the registry stores a plain `action_class`.

### 3.2 Computed at the call site

The class depends on the target. Clicking **Send** is `sensitive`; clicking
**Next** is `act`. This is the same reasoning as `browse`'s class coming from the
DOM.

→ the registry stores `element_dependent=True` plus a **`strict_class` ceiling**.
The call site (`ops.classify`, `browser.actions`) keeps computing the real class.
The registry does not second-guess it.

### 3.3 Declared floor, underivable

`run_task` is `destructive` because *"a plan the model wrote may contain a step we
have not read yet."* Its floor must be safe for the plan that arrives, not the
one that was expected. **No computation over its own capabilities can produce
this value.**

→ it stays a **declared** entry, not a derivation.

### 3.4 A fourth nuance

`whatsapp_ops.tool_action_class()` computes from **`selectable_operations(tool)`
only**. A non-selectable verb must not raise a tool's badge, or the badge would
describe a capability that does not exist and would stay `destructive` long after
the verb is gone. Load-bearing; must survive.

---

## 4. GSD → Lumine

| GSD concept | Lumine equivalent | Verdict | Reason |
|---|---|---|---|
| Context rot | `context_trim`, `tool_results` | Keep symptom, **refuse cure** | A 200k window does not exist here. Subagents add latency and cost against a per-minute budget |
| `.planning/` file state | — | **Adapt** → Phase 3 | Genuinely missing |
| GSD projects | — | **Refuse** | Those describe a repo. Lumine's projects are the user's |
| Discuss→Plan→Execute→Verify→Ship | `task_model` + `task_engine` | Already have it | Same shape |
| Thin orchestrator + specialists | `task_runner` | Keep; do not extend | — |
| Deterministic progression | `task_engine` | Already have it | — |
| Verification gates | `verification.py` | Already have it | Ours has a ladder |
| Safety gates | `permission_gate` | Already have it | — |
| Absent = enabled | `tools.<id>` = on | **Do not unify** | Opposite failure directions are deliberate |
| Research agent | — | Adapt, Phase 9 | The one genuine transfer |

---

## 5. Conflicts between the request and deliberate existing decisions

Three places where the request asked for something the codebase had already
decided against. Each is recorded because the decision was made on purpose and
the reasoning is in the source.

### 5.1 "The LLM should classify arbitrary goals"

`plan_for` is a deterministic scenario table whose docstring states: *"The
failure mode of a general planner is a plan nobody can read."*

**Resolution:** deterministic stays authoritative. An LLM planner runs only for
routes matching no scenario, and is Phase 8.

### 5.2 Dotted tool namespaces

A literal split of `browse(action=…)` into eight function tools was considered
and rejected on a **measured** cost: `tools_policy.py` records instructions plus
tool schemas at **~4.7k tokens per completion** — *"more than half the minute"* of
Groq's free 8,000 TPM — and encodes *"one job is one tool."* Gemini Live exposes
no `parallel_tool_calls` switch, so there is no provider-side backstop.

**Resolution:** dotted ids are the **canonical authority**; the verb-parameterised
tool stays the **efficient model-facing interface**. See
[ADR-0014](../decisions/0014-canonical-capability-namespace.md).

### 5.3 Filesystem tools are a real authority increase

The media tool's docstring states: *"She never moves, renames or deletes a file,
and there is no verb that would."*

**Resolution:** read-only (`list`/`search`/`read`) only, scoped to allowlisted
roots, mirroring the attachment path check that already exists. Phase 6.

---

## 6. Risks carried forward

| Risk | Mitigation |
|---|---|
| Voice regression — `gemini_live` has no TTS stage, so `session.say()` raises and the turn dies | Every new path tested against a session whose `say()` raises |
| The namespace migration breaks the Tools page or existing config | Legacy keys keep working; `tool_catalog.py` output asserted byte-identical |
| `.lumine/` becomes a privacy leak | Explicit allow-list of persisted fields; no message bodies, no credentials |
| Over-building, which is this track's own named failure mode | Phases 1, 2, 5 add no user-visible capability |
| Losing the reason behind a decision | Every new module carries a docstring stating why, matching the house style |

---

## Next

[`01-capability-registry.md`](01-capability-registry.md) — approved, ready to build.
