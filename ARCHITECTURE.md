# Lumine — Architecture

Target architecture for the companion runtime, and an honest statement of which
parts exist today. Recorded 2026-10-05.

Read [`VISION.md`](VISION.md) first; this document assumes it. The phase-by-phase
build order is [`ROADMAP.md`](ROADMAP.md). Every choice marked **Decision** has a
record in [`docs/decisions/`](docs/decisions/), and the full gap list — including
risks and open questions — is [`docs/gap-audit.md`](docs/gap-audit.md).

Status tags used throughout: **[built]** exists and works · **[partial]** exists in
a form that will have to change · **[absent]** does not exist.

---

## 1. The layer map

The product is not one application. It is a set of responsibilities with different
lifetimes, different failure costs, and different privacy boundaries. Collapsing
them is the architectural mistake this project has to avoid.

```text
┌──────────────────────────────────────────────────────────────────────┐
│ 0. RESIDENT RUNTIME            always on, never visible              │
│    supervises everything below; outlives every conversation           │
├──────────────────────────────────────────────────────────────────────┤
│ 1. WAKE / ATTENTION            local mic · keyword detection         │
│    owns: the continuous audio path and nothing else                   │
├──────────────────────────────────────────────────────────────────────┤
│ 2. LIFECYCLE                   one state machine; the only authority  │
│    owns: SLEEPING→…→ERROR, idle timeout, sleep command               │
├──────────────────────────────────────────────────────────────────────┤
│ 3. AGENT MANAGER               worker process lifecycle + readiness  │
│    owns: start/stop/restart/recover the Python worker                 │
├──────────────────────────────────────────────────────────────────────┤
│ 4. VOICE AGENT (Python)        one room per conversation              │
│    owns: STT/LLM/TTS, tools, memory, the persona                     │
├──────────────────────────────────────────────────────────────────────┤
│ 5. TOOL / ACTION SYSTEM        bounded capability                    │
│    owns: browser, apps, media, messaging, desktop                     │
├──────────────────────────────────────────────────────────────────────┤
│ 6. PERMISSION SYSTEM           the only door into layer 5            │
│    owns: action classes, approval, expiry, the audit trail           │
├──────────────────────────────────────────────────────────────────────┤
│ 7. TASK / PLANNER              multi-step work, cancellation         │
│    owns: route → plan → execute → verify → report                     │
├──────────────────────────────────────────────────────────────────────┤
│ 8. MEMORY / CONTEXT            conversation + durable context        │
│    owns: chat context budget, and later: persistent memory           │
├──────────────────────────────────────────────────────────────────────┤
│ SURFACES                                                                  │
│ 9a. ISLAND UI                 transient, top-centre, appears on wake  │
│ 9b. FULL UI                   her home; opened only on request        │
│ 9c. TRAY                      manual wake / open / quit              │
└──────────────────────────────────────────────────────────────────────┘
```

Layers 5–7 are **[built]** and well-tested. Layers 8's conversation budget is
**[built]**; durable memory is **[absent]**. Layers 0–3 and 9a–9c are
**[absent]** or **[partial]**, and they are what Phases 1–3 are about.

### The dependency rule

Dependencies point **downward only**. Specifically:

- The wake layer knows nothing about the agent, the tools, or the provider. It
  emits one event: *wake, with this utterance*.
- The island knows about the lifecycle and the permission system. It does **not**
  know about LiveKit, Groq, Cartesia, or Gemini, and it never will.
- The full UI may know about the lifecycle. It is not a prerequisite for it.
- Nothing in layers 5–7 may call a surface. A tool announces through the layer it
  is given (currently the data channel and `LUMINE_EVENT`), never by reaching for
  a window.

**Decision:** the wake/attention layer is a separate component from the agent, and
the island is a separate surface from the full UI —
[ADR-0003](docs/decisions/0003-wake-layer-separation.md),
[ADR-0006](docs/decisions/0006-island-as-primary-surface.md).

---

## 2. Process model

### Today

Three tiers, two of which are OS processes:

```text
lumine.exe  (Rust: window, keyring, settings, worker supervision)
   └── WebView2 (React 19 — the only window that exists, declared in
                  tauri.conf.json, shown at launch)
             │  invoke() / listen()  — 33 commands, 5 events
             ▼
python.exe agent/agent.py dev      (persistent LiveKit AgentServer "lumine")
             │  LUMINE_EVENT <json> on stdout  — 24 record types
             ▼
LiveKit cloud → STT / LLM / TTS providers
```

