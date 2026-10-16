# Gap audit — vision versus current architecture

Recorded 2026-10-05. Phase 0 deliverable.

**Method.** Every claim about current behaviour below was read out of the source or
checked by grep against this repository, not inferred from prose. Where an existing
document disagrees with the code, the code is cited and the document is the thing
that is wrong. Nothing was modified as part of this audit.

---

## 1. What Lumine is today, precisely

Three tiers, verified:

| Tier | Fact |
| --- | --- |
| **Tauri shell** (Rust) | 15 modules. 33 `#[tauri::command]`s. 5 Tauri events (`agent_started`, `agent_state_changed`, `agent_stopped`, `agent_error`, `agent_runtime`). Two plugins: `opener`, `dialog`. |
| **Webview** (React 19) | Exactly **one** window, declared statically in `tauri.conf.json` with five keys: `title`, `width`, `height`, `minWidth`, `minHeight`. No `transparent`, `alwaysOnTop`, `decorations`, `skipTaskbar` or `visible`. **Zero** imports of `@tauri-apps/api/window` — the frontend never manipulates a window at all. No router; `Home.tsx` is a ternary chain. |
| **Worker** (Python) | `agent/agent.py`, 1030 lines. A persistent `AgentServer` named `lumine`; one `AgentSession` per dispatched room. `AgentServer.from_server_options(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="lumine"))` — no host, no port, no local mode. |

Verified facts that matter for the roadmap:

- **Microphone capture lives in the webview, not the worker.** The only audio input is
  `room.localParticipant.setMicrophoneEnabled(true)`
  (`features/voice/voice-manager.ts:1239`). The worker opens no audio device
  anywhere — no `pyaudio`, no `sounddevice`, no capture code at all.
- **No autostart mechanism exists.** Searched the whole repository for `autostart`,
  `auto_launch`, `login_item`, `run_at_startup`, `schtasks`, `CurrentVersion\Run`,
  `winreg`. Every hit *was* the Focus timer's `autoStart` field — that workspace has
  since been removed, and there is now no hit at all. `winreg` is in
  `Cargo.lock` only as a transitive build-time dependency of `tauri-winres`.
- **No tray icon.** Zero occurrences of `tray` in `src-tauri/`, `tauri.conf.json` or
  the manifests; every hit is prose in `AGENTS.md` listing it as a gap.
- **No wake word, no hotword, no frontend VAD, no always-on audio path.** Every hit
  for "wake" is either the word "wake-ups" in a comment about a polling interval,
  the `atomic-waker` transitive JS dependency, or `AGENTS.md` listing it as a gap.
- **No idle timeout and no in-worker shutdown.** `idle_timeout`, `IDLE_TIMEOUT`,
  `min_idle`, `inactivity_timeout` return nothing in `agent/`, `lumine-ui/src/` or
  `lumine-ui/src-tauri/src/`. A session ends only when the last participant
  disconnects (`agent.py:929-941`) or LiveKit tears the room down.
- **Stopping the worker is ungraceful.** `agent_manager.rs:303-304` is
  `child.kill(); child.wait()`. The worker's `close_session` shutdown callback
  (`agent.py:900-927`) never runs on a Tauri-initiated stop, so `session_ended` does
  not appear.
- **There is no lifecycle state machine in Python.** `agent_state_changed` is
  subscribed at `agent.py:800` and recorded only as a latency mark; only
  `user_state` is published. Three state vocabularies exist (see
  `ARCHITECTURE.md` §3) and none is authoritative.
- **`AgentState::Sleeping` means "there is no worker process"** and is set at four
  places in `agent_manager.rs`. Four of the nine variants are never constructed.
- **The greeting is unconditional** (`agent.py:974-981`).
- **`bundle.active` is `false`** — `npm run tauri build` writes `lumine.exe` and
  stops. A working install needs Python and Playwright's Chromium on the machine.
- **[built, and reusable]** 24 `LUMINE_EVENT` record types; the permission card
  payload; `voice_line.speak_line` with `SAID`/`GENERATED`/`SILENT`; progress steps
  with `index`/`total` on `lumine.tool`; the avatar engine's
  per-state emotion and activity tables; the credential-free IPC boundary.
  (`useVoiceDucking.ts` was listed here; it and the Focus workspace it served have
  been removed, so island self-audio must be rebuilt — see `ARCHITECTURE.md`
  §Self-audio.)

---

## 2. Vision section by section

