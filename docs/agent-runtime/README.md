# Agent Runtime — Goals and Phases

The second engineering track. [`ROADMAP.md`](../../ROADMAP.md) is the **companion**
track — wake, island, lifecycle, memory, proactive. This directory is the
**runtime** track — the internal machinery that decides, plans, executes,
observes, verifies, recovers, and persists.

They are separate because they fail differently. The companion track is about
what a person *sees*. The runtime track is about what the worker *does* when a
person says something.

| | Companion track | Runtime track (this) |
|---|---|---|
| Question | "What does Lumine look and sound like?" | "What does Lumine actually do?" |
| Governed by | [`VISION.md`](../../VISION.md) | this directory |
| Consumer | the user | the model, the planner, the gate |
| Surface | island, tray, full UI, voice | none — invisible by design |

**Status: Phase 0 complete. Phase 1 approved. Phases 1.5 and 2–11 specified,
not approved** (1.5 = [capability discovery](07-capability-discovery.md); 11 =
[coding delegate](08-coding-delegate.md); ADRs [0015](../decisions/0015-coding-delegate.md)
and [0016](../decisions/0016-filesystem-write-scoped-worktrees.md) are **drafts**,
status *Proposed — not accepted*).

**Dependency graph, acceptance matrix, and the cross-document consistency
audit:** [`10-consistency-audit.md`](10-consistency-audit.md). Every
provisional threshold and every negative scope lives there or in the phase
doc that owns it.

---

## The goal

> **Lumine should not become a collection of tools. Lumine should become an agent
> runtime that knows when, why, and how to use its tools.**

Tools are capabilities. Tasks are goals. The planner decides how a goal becomes
actions. Permissions decide what Lumine is allowed to do. Observation tells her
what is actually happening. Verification tells her whether it worked. Recovery
lets her continue when reality differs from the plan. Memory lets her continue
across time. The personality layer makes all of it feel like **Lumine**.

### Definition of done

The runtime is done when **all** of these are true. This is the checklist the
phases are sized against, and it is deliberately not "the classes exist".

- [ ] There is one owner of the agent lifecycle (`AgentRuntime`) — and
      **cancellation has one work-scope owner plus a separate turn scope**;
      acknowledgement and termination are reported separately
- [ ] Tool context is loaded on demand — the always-sent set is the catalog, not every schema ([Phase 1.5](07-capability-discovery.md)); **the loader is session-owned, injected, event-driven, and never reads task state**
- [ ] Capabilities have canonical dotted identities with **one** authority
- [ ] Tasks carry an **embedded effect ledger** — every potentially-executed
      effect persisted before invocation, `may_have_happened` surviving crashes
- [ ] **Trace identity has one owner** (Phase 5): `task_id` per task,
      `trace_id` per execution turn, `event_id` per event
- [ ] **Retention is centralised, configurable, bounded** — uncertain and
      actively reviewed evidence is never auto-deleted
- [ ] Every phase doc carries a **canonical negative scope** section
- [ ] Coding agents can be delegated repaired, worktree-isolated work — propose-only, human-merge-only ([Phase 11](08-coding-delegate.md), [ADR-0015](../decisions/0015-coding-delegate.md))
- [ ] Tasks have persistent state that survives a worker restart
- [ ] Complex requests are decomposed into dependency-ordered steps
- [ ] Steps execute independently, with bounded concurrency
- [ ] Permission is separate from tool availability, and says so structurally
- [ ] Every side-effecting action is observed and then verified
- [ ] A failed action triggers a bounded recovery ladder
- [ ] Context is selectively loaded, never dumped
- [ ] Long subtasks can run in an isolated context
- [ ] The UI can receive task/runtime events
- [ ] LiveKit remains **an interface**, not the architecture
- [ ] Existing voice functionality keeps working
- [ ] The core lifecycle is covered by tests
- [ ] Future tools are added without editing the core agent

---

## The finding that shaped this track

The request that started this was to evolve Lumine from *"LLM + function tools"*
into a structured agent runtime. **The audit found that premise out of date.**

The primitives already exist, are tested, and in several places are stricter
than the request asked for:

| Asked for | Reality |
|---|---|
| Verification vocabulary | `runtime/verification.py` — a 7-value evidence ladder |
| `Task` / `TaskStep` | `runtime/task_model.py` — complete |
| `Executor` | `runtime/task_engine.py` — complete |
| `PermissionManager` | `runtime/permission_gate.py` — complete |
| Intent routing | `runtime/router.py` — pure, fully tested |
| Observation + freshness | `tools/desktop/capture.py`, `tools/browser/state.py` — complete |
| Structured results | `tools/tool_results.py` — complete |

So the work is **consolidation, not construction**. Full findings:
[`00-architecture-audit.md`](00-architecture-audit.md).

---

## Phases

| Phase | Name | Status | Adds user-visible capability? |
|---|---|---|---|
| 0 | Architecture audit | ✅ done | no |
| **1** | [`CapabilityRegistry` — canonical dotted ids](01-capability-registry.md) | **✅ approved** | **no** |
| 1.5 | [Capability discovery — deferred tool exposure](07-capability-discovery.md) | specified, not approved, **consumes Phase 1** | partly |
| 2 | [`AgentRuntime` façade](02-agent-runtime.md) | specified | no |
| 3 | [Persistent task store](03-persistent-task-store.md) | specified | **yes** — survives restart |
| 4 | [`RecoveryEngine`](04-recovery-and-events.md#phase-4--recoveryengine) | specified | partly |
| 5 | [`EventBus`](04-recovery-and-events.md#phase-5--eventbus) | specified | no |
| 6 | [Read-only filesystem](05-capabilities-and-context.md#phase-6--read-only-filesystem) | specified | **yes** |
| 7 | [Selective context retrieval](05-capabilities-and-context.md#phase-7--selective-context-retrieval) | specified | partly |
| 8 | [LLM planner fallback](05-capabilities-and-context.md#phase-8--llm-planner-fallback) | specified | partly |
| 9 | [Research worker](06-research-and-proactive.md#phase-9--researchworker) | specified | **yes** |
| 10 | [Proactive Lumine](06-research-and-proactive.md#phase-10--proactive-lumine) | deferred | **yes** |
| 11 | [Coding delegate](08-coding-delegate.md) | specified, not approved — **ADR 0015 required first** | **yes** |

**Phases 1, 2 and 5 add no user-visible capability.** That ordering is the point:
the foundation is made explicit before anything is built on top of it. A registry
nothing reads is not progress.

> **Beyond the phases:** the 2026-10-30 expansion proposal (context manager,
> unified cancellation, idempotency, authority model, observability, replay and
> friends) is mapped into these phases item by item in
> [`09-expansion-reconciliation.md`](09-expansion-reconciliation.md) — most of it
> extends what is already built, none of it authorises work. The twelve
> decisions reconciling it are resolved in
> [`10-consistency-audit.md`](10-consistency-audit.md), and the two ADRs that
> amend recorded positions are filed there as drafts:
> [0015](../decisions/0015-coding-delegate.md),
> [0016](../decisions/0016-filesystem-write-scoped-worktrees.md).

---

## Principles binding every phase

1. **Additive and conservative.** Consolidate authority; never rewrite working
   behaviour to make metadata look tidier.
2. **LiveKit independence.** Nothing in `runtime/` or the capability registry may
   import LiveKit. Voice is one interface into the runtime, not the runtime.
3. **No new voice execution path.** The default `gemini_live` profile has no TTS
   stage, so `session.say()` raises and the turn dies. Use
   `runtime/voice_line.speak_line`, which already handles the escalation.
4. **One vertical slice at a time** ([ADR-0011](../decisions/0011-no-overbuild.md)).
   Phases preserve direction; they do not authorise work.
5. **Tool availability is never permission.** `AVAILABLE` and `AUTHORIZED` stay
   separate types, separate switches, and separate tests.
6. **Fail-open and fail-closed stay opposite.** A key that only *reduces*
   authority reads an unrecognised value as `on`; a key that *grants* a capability
   reads it as `off`. Do not unify them.
7. **A broken mechanism degrades to a refusal**, never to a plausible wrong action.
8. **A telemetry write never raises.** A dead pipe once killed every session.
9. **Never log secrets, keys, tokens, or message bodies.**
10. **Docstrings carry the *why*** — usually because the obvious version was tried
    and measured first. New modules match the house style.
11. **Every phase doc carries `## Negative scope — must not implement`** —
    adjacent-phase features named by number and name, so scope creep has to
    walk past a list. The canon — heading, assembly rule, per-phase index —
    lives in
    [`10-consistency-audit.md §10`](10-consistency-audit.md#10-the-negative-scope-canon).
12. **Numeric thresholds are provisional until proven** — [§2](10-consistency-audit.md#2-phase-acceptance-matrix)
    of the consistency audit records the definition and the feasibility note a
    number needs before it becomes a gate.

---

## What Lumine will be able to do

Not a list of verbs. A list of **situations**, with the phase that delivers each.

| Situation | Phase |
|---|---|
| "Open Notepad." | ✅ now |
| "Open Chrome, search YouTube for X, play it, start a 25-minute focus session." | ✅ now (`run_task`) |
| "What's the weather?" — one tool call, no task, no ceremony | ✅ now |
| "Send this WhatsApp message." — draft on your screen, ask, send, verify | ✅ now |
| "Delete this file." — **refused, because the capability does not exist** | ✅ now, by design |
| …and the task survives a worker restart | Phase 3 |
| …and the schemas she carries stay small, whatever she can do — the catalog is always present, the details load only when needed | [Phase 1.5](07-capability-discovery.md) |
| …and a browser failure re-observes and recovers instead of lying | Phase 4 |
| …and a long research runs without filling the conversation | Phase 9 |
| "Read the notes file I keep in Documents." | Phase 6 |
| "Continue where we left off." — task and preference context, selectively | Phase 7 |
| "Find something on my PC I half-remember." — a request the scenario table cannot express | Phase 8 |
| "Research this and tell me when you're done." | Phase 9 |
| "Add a feature to yourself, test it, and tell me when it's ready." — spec read back, a coding agent works, she reports what *may* be merged | [Phase 11](08-coding-delegate.md) |
| "Hey Lumine, what's the weather?" — one interaction, window never opened | [companion Phase 1](../../ROADMAP.md#phase-1--always-on-wake-runtime-first-vertical-slice) |

The last row is not this track, and that is the point: **the runtime is built so
the companion track can land on top of it.** None of the rows above require the
full UI to be open.

---

## GSD: what was taken, and what was refused

The architectural inspiration was [GSD Core](https://github.com/open-gsd/gsd-core),
a context-engineering and spec-driven system for coding agents. **It was studied
and adapted, not forked.** Lumine is not a coding agent, and GSD's terminology
does not enter the user-facing product.

| GSD concept | Lumine equivalent | Verdict | Reason |
|---|---|---|---|
| Context rot | `context_trim`, `tool_results` | Keep symptom, **refuse cure** | Voice turns are short; there is no 200k window to protect. Subagents would add latency and cost against a per-minute budget |
| `.planning/` file state | `.lumine/tasks/` (Phase 3) | **Adapt** | Genuinely missing; the highest-value item here |
| Spec-driven phases | `task_model` + `task_engine` | **Already have it** | Same shape, different names |
| Thin orchestrator + specialists | `task_runner` | Keep; do not extend to subagents | — |
| Deterministic progression | `task_engine` | **Already have it** | Matches GSD's own reasoning |
| Verification gates | `verification.py` | **Already have it** | Ours is stricter — it has a ladder |
| Confirm/Quality/Safety gates | `permission_gate` | **Already have it** | — |
| Absent = enabled | `tools.<id>` = on | **Do not unify** | Our switches fail in opposite directions on purpose |
| GSD projects (`CONTEXT.md`/`STATE.md`) | — | **Refuse** | Those describe a *repo*. Lumine's projects are the user's. Copying the layout imports a coding-agent concept into a companion |
| Research agent | `ResearchWorker` (Phase 9) | **Adapt, later** | The one place fresh-context genuinely transfers |

---

## Related

- [`../../README.md`](../../README.md) — current state, setup, capability table
- [`../../ROADMAP.md`](../../ROADMAP.md) — the companion track
- [`../decisions/0014-canonical-capability-namespace.md`](../decisions/0014-canonical-capability-namespace.md) — why dotted ids do not split the tools
- [`../../agent/AGENTS.md`](../../agent/AGENTS.md) — the runtime's invariants