**[built]** The worker's lifecycle is owned by Rust (`agent_manager.rs`): idempotent
`start`, `stop`, `restart`, and a readiness gate that blocks until
`worker_registered`. Credentials are injected one variable per keyring slot, and no
secret ever reaches the webview.

**The load-bearing property today:** the app is correct only while a window is
open. `run()`'s `ExitRequested` handler stops the worker, and the worker is only
ever started by a button press. "The app is running" and "Lumine is available" are
the same statement today, and the vision requires them to be different.

### Target

```text
lumine-runtime.exe   resident, hidden, supervises everything
   ├── wake service            local audio in, keyword detection out
   ├── lifecycle state machine
   ├── agent manager           start/stop/recover python.exe
   └── island window           shown on demand, hidden otherwise

lumine.exe (full UI)  opened only when asked — a window, not a prerequisite

python.exe agent/agent.py      unchanged: persistent worker, one room per call
```

**Decision:** the resident runtime is one process, with the island as its own
webview entry point and *not* the dashboard's React tree —
[ADR-0002](docs/decisions/0002-single-resident-runtime.md). The full UI stays a
separate process-quality surface, launched on request.

The unresolved half — whether the resident runtime should be the existing Tauri
binary or a new Rust binary that outlives it — is the first thing Phase 1 must
decide, and it is recorded as an open question in
[`docs/gap-audit.md`](docs/gap-audit.md) → *Open questions*.

---

## 3. Lifecycle

One explicit state machine is the authority. Not `is_listening` and `is_speaking`
and `is_awake` as independent booleans scattered across two languages.

### The states

| State | Meaning | LiveKit / worker | Island | Full UI |
| --- | --- | --- | --- | --- |
| `SLEEPING` | nobody addressed her; only the wake listener is live | worker stopped or unregistered | hidden | closed unless the user opened it |
| `WAKING` | wake detected; the utterance is being preserved and handed on | worker starting / room connecting | appearing | unaffected |
| `LISTENING` | capturing a request | room live, mic publishing | showing | open if the user opened it |
| `THINKING` | model is working | turn in flight | showing | — |
| `EXECUTING` | a tool or task is running | tool steps publishing | showing progress | — |
| `SPEAKING` | she is talking | TTS active | showing | — |
| `IDLE` | turn finished; conversation still alive | room still connected | still visible | — |
| `ERROR` | something failed and the user should know | circuit may be open | showing the failure | toast |

Transitions, and who owns each:

```text
SLEEPING ──wake detected─────────────▶ WAKING        [wake service]
WAKING ────utterance handed on───────▶ LISTENING     [lifecycle]
LISTENING ──speech detected──────────▶ THINKING      [lifecycle]
THINKING ──first tool step───────────▶ EXECUTING     [lifecycle]
EXECUTING ──all steps verified───────▶ SPEAKING      [lifecycle]
THINKING/EXECUTING ──reply ready─────▶ SPEAKING      [lifecycle]
SPEAKING ──speech finished───────────▶ IDLE          [lifecycle]
any ───────"sleep" / user stop──────▶ SLEEPING      [lifecycle]
any ───────failure───────────────────▶ ERROR         [lifecycle]
ERROR ──────recovered or reset───────▶ SLEEPING      [lifecycle]
IDLE ───────idle timeout reached─────▶ SLEEPING      [lifecycle, timer suspended
                                                       by THINKING/EXECUTING/
                                                       SPEAKING/waiting-for-
                                                       approval/waiting-for-
                                                       input)
```

Two rules make this state machine honest:

1. **Only the lifecycle sets it.** A surface reflects the state; it never infers
   one. The webview's own `setState` calls during room setup are exactly the
   inference this removes.
2. **Transitions are events.** Every transition is published once, in one shape,
   to every surface, so the island and the full UI cannot disagree about what
   Lumine is doing.

**Decision:** one lifecycle authority, published as a single typed event —
[ADR-0005](docs/decisions/0005-single-lifecycle-authority.md).

### What exists today, and why it is not enough

There are **three** state vocabularies and no machine:

| Where | Type | Values | Problem |
| --- | --- | --- | --- |
| `agent_manager.rs` | `AgentState` | `Booting, Ready, Sleeping, Listening, Thinking, Speaking, Error, ShuttingDown, Stopped` | Describes the **process**, not the companion. `Listening`/`Thinking`/`Speaking`/`Stopped` are never constructed. |
| `agent.py` | LiveKit's `AgentState` | forwarded as a latency mark only | Never published; `user_state` is published instead. |
| `types.ts` | `LumineState` | `idle, connecting, online, listening, thinking, speaking, error` | Set *by the webview* from room events. `"thinking"` is never assigned. |

The name collision is the dangerous part: **`AgentState::Sleeping` currently means
"there is no worker process."** In the target it means "the user did not ask for
anything and the wake listener is all that is running." Those are opposite
conditions. The process state and the companion state must be separate types with
separate names — [ADR-0012](docs/decisions/0012-process-vs-companion-state.md).

---

## 4. The IPC contract

### What stays

`LUMINE_EVENT <json>` on the worker's stdout, re-emitted verbatim by Rust as the
Tauri event `agent_runtime`, is a good seam and stays. It is single-key, cannot be
spoofed by a payload (`emit` drops any `type` the caller supplies), never raises
on a dead pipe, and it works for a worker started outside Tauri. The data channel
carries the conversational topics: `lumine.permission`, `lumine.tool`,
`lumine.notice`, `lumine.emotion`, `lk.chat`.

**[built]** 24 record types today, including `worker_registered`, `session_started`,
`user_state`, `tool_status`, `permission_request`, `permission_announced`,
`latency`, `error`, `retry_after`, `session_ending`, `session_ended`.

### What is added

New records, all of them additive — an older UI must not break on a newer worker:

| Record | Why |
| --- | --- |
| `lifecycle_state` | the one authoritative companion state, with the previous state and the cause |
| `wake` | detected phrase, confidence, whether a usable utterance was captured, and its duration — the wake layer's only vocabulary |
| `utterance` | the preserved text (and later audio) of the captured request, handed to the session as the first turn |
| `idle_transition` | why the idle timer fired or suspended, so a surprising sleep is explainable |
| `suppression` | self-audio protection engaged/disengaged, with the reason |

### What is forbidden

- No LiveKit, provider, or model detail may cross into a surface. Surfaces get
  lifecycle, tool status, permission requests, notices, and emotion — never
  `gemini-3.8-live` or a token.
- A surface may not command the worker into a state directly. It raises an
  *intent* (`request_sleep`, `request_wake`); the lifecycle decides.
- A telemetry write may never raise. `runtime_events.write_event_line` already
  guarantees this and must keep doing so — a dead pipe is how every session died
  once.

---

## 5. The wake / attention layer

**Status: [absent].** Nothing in the repository captures a microphone outside a
live LiveKit session.

### What it owns

A continuous, local audio path and exactly one output: *wake, plus the utterance*.
Concretely it must provide:

- **A local capture path.** The worker has none; today the microphone is a LiveKit
  publication created by the webview (`setMicrophoneEnabled`). Continuous audio
  cannot go through a room that only exists during a conversation.
- **A local ring buffer.** Enough pre-roll to cover the wake phrase itself and a
  short continuation window, so nothing said immediately after the name is lost.
- **A keyword/phonetic detector.** Not a transcript comparison.
- **A confidence threshold and a post-speech cooldown.**
- **Suppression while Lumine speaks**, so she cannot trigger herself.

### What it must never do

- Send continuous audio to any provider.
- Import the agent, the tools, or the settings that describe a model.
- Fail when the worker, the network, or the provider is down. Wake is the one
  subsystem with no upstream dependency.

**Decision:** wake is local, continuous, and separated —
[ADR-0003](docs/decisions/0003-wake-layer-separation.md),
[ADR-0004](docs/decisions/0004-wake-not-a-transcript.md),
[ADR-0009](docs/decisions/0009-self-audio-protection.md).

---

## 6. One-shot commands

**This is a core acceptance criterion, not a future enhancement.**