| # | Requirement | Status | Note |
| --- | --- | --- | --- |
| 1 | Companion, not a generic chatbot | **Partial** | Voice, persona, tools, avatar are a companion. But the product is still only reachable through a window. |
| 2 | Always available, not always visible | **Absent** | No autostart, no tray, no resident process. `ExitRequested` stops the worker. |
| 3 | Distinct runtime layers | **Partial** | Tools, permissions, tasks, memory-budget and persona are genuinely separated and well-factored. Layers 0–3 and the island do not exist. |
| 4 | Always-on wake system | **Absent** | No local capture, no detector, no ring buffer. The mic only exists inside a LiveKit session. |
| 5 | Wake word independent of STT | **Absent** | Same. Note the product's prior art on this exact failure: the WhatsApp search pre-filter was a verbatim substring test and a one-letter mis-hearing made a visible contact report as absent. |
| 6 | One-shot commands | **Absent / conflicting** | The two-step shape is forced by the architecture: nothing captures audio before a room exists. The unconditional greeting would also consume the first turn. |
| 7 | The island | **Absent** | No second window, no transparency, no always-on-top, no window API in the frontend. |
| 8 | Full UI optional | **Absent** | The UI is the only entry point, and it opens at launch. |
| 9 | Explicit lifecycle states | **Partial / conflicting** | A process enum exists and is half-dead; two more vocabularies exist elsewhere; three `AgentState` variants collide in name with companion states and one of them means the opposite. |
| 10 | Idle ≠ sleeping | **Absent** | No idle timeout at all. |
| 11 | Barge-in / interruption | **Partial [built]** | The plumbing is genuinely there: `barge_in` is the default interruption mode, `silence_duration_ms` is 700 on the realtime path, `ActivityHandling.START_OF_ACTIVITY_INTERRUPTS`, `false_interruption` records, `voice_line(allow_interruptions=…)`. Untested end to end from the island, and blocked on Phase 1's absence. |
| 12 | Sleep command | **Absent** | No typed sleep/wake protocol; LiveKit dispatch metadata plus the event stream are the only seam. |
| 13 | Self-audio / false-wake protection | **Absent** | Turn-level policy exists (`allow_interruptions`, `false_interruption`); detector-level suppression does not. |
| 14 | Voice agent stays a separate process | **[built]** | Held. The worker is a separate process and the IPC keeps secrets out of the webview. |
| 15 | Local/cloud flexibility | **Partial** | The attention layer does not exist yet, so nothing is coupled — which is convenient rather than designed. `ollama` exists as an address; no local STT/LLM/TTS. |
| 16 | A real computer agent | **[built]** | Seventeen tools, action classes, verification ladder, task engine. The strongest part of the product. |
| 17 | Multi-step execution | **[built]** | `router` → `task_model` → `task_engine` → `task_runner`, with dependencies, retries, checkpoints, cancellation. |
| 18 | Permissions as part of the experience | **[built], wrong surface** | Voice-first, audited, same sentence spoken and carded, expiry, session binding. It just renders in the full UI, which is the thing the vision says must not be required. |
| 19 | Memory and personality | **Partial** | Persona is `[built]` and enforced: core + on-demand full text, `test_persona_split.py`. Durable memory is `[absent]`; only a conversation item budget exists. |
| 20 | Avatar / visual identity | **[built]** | SVG engine, 29 emotions, per-state tables, montage, energy input. Needs island-specific states, not a new engine. |
| 21 | Full UI's role | **Partial** | It is a control centre. It is also the only way in, which the vision forbids. |
| 22 | Media / music | **[built]** | `play_media`, seven verbs, allowlisted folder, `.m3u8` queues. Already closer to the long-term concept than most of the roadmap. |
| 23 | Proactive future | **Absent, correctly deferred** | Nothing. Right for now. |
| 24 | Offline / local future | **Partial** | Silero VAD is already local, but only on the cascade path; the default realtime path runs server-side activity detection. No local wake. Nothing decided so far prevents local operation. |
| 25 | Performance philosophy | **Absent where it matters** | Latency is measured for LLM turns (`runtime/latency.py`) and there are `step_*` measurements, but there is no measurement for wake → visible, because there is no wake. |
| 26 | Privacy philosophy | **[built] in spirit** | Credentials are keyring-only and never cross into the webview; the music folder and attachment paths are resolved-then-compared. The always-on microphone does not exist yet, which is why the local-only rule must be a constraint on Phase 1 rather than a retrospective fix. |
| 27 | Development rules | **New** | Adopted: the questions above are now part of the pre-flight for any feature. |
| 28 | Architectural decision rule | **New** | Adopted as the twelve-item preference list in `VISION.md`. |
| 29 | Roadmap structure | **[built]** | `ROADMAP.md`, Phase 0 complete. |
| 30 | Coding agent behaviour | **New** | Adopted into `AGENTS.md` as mandatory pre-flight. |
| 31 | Do not overbuild | **[built]** | ADR-0011. The caution it names has since been acted on: the Focus workspace — the largest thing in the tree and the part marked "a work in progress" — has been removed entirely. |
| 32 | Success criteria | **Absent end to end** | Every step of the clean-install script depends on Phase 1. Steps 1–2 are the only ones that cannot be reached today. |
| 33 | The North Star | **[built]** | `VISION.md`. |

