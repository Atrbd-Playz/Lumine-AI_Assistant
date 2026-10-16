# Lumine — Roadmap

Phased, and deliberately **not** a backlog. A phase describes a slice of the
product that must work end to end for a real person; it does not authorise
implementing its feature list. [`docs/decisions/0011-no-overbuild.md`](docs/decisions/0011-no-overbuild.md)
is the rule that makes this file safe to read.

Read [`VISION.md`](VISION.md) and [`ARCHITECTURE.md`](ARCHITECTURE.md) first.
Current state versus intent is measured in [`docs/gap-audit.md`](docs/gap-audit.md).

> **This file is the companion track.** It is about what a person *sees and hears*.
>
> The **runtime track** — the internal machinery that decides, plans, executes,
> observes, verifies, recovers and persists — has its own goals, phases and
> definition of done in [`docs/agent-runtime/`](docs/agent-runtime/README.md).
> The two are separate because they fail differently: companion Phase 6 (planning)
> depends on runtime Phases 1–4 being solid, and runtime Phase 10 (proactive) is
> blocked on companion Phase 3 (lifecycle).
>
> Phase 0 of both tracks is complete. The runtime track's Phase 1
> ([`CapabilityRegistry`](docs/agent-runtime/01-capability-registry.md)) is
> **approved and ready to build**; no runtime phase beyond it is authorised.

Each phase below states: **the slice**, **exit criteria** (observable, testable),
**deliberately not built**, and **consumes** (which decision records bind it).

---

## Phase 0 — Architecture & documentation

**Status: complete (2026-10-05).**

Vision, target architecture, lifecycle, IPC contract, wake and island design, gap
audit, thirteen decision records, and a written Phase 1 plan. No implementation
changes were made, and none should be made until Phase 1 is explicitly authorised.

**Produced**

| Document | What it holds |
| --- | --- |
| [`VISION.md`](VISION.md) | The North Star, success criteria, the surface-routing rule, anti-goals |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The nine layers, process model, state machine, IPC, wake, island, boundaries |
| [`ROADMAP.md`](ROADMAP.md) | This file |
| [`docs/decisions/`](docs/decisions/) | Thirteen ADRs with status, alternatives, and revisit conditions |
| [`docs/gap-audit.md`](docs/gap-audit.md) | What exists, what does not, conflicts, risks, open questions |
| [`docs/plans/wave-1-always-on-runtime.md`](docs/plans/wave-1-always-on-runtime.md) | The detailed Phase 1 plan — kept separate so the roadmap stays direction, not work |

**Exit criteria — met.** The repository states what Lumine is, how the runtime
should be shaped, what was chosen and refused, and what is next. Every claim about
current behaviour was verified against the source rather than assumed.

---

## Phase 1 — Always-on wake runtime (first vertical slice)

**The slice:** *Windows starts → nothing appears → the user says "Hey Lumine, what's
the weather?" → the island appears immediately → the rest of the utterance is
captured → she answers → the island retracts → the wake listener is still there.*

One slice, not a layer. Wake detection, the resident runtime, the island, the
lifecycle and one-shot capture all arrive together or not at all, because each of
them alone is invisible to the user.

**Exit criteria** — all observable, all tested:

1. The runtime starts with the OS and shows **no window, no notification, no
   dialog**.
2. An idle resident costs a measured, published budget for CPU and memory — and the
   number is in the docs, not in someone's head.
3. A wake phrase produces visible acknowledgement within a stated latency, measured
   on the target machine.
4. **"Hey Lumine, what's the weather?" is answered as one interaction.** The
   utterance is captured locally; no greeting fires.
5. Multiple invocation styles work, at a configurable threshold.
6. Self-audio protection is on by default and its false-trigger rate is measured,
   not assumed.
7. She does not wake on her own voice, on silence, or on background media.
8. The full UI never opened during any of the above.

**Deliberately not built:** barge-in, multi-turn follow-up (Phase 4), memory,
proactive anything, local LLM inference, the island's permission card *as a
first-class island citizen* (the card works in the dashboard in this phase), a
tray menu beyond the minimum needed to stop it.

**Consumes:** 0001, 0002, 0003, 0004, 0005, 0006, 0007, 0009, 0010, 0012, 0013.

**Plan:** [`docs/plans/wave-1-always-on-runtime.md`](docs/plans/wave-1-always-on-runtime.md).

---

## Phase 2 — Lumine Island

**The slice:** the island becomes a real surface with the full state vocabulary,
smooth animation in and out, listening / thinking / speaking / working / waiting
states, permission cards rendered in the island, and retraction on idle — with a
layout that behaves on a multi-monitor desktop and does not take focus.

**Exit criteria**

1. Every companion state has a distinct, intentional visual, driven by a total
   `Record` so a new state fails the build.
2. Appearance and retraction are animated, do not steal focus, and never appear in
   the taskbar or a task switcher.
3. A permission request is answered in the island, using the *same sentence the gate
   speaks* — no second wording.
4. Tool progress renders from the `lumine.tool` records that already exist.
5. Correct position and behaviour on a multi-monitor setup, including a primary
   monitor change.
6. The island's bundle does not import dashboard modules, and a test says so.

**Deliberately not built:** anything in the island that only makes sense while
sitting in a settings page. The island is a voice surface, not a dashboard.

---

## Phase 3 — Agent lifecycle

**The slice:** wake → LiveKit → agent; and back down again — start, stop, reconnect,
crash recovery, idle timeout, sleep command, and a graceful shutdown that actually
runs the worker's `close_session`.

**Exit criteria**

1. Wake brings up the agent; sleep takes it down; both are visible in one log.
2. **A crashed worker is detected and recovered** without user action, and the wake
   listener survives the crash.