```text
user: "Hey Lumine, what's the weather?"
   │
   ├─ wake service is already capturing ── ring buffer holds the whole sentence
   ├─ keyword fires on "Lumine"
   ├─ utterance = buffer[pre-roll → endpoint]        ← nothing is lost here
   ├─ island appears (immediate feedback)
   ├─ lifecycle: WAKING
   ├─ agent manager: ensure the worker is up          ← may take seconds; the
   │                                                      utterance is already safe
   ├─ session opens with `utterance` as its first turn
   ├─ THINKING → (tool) → SPEAKING → IDLE
   └─ no greeting: there was already a request
```

The load-bearing consequence: **the utterance must be captured locally and handed
to the session, because the room takes real time to connect and the user is not
going to repeat themselves.** A design where the agent transcribes the live room
will drop the part of the sentence that matters.

The two acceptable ways to produce the utterance text, in order of preference:

1. **Local STT on the captured buffer.** No network, no privacy cost, no provider
   latency. This is the target.
2. **One cloud STT call on the captured utterance only** — after wake, never
   before. Materially different from streaming silence to a provider, and
   permitted by the privacy rule, but it is a billable call and a latency cost,
   so it is a fallback rather than the design.

Holding the audio buffer and replaying it into the room is a third option and is
more faithful, but it complicates turn detection for no user-visible gain over
option 1. Recorded, not chosen —
[ADR-0007](docs/decisions/0007-one-shot-commands.md).

---

## 7. The island

**Status: [absent].** No second window exists; the frontend imports no Tauri window
API at all, and `tauri.conf.json` declares a single ordinary window.

A transient, always-on-top, non-focus-stealing, click-through-when-idle surface at
the top centre of the *primary* monitor. It is an interaction surface, not an
application, and the differences are the design:

| Property | Why |
| --- | --- |
| Appears on wake, retracts on idle-timeout | It exists because she is being addressed |
| Never in the taskbar, never a task-switcher entry | It is not an application |
| Does not take focus when appearing | Taking focus from a user mid-sentence is the bug |
| Renders the same state machine the full UI does | Two truths about her state would be worse than one |
| Carries the permission card | See below |
| Has its own entry point | It must not be able to import the dashboard |

### What the island renders

1. **Acknowledgement** — within a few frames of the wake. Before anything else.
2. **State** — listening / thinking / speaking / working, from the lifecycle.
3. **Progress** — tool steps. The data already exists: `progress.py` publishes
   `tool_status` records on `lumine.tool` with an `index` and a `total`.
4. **The transcript of the current exchange**, briefly.
5. **Permission requests** — the same `question` string the gate speaks, never a
   paraphrase. Two wordings would let a user approve one action and believe they
   approved another.
6. **Two actions**: answer a permission, or open the full UI.

**[built]** and reusable for free: the gate already speaks the question, publishes
the card on `lumine.permission`, and carries `expires_at`. The island is mostly a
presentation change. **[built]** and reusable: `voice_line.speak_line` is the
mechanism for acknowledging the wake without involving the model.

### Self-audio

The island must be able to mute or duck whatever the computer is playing, so she
does not talk over the user's video. A `useVoiceDucking.ts` hook once did this for
the Focus workspace's in-app music player, ducking local audio whenever a voice
session was live; both it and the workspace are removed, so the island must not
assume that hook still exists — the behaviour has to be rebuilt with the island.

**Decision:** the island is the primary voice surface —
[ADR-0006](docs/decisions/0006-island-as-primary-surface.md).

---

## 8. Idle timeout and sleep

**Status: [absent].** No idle timeout, no inactivity timer, and no in-worker
shutdown exists. A session today ends only when the last participant leaves or
LiveKit tears the room down, which means a user who walks away holds a room, an
HTTP client, a permission gate and a browser alive indefinitely.

The target:

- `IDLE → SLEEPING` after a configurable quiet period (default: short — this is a
  companion, not a conference call).
- The timer **suspends** while `THINKING`, `EXECUTING`, `SPEAKING`, while an
  approval is pending, and while a task is running. A timeout that fires during
  work is a bug.
- "Sleep, Lumine" forces `→ SLEEPING` immediately.
- Sleeping disconnects or stops the agent. It does **not** close the full UI if the
  user opened it, and it never stops the wake listener.
- Rust's current `child.kill()` is not a graceful stop: it never runs the worker's
  `close_session`, which is why `session_ended` does not appear on a Tauri stop.
  Phase 3 fixes this; until then, "the agent stopped" and "the agent ended
  cleanly" must not be conflated.