---

## 3. Conflicts — code that contradicts the vision

These are the important ones. Each is a real property of the current code, not a
missing feature.

**C1. The app's lifetime is the runtime's lifetime.** `run()`'s `ExitRequested`
handler stops the worker, and nothing but a button press starts it. The vision
requires the opposite, and this is the single change everything else depends on.
*Blocking: Phase 1.*

**C2. The microphone only exists inside a conversation.** `setMicrophoneEnabled` on a
LiveKit room means there is no way to hear a wake word while asleep. Any wake design
that reuses the room is paying for a permanently open session — continuous provider
audio, a session that never ends, and a privacy failure.
*Blocking: Phase 1. Recorded in ADR-0003.*

**C3. The greeting is unconditional.** `generate_reply` on every session start means a
wake with a request produces a greeting first — the two-step shape arriving through a
different door. *Blocking: one-shot. ADR-0013, a one-line branch.*

**C4. `AgentState::Sleeping` means the opposite of the vision's.** It is set when there
is no worker process. In the target it is the healthy state in which a companion
spends most of the day. Any log line or UI string reading "sleeping" is therefore
ambiguous between "she is resting" and "something died". *ADR-0012.*

**C5. Three state vocabularies, no authority.** `AgentState` (process), LiveKit's
`agent_state_changed` (a latency mark, never published), and `LumineState` (set by
the webview, with `"thinking"` never assigned). Two surfaces will eventually disagree
about what she is doing. *ADR-0005.*

**C6. No idle timeout means an unbounded session.** A user who walks away holds a
room, an HTTP client, a permission gate and possibly a browser. It is also the reason
the vision's cost story cannot currently be told. *Blocking: Phase 3.*

**C7. Stopping is ungraceful.** `child.kill()` skips `close_session`, so
`session_ended` never appears on a Tauri stop. Anything built on "did the agent stop
cleanly" is currently guessing. *Phase 3.*

**C8. Permissions render in the wrong place.** The gate itself is
`[built]` and voice-first; the full UI is the only surface that can show it, and the
full UI is the prerequisite the vision forbids. *Phase 2.*

**C9. The window model cannot express the island.** One statically declared window,
no transparency, no always-on-top, no skip-taskbar, no `visible: false`, and zero
window-API usage in the frontend. *Blocking: Phase 1/2. Also note `capabilities/default.json`
grants only `core:default` and `opener:default`, so a new window needs its own
capability entry — the dialog plugin is deliberately Rust-only for exactly this
reason.*

**C10. Packaging blocks "starts with Windows".** `bundle.active: false`, and a working
install needs Python plus Playwright's Chromium on the machine. An autostart entry
can point at a dev checkout, so this blocks *distribution*, not the Phase 1
architecture — but it blocks the clean-install success criterion in `VISION.md`.

---

## 4. Risks