3. **Stopping is graceful.** `session_ended` appears on a Tauri-initiated stop, not
   only on a room teardown.
4. `IDLE → SLEEPING` after the quiet period, with the timer **suspended** during
   tool execution, a pending approval, a running task and error recovery — each of
   those four tested.
5. "Sleep, Lumine" works from voice, from the island and from the full UI, and all
   three go through the same intent.
6. The full UI, if the user opened it, stays open across a sleep.

**Deliberately not built:** proactive wakeups, waking on a schedule, waking because
of a system event. Those need the lifecycle to be trustworthy first.

---

## Phase 4 — Natural voice interaction

**The slice:** the conversation that feels like a conversation. Follow-up sentences
with no wake phrase, barge-in, interruption, a smart idle timeout, and sleep
commands as ordinary speech.

**Exit criteria**

1. Three consecutive sentences after one wake, with no wake phrase between them.
2. **Speaking over her cuts her off**, from voice and from the island.
3. Interruption during a tool run is handled deliberately — not ignored, not
   treated as a cancellation.
4. The idle timer never fires mid-work.
5. "Go to sleep" / "Sleep, Lumine" understood in several natural phrasings.

**Deliberately not built:** multiple simultaneous conversation threads, hands-free
mode without any wake word, wake-word-free continuous listening. Continuous
listening without a wake word contradicts the privacy model.

---

## Phase 5 — Tools & permissions

**The slice:** the existing toolset becomes dependable *through the island*, and the
permission story is coherent on a surface the user may never open the full UI from.

**Exit criteria**

1. A permission request is visible and answerable from the island alone.
2. Every action class — `read`, `act`, `sensitive`, `destructive` — is reachable,
   tested and explained from that surface.
3. Cancelling a running multi-step task works from the island.
4. The island's tool progress is accurate for a long task, including failure.
5. Nothing is added to the full UI that is only needed because it is easier there.

**Deliberately not built:** new tools. This phase hardens what exists. New
capability waits for a phrase a real person would say.

---

## Phase 6 — Agent planning

**The slice:** multi-step work that a user trusts enough to walk away from — plan,
execute, verify, report, recover.

**Exit criteria**

1. "Find a relaxing playlist on YouTube and play it" completes as one request.
2. Each step's outcome is *verified*, using the existing `verification.py` ladder —
   `uncertain` stays a decision rather than an accident.
3. A mid-task failure is reported honestly and offers a next step.
4. Cancellation is immediate and leaves nothing half-done.
5. Progress is legible on the island throughout.

**Deliberately not built:** autonomous long-running agents, unattended execution,
anything that continues after the user has left. Proactive task execution belongs to
Phase 9 and needs the lifecycle to be trustworthy first.

---

## Phase 7 — Memory, context & personality

**The slice:** Lumine remembers, and is recognisably *her* on every surface.

**Exit criteria**

1. Durable memory across sessions, with the user able to see and delete it.
2. Memory retrieval is relevant, not a transcript dump — and bounded, because a
   tool result is re-billed on every later turn.
3. Persona consistency verified across voice, island, full UI, notices and
   proactive lines.
4. A spoken-from-code line sounds like her, because a line the *product* speaks is
   still Lumine speaking.
5. Memory never influences a permission decision.

**Deliberately not built:** inference about the user from unrelated data, memory of
conversations the user did not consent to, anything that reads other people's
messages beyond the existing per-session grant.

---

## Phase 8 — Local AI

**The slice:** Lumine runs on the user's own hardware.

**Exit criteria**

1. A local STT, LLM and TTS profile can be selected and works end to end.
2. The wake path, the lifecycle and the island are **unchanged** by the swap — which
   is the test that the attention layer really is decoupled.
3. An offline run works: no network, no provider, no crash.
4. The cost of local inference is measured and documented rather than assumed.

**Deliberately not built:** a claim that local is the default. `local` stays
reserved for weights that ship with the app; `ollama` stays the honest version of
the same idea as a server the user started.

---

## Phase 9 — Proactive companion

**The slice:** she chooses to speak when she has something worth saying, and stays
quiet otherwise.

**Exit criteria**

1. Reminders and scheduled actions work, and are cancellable by voice.
2. A task-completion notification arrives when it should, not earlier.
3. She never interrupts a task in progress or an active conversation.
4. **Restraint is measured** — the number of unprompted utterances per day is a
   reported number, and going up is a regression.
5. The philosophy is enforced by test, not by good intentions.

**Deliberately not built:** emotional pressure, streak mechanics, guilt
notification, anything designed to increase engagement. A companion that nags is
worse than no companion.

---

## Cross-phase invariants

These hold in every phase, and a change that breaks one is a change that needs a
decision record:

- The wake listener survives the agent, the network and the provider.
- Nothing in the webview ever holds a secret.
- The worker stays a separate process from the UI.
- A capability that grants power fails **closed** on an unrecognised value; a
  switch that only reduces power reads it as `on`. They stay deliberately
  opposite.
- A broken mechanism degrades to a **refusal**, never to a plausible wrong action.
- A telemetry write never raises.
- The full UI is never a prerequisite.
- Every state a surface renders comes from the lifecycle machine, never from that
  surface's own inference.

## Sequencing rationale

The order is not "easy first". It is **cheapest to be wrong first**:

1. Wake and the island before anything else, because they are the product's face
   and the hardest to retrofit.
2. Lifecycle before memory, because memory without lifecycle makes answers depend on
   restart count.
3. Tools before planning, because planning multiplies whatever the tools get wrong.
4. Local AI before proactive behaviour, because a companion who depends on a network
   should not be the one deciding to interrupt you.
5. Proactive last, always, because it is the only phase that can make the product
   actively unwelcome.