**Decision:** idle and sleep are separate, and the timeout is smart —
[ADR-0008](docs/decisions/0008-idle-is-not-sleeping.md).

---

## 9. Tools, permissions, tasks

**[built]** and unchanged by this roadmap. Recorded here so the new layers treat
them correctly.

- Every side-effecting call passes `runtime/permission_gate.py`. Classes are `read`,
  `act`, `sensitive`, `destructive`; the saved config can only raise a class, never
  lower it.
- Approvals are session-scoped, expire (default 60s), are answered by voice or by
  the card, and land in a SHA-256-digested JSONL audit.
- A run with no gate fails closed for `sensitive`/`destructive`.
- `runtime/task_engine.py` gives dependent steps, cancellation, checkpoints and
  bounded concurrency; `tools/task_runner.py` exposes them as `run_task` /
  `cancel_task`.
- `runtime/verification.py` is the shared honesty vocabulary: `requested`,
  `dispatched`, `accepted`, `observed`, `failed`, `timed_out`, `uncertain`.

What changes: the island renders permission requests, the lifecycle reports
`EXECUTING`, and the wake layer can interrupt an `EXECUTING` state only by being
refused — a task in flight is not something a wake word may abandon.

---

## 10. Memory and context

**[partial]** `runtime/context_trim.py` keeps the conversation inside an item
budget (default 40, floor 24) and preserves the system message. `runtime/latency.py`
and the persisted Activity feed are the only history that survives a session.

**[absent]** durable memory across sessions. Phase 7, deliberately: memory without
a lifecycle makes a companion's answers depend on how many times the process
restarted, which is worse than no memory at all.

---

## 11. Boundaries that must not move

These already hold and are load-bearing. New subsystems do not get to relax them.

- **No secret in the webview.** `get_livekit_token` mints in Rust; the keyring is
  write-only toward the frontend; there is deliberately no command that returns a
  plaintext secret. The island gets credentials no differently than the dashboard.
- **The worker stays a separate process.** Moving the agent into the frontend for
  convenience would make the runtime's cheapest path its heaviest, and would put
  the model in the renderer.
- **`local` stays reserved** for inference whose weights ship with the app;
  `ollama` is the honest version of the same idea as a server the user started.
- **Fail closed on authority, fail open on convenience.** A capability key that
  *grants* power (`desktop.input`, `media.enabled`) reads an unrecognised value as
  `off` and reports an error; a key that only *reduces* power (`tools.<id>`) reads
  it as `on`. Do not make these consistent — they are deliberately opposite.
- **A broken selector must degrade to a refusal**, never to a plausible wrong
  action. This applies to wake detection, island rendering and tools alike.

---

## 12. What this architecture deliberately does not have

- **No video output.** The SVG avatar is the answer. `AvatarSession` is a future
  decision, not a gap to be closed by reflex.
- **No bundled media player.** Existing players, an `.m3u8` and a truthful "I
  cannot pause it" already cover the library.
- **No cloud backend of Lumine's own.** LiveKit carries the audio and the config
  document lives on disk; there is no Lumine server.
- **No plugin/MCP surface in the runtime.** Dev tooling exists in `opencode.json`
  for this repository; the shipped product ships no MCP servers.
- **No multi-agent orchestration.** One companion, one room at a time.

---

## 13. Test and verification contract for the new layers

The existing suites keep their shape: Python via `unittest`, frontend contract
checks via `npm run check`, Rust via `cargo check`. New layers add:

- Wake: a **CLI probe** beside the existing probes (`agent/window_probe.py`,
  `agent/desktop_probe.py`) that prints what the detector heard, the score, and
  whether suppression was engaged — read-only, no agent involved.
- Lifecycle: a pure transition table with a table-driven test, so an illegal
  transition fails a test rather than a demo.
- Island: a state-to-visual mapping that is a total `Record`, like the existing
  `Record<LumineState, …>` tables — adding a state then fails the build.
- End-to-end: an extended `desktop_smoke.py`-style staged check, where the
  `refuse` stage still runs **before** anything that touches the machine.

The rule the existing suite already encodes and this layer inherits: **assert
against the real boundary, not a fake that cannot fail.** The vacuous generation
check, the fake accessibility tree with no child windows, and the balanced-brace
script check were all found by testing the real thing.