| # | Risk | Why it matters | Mitigation in the plan |
| --- | --- | --- | --- |
| R1 | **The resident runtime's idle cost is unknown.** | The whole product rests on "cheap enough to always run". A resident Tauri app holding a hidden dashboard webview can plausibly cost more than everything else combined. | Measure idle CPU and RSS before committing to the process shape; ADR-0002 is marked **Proposed** with an explicit revisit condition for exactly this. |
| R2 | **A wake engine with unusable licensing or CPU cost.** | The vision requires local, offline, always-on. An engine needing an account, or burning 5% CPU while idle, breaks the product. | Written evaluation of candidates *before* implementation, in Phase 1's first task. |
| R3 | **Local STT for the utterance is heavy.** | One-shot capture depends on it. Whisper-class models are not small. | Architecture allows a single post-wake cloud call as a fallback, so a heavy local engine degrades latency rather than correctness. |
| R4 | **Two audio paths become permanent technical debt.** | Wake audio and room audio will be separate, and future maintainers will want to unify them. | ADR-0003 and ADR-0010 name the unification as forbidden, with the reason. |
| R5 | **The dashboard webview becomes load-bearing again.** | Every new feature is one `invoke` away, and that is the easy path. | ADR-0006 plus a guard test asserting the island does not import dashboard modules. |
| R6 | **A second app process (a separate runtime binary) doubles the lifecycle surface.** | Two binaries owning a child process and an event stream is a real operational cost. | ADR-0002 keeps it to one resident process; a second supervisor watching a first is explicitly rejected. |
| R7 | **The island fights the OS.** | Always-on-top transparent windows over other apps are a well-known source of focus stealing, wrong-monitor placement and z-order fights. | Exit criteria in Phase 2 are written as observable behaviours (no focus steal, correct monitor), not "it renders". |
| R8 | **Self-wake on the first test machine.** | The failure that makes a companion unusable and is blamed on "AI". | Suppression ships with the detector, not after; both false-accept and false-reject rates are measured. |
| R9 | **Scope gravity toward the full UI.** | The largest, most comfortable surface is the one the vision demotes. | ADR-0011 plus the surface-routing table in `VISION.md`, and the explicit note that the Focus workspace *was* the example of features that grew large and unconnected — it has since been removed in full. |
| R10 | **The wake phase lands as four disconnected components.** | Wake service, runtime, island and lifecycle each built separately all look fine and produce nothing a user can see. | Phase 1 is defined as one slice with one end-to-end exit criterion set, not as a component list. |
| R11 | **Renaming `agent_state_changed` breaks consumers.** | Tauri events are subscribed by name. | ADR-0012 requires the rename to be additive with a deprecation window, listed as migration work in the Phase 1 plan. |

---

## 5. Open questions

These are the questions a Phase 1 session must answer, with evidence, before writing
code. None has a default answer I am willing to commit to on the product's behalf.

1. **Resident process shape.** The existing Tauri binary with a tray and a hidden
   window, or a new Rust runtime that outlives it? Decided by measurement (R1), not
   taste. — *ADR-0002, open by design.*
2. **Wake engine.** Candidates evaluated on licence, offline operation, idle CPU,
   model size, multi-phrase support, and whether it can run in-process beside a
   Python worker without a second heavyweight runtime.
3. **Utterance transcription.** Local STT on the buffer, or one post-wake cloud call
   as the first slice with local as the Phase 8 destination?
4. **Island technology.** A second Tauri webview, or a native overlay that does not
   drag WebView2 into the wake path? This is the main determinant of R1 and R7.
5. **Which monitor, and how it is chosen.** Top-centre of the primary monitor is
   specified; "primary" needs a rule for when it changes, and a rule for a
   configuration with no primary.
6. **Default idle period.** Short by default per ADR-0008, but the right number is
   empirical: long enough for a three-sentence follow-up, short enough that a user
   does not notice a cost.
7. **Wake phrases, exactly.** The vision lists six styles; the engine's support for
   them, and the settings surface for them, are unresolved.
8. **Does a wake count as a turn for the permission ledger?** A wake carries no
   request; it must not consume an approval window or count toward a session's grant.
9. **What does the tray own in the first slice?** Minimum viable is "stop"; a menu
   with settings and "open interface" is arguably Phase 2. The vision calls the tray
   a runtime, not a surface.
10. **Sleep-command wording.** Where it is parsed — the model, or a runtime rule
    evaluated before dispatch? A runtime rule is faster and cheaper; the model is more
    forgiving. Phase 4, but the answer constrains the lifecycle API.
11. ~~**Do we keep the focus workspace's scope as-is?**~~ **Answered, and the
    decision went further than the question: the entire Focus workspace has been
    removed** — page, Rust store/commands/stats, Python `focus_store`/`focus_stats`
    and its documentation. It was the largest surface in the product and the vision
    did not mention it, so the drift resolved by deletion rather than by scoping.
12. **Packaging path.** `bundle.active: false` and a Python dependency block the
    clean-install criterion. Does Phase 1 add the autostart entry pointing at a dev
    checkout, or does packaging need to start earlier?

---

## 6. What to do next

**Phase 1, first task, before any code:** the written evaluation for questions 1–4,
with measurements where a measurement is possible. Everything else in the Phase 1
plan depends on those answers.

**Explicitly not next, and not to be started while Phase 1 is open:** any new tool,
any new full-UI page, memory, proactive behaviour, local model work, or a refactor of
existing tools. The existing seventeen tools, the permission system and the task
engine are in good shape and are not the bottleneck — reachability is.