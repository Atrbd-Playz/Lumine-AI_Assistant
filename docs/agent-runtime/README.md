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

**Status: Phase 0 complete. Phase 1 approved. Phases 2–10 specified, not approved.**

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

- [ ] There is one owner of the agent lifecycle (`AgentRuntime`)
- [ ] Capabilities have canonical dotted identities with **one** authority
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
| 2 | [`AgentRuntime` façade](02-agent-runtime.md) | specified | no |
| 3 | [Persistent task store](03-persistent-task-store.md) | specified | **yes** — survives restart |
| 4 | [`RecoveryEngine`](04-recovery-and-events.md#phase-4--recoveryengine) | specified | partly |
| 5 | [`EventBus`](04-recovery-and-events.md#phase-5--eventbus) | specified | no |
| 6 | [Read-only filesystem](05-capabilities-and-context.md#phase-6--read-only-filesystem) | specified | **yes** |
| 7 | [Selective context retrieval](05-capabilities-and-context.md#phase-7--selective-context-retrieval) | specified | partly |
| 8 | [LLM planner fallback](05-capabilities-and-context.md#phase-8--llm-planner-fallback) | specified | partly |
| 9 | [Research worker](06-research-and-proactive.md#phase-9--researchworker) | specified | **yes** |
| 10 | [Proactive Lumine](06-research-and-proactive.md#phase-10--proactive-lumine-deferred) | deferred | **yes** |

**Phases 1, 2 and 5 add no user-visible capability.** That ordering is the point:
the foundation is made explicit before anything is built on top of it. A registry
nothing reads is not progress.

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
| …and a browser failure re-observes and recovers instead of lying | Phase 4 |
| …and a long research runs without filling the conversation | Phase 9 |
| "Read the notes file I keep in Documents." | Phase 6 |
| "Continue where we left off." — task and preference context, selectively | Phase 7 |
| "Find something on my PC I half-remember." — a request the scenario table cannot express | Phase 8 |
| "Research this and tell me when you're done." | Phase 9 |
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
