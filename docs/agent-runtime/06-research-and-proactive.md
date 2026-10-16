# Phases 9 and 10 — Research and Proactive

The two phases where this track stops being invisible.

---

# Phase 9 — `ResearchWorker`

- **Status:** specified, **not approved.**

## 1. The slice

> "Research the best local AI model for my PC, compare them, and tell me which
> one to get."

Multiple searches, several sources, a comparison, a recommendation — and a
result that does not sit in the conversation being re-billed on every later turn.

## 2. Why this is the one place fresh context genuinely transfers

The GSD insight — heavy work belongs in a fresh context — was **refused** for the
runtime as a whole, because a voice turn is short and there is no 200k window to
protect.

It applies here, for a specific and measurable reason:

- A research output is large.
- It is third-party text, so it is re-charged on **every later turn** in the
  conversation.
- It has no business in a voice conversation's history at all.

`whatsapp_read` already solved this exact shape: history results use a tighter
budget than `tool_results.MAX_RESULT_CHARS` *"because they are third-party text
paid for on every later turn."* Research needs the same treatment.

## 3. Do

- A worker receives only what its task needs: the question, the constraints, and
  the sources gathered so far. **Not** the conversation.
- Sources are preserved. Comparison and synthesis happen **against the fetched
  set**, never from memory.
- The worker's output is a digest written to a task artefact, not a conversation
  turn.
- Follow-up questions about the research read the artefact, so the cost is paid
  once.

## 4. Do not

- Add subagents as a **general** mechanism. One worker, one job.
- Spawn a worker for something one `search_web` call answers.
- Let a worker hold a permission request. It has no voice, so an approval it
  raised could not be answered — the same reasoning that makes the WhatsApp
  pre-warm fire-and-forget.
- Report a recommendation the sources do not support. `web_search` returns titles
  and links only; **work with what you are given and never describe data you were
  not given**.

## 5. Tests

- the worker receives no conversation history
- the artefact is written once and read many times
- a source list is preserved verbatim into the result
- a worker that fails reports a sentence, not a partial recommendation
- a permission-classed action inside research is refused or escalated, never
  silently attempted

---

# Phase 10 — Proactive Lumine

- **Status:** ⏸ **deferred.** Not scheduled.

## 1. Why deferred

Phase 10 needs three things that do not exist yet:

| Needs | Provided by |
|---|---|
| a lifecycle that is respectful to interrupt | [companion Phase 3](../../ROADMAP.md#phase-3--agent-lifecycle) |
| durable state | [Phase 3](03-persistent-task-store.md) |
| a reliable event surface | [Phase 5](04-recovery-and-events.md#phase-5--eventbus) |

A background task that cannot survive a restart, cannot be cancelled, and cannot
be observed is not a proactive companion. It is a stray process.

## 2. What it would cover

Background tasks · scheduled tasks · wake word · routines · proactive
suggestions · long-running workers.

## 3. Hard constraints, whenever it is scheduled

- **Restraint is measured, not intended.** The number of unprompted utterances per
  day is a reported number, and going up is a regression. This is
  [companion Phase 9](../../ROADMAP.md#phase-9--proactive-companion)'s criterion
  and it binds this phase too.
- **Never interrupt a task in progress or an active conversation.**
- **Wake lives outside `agent/`.**
  [ADR-0003](../decisions/0003-wake-layer-separation.md): nothing in `agent/` may
  import or configure a wake engine, and the microphone is not opened here. A
  wake-word service must not need to instantiate the UI.
- **No emotional pressure.** No streak mechanics, no guilt notifications, no
  engagement design. *"A companion that nags is worse than no companion."*

## 4. The two ways it can go wrong

| Failure | Why it is worse than doing nothing |
|---|---|
| Speaks when it has nothing worth saying | The user learns to ignore her, and then the things she does say stop landing |
| Interrupts something in progress | The one moment her output is guaranteed unwanted is mid-task |

Both are prevented by the companion track's lifecycle before this phase begins.
That ordering is the point.
