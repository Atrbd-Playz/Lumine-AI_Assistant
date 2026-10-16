# Wave 1 — Always-on wake runtime

The detailed plan for Phase 1 of [`../../ROADMAP.md`](../../ROADMAP.md). Kept as a
separate file so the roadmap stays direction and this stays work — and so that
"Phase 1" is never read as permission to build a component list.

**Status:** proposed. Not authorised for implementation. Per the user's instruction,
Phase 1 waits until this plan has been read and explicitly approved.

Read first: [`VISION.md`](../../VISION.md), [`ARCHITECTURE.md`](../../ARCHITECTURE.md),
[`../gap-audit.md`](../gap-audit.md), and [`../decisions/`](../decisions/).

---

## 1. The slice

One vertical slice, end to end, observable by a person:

```text
Windows starts
    runtime starts, nothing appears
user: "Hey Lumine, what's the weather?"
    island appears within milliseconds
    the rest of the utterance is captured
    she answers out loud
    island stays for follow-up
    idle → island retracts, agent sleeps, wake listener still listening
```

**Nothing in this phase is shippable as a unit on its own.** Wake without an island
is invisible; an island without a lifecycle has nothing to render; a lifecycle
without a wake has nothing to transition from. If the phase stalls, the honest
partial state is "nothing", not four disconnected components.

## 2. Non-goals — the things this phase must not become

- No new tool. No new full-UI page. No memory. No proactive behaviour.
- No refactor of the existing tools, browser, WhatsApp or desktop control. They work;
  they are simply not reachable from the island yet.
- No barge-in (Phase 4). Suppression during synthesis is Phase 1; *interrupting*
  suppression is Phase 4.
- No multi-turn without a wake (Phase 4), although the slice naturally produces one
  follow-up — that is a consequence, not a feature.
- No packaging, no installer, no `bundle.active` change.
- No CI.

---

## 3. Pre-flight — four written evaluations, before any code

Open questions 1–4 in [`../gap-audit.md`](../gap-audit.md). Each is answered with
evidence, in a short document under `docs/`, and each names the choice and what would
have changed it. No implementation starts until all four are settled, because the
workstreams below are shaped by them.

### 3.1 Process shape (Q1)

Measure, not argue:

- Idle CPU and resident memory of the app with **no window created** versus **a
  hidden dashboard window** versus **a hidden island-only webview**.
- Time from process start to "wake service listening".
- Time from wake to first island pixel.

Decision rule: if a hidden dashboard webview costs a material share of the idle
budget, the resident runtime becomes its own Rust binary and ADR-0002 is superseded
in writing. A hidden 1280×820 React application at boot to host a 600px island is
exactly the "permanently open Electron/Tauri application" the vision refuses.

### 3.2 Wake engine (Q2)

Evaluate candidates on, in this order:

1. Licence, including commercial use and any redistribution requirement.
2. Offline operation. **Non-negotiable** — the engine may not require an account.
3. Idle CPU while listening, on the target machine.
4. Model size on disk.
5. Multi-phrase support, and whether a user can add phrases without a rebuild.
6. Score rather than boolean output, and a configurable threshold.
7. Whether it can run in-process beside a Python worker without a second heavyweight
   runtime, or whether it forces a Rust/native binding, a subprocess, or WASM.

Reject anything that needs a Python dependency the worker would inherit. The worker
is spawned per session and pays its import cost; it must not inherit a wake model.

### 3.3 Utterance transcription (Q3)

Measure a local engine's accuracy and latency on recorded phrases containing the
wake word plus a request, on this machine, with a headset and with laptop speakers.

Decision rule: if local is not usable on modest hardware, ship **one post-wake cloud
call on the captured utterance** as the first slice, with local as the Phase 8
destination. That fallback is architecturally legitimate — it is after wake, never
before — and it keeps the ring buffer, the lifecycle and the island unchanged.

### 3.4 Island technology (Q4)

- Tauri webview: reuse of the existing stack, existing CSS, one process. Cost: a
  WebView2 instance in the wake path.
- Native overlay: no WebView2 in the wake path, lowest latency, most code, no
  reuse of the React island design.

Decide with the §3.1 numbers and with the answer to "how big can the island's bundle
be before wake-to-pixel suffers".

---

## 4. Workstreams

Each names the files it touches. Existing files are named so a reviewer can see
exactly what is being modified — the reviewable diff is the point.

### W1 — Resident runtime and process lifetime

Removes C1 from the gap audit. The runtime outlives the UI.

| Step | Work |
| --- | --- |
| 1.1 | Add tray presence and an autostart entry (per Q1's process shape). Windows-first via `HKCU\...\Run`; keep the mechanism behind a trait so macOS/Linux are not carved out. |
| 1.2 | Start hidden at boot: no main window until asked. Add the capability to create the dashboard window on demand and to re-show an existing one. |
| 1.3 | Single-instance guard, so a second launch focuses the existing window instead of starting a second resident runtime. Two wake services on one microphone is a self-wake loop. |
| 1.4 | Move `AgentManager`'s ownership from "the app" to "the runtime", with no behaviour change. Files: `lumine-ui/src-tauri/src/agent_manager.rs`, `lib.rs`. |
| 1.5 | Do **not** touch `stop()`'s `child.kill()` in this phase. Graceful shutdown is Phase 3 (C7); changing it here would conflate two phases. |

`lib.rs` note: any new command that shells out to Python must be `async` and go
through `run_off_thread`. That is a bug that already happened once.

### W2 — Wake / attention service

The largest new piece, and the one ADR-0003 and ADR-0004 constrain.

| Step | Work |
| --- | --- |
| 2.1 | Local capture path in the resident runtime. **Not** LiveKit, **not** the webview. One owner, documented in the module docstring. |
| 2.2 | Ring buffer sized for pre-roll plus a continuation window. Its contents never leave the process except as a completed utterance. |
| 2.3 | Detector with the engine from §3.2, wrapped so the engine is replaceable behind one interface. |
| 2.4 | Threshold, phrase list, cooldown, and an explicit suppression gate. ADR-0009: suppression is **muted**, not merely less sensitive. |
| 2.5 | Output: one `wake` record with phrase, score, whether a usable utterance was captured, and its duration. Nothing else crosses this boundary. |
| 2.6 | A read-only CLI probe beside the existing ones (`agent/*_probe.py` convention): prints what the detector heard, the score, and whether suppression was engaged. This is how the engine gets tuned without a demo. |

### W3 — Lifecycle machine

| Step | Work |
| --- | --- |
| 3.1 | The state as **data**, with the transition table beside it, and intents (`request_wake`, `request_sleep`, `request_end`, `request_ui`) rather than direct state writes. |
| 3.2 | Table-driven tests over every legal and illegal transition. The dangerous cases are named in the test, not left to be discovered: wake during an approval, wake during `EXECUTING`, sleep during `SPEAKING`, error during `WAKING`. |
| 3.3 | Publish `lifecycle_state` (`from`, `to`, `cause`) on every transition. |
| 3.4 | Split process state from companion state (C4, ADR-0012): rename the Rust enum's variants, keep the old Tauri event working, add the new one, record the migration. |
| 3.5 | Retire or restore the four never-constructed `AgentState` variants. Leaving them is what made the enum misleading. |
| 3.6 | Derive `LumineState` in `types.ts` from `lifecycle_state`, and make it a **total** `Record` so a new state fails the build. |

### W4 — The island

| Step | Work |
| --- | --- |
| 4.1 | New window with `transparent`, `alwaysOnTop`, `decorations: false`, `skipTaskbar`, `visible: false`, `focus: false`. Per Q4's decision. Its own capability entry in `capabilities/default.json`. |
| 4.2 | Its own entry point and bundle entry. **No** import from `pages/` or `features/settings`. |
| 4.3 | Position top-centre of the primary monitor, re-evaluated on monitor change. |
| 4.4 | Appear/retract animation. Appearing must not take focus. |
| 4.5 | Render: acknowledgement, state, current exchange. Tool progress and permission cards arrive in Phase 2 — the records already exist, so this is presentation. |
| 4.6 | A guard test asserting the island's bundle does not import dashboard modules (R5). |

### W5 — One-shot handoff and the conditional greeting

| Step | Work |
| --- | --- |
| 5.1 | On wake, extract the utterance from the buffer: pre-roll through the endpoint. |
| 5.2 | Hand it to the session as the **first turn**, so agent startup latency costs nothing the user can hear. |
| 5.3 | Greeting becomes conditional (C3, ADR-0013): skipped when the session opened with a captured utterance, preserved otherwise. One branch at session construction, plus a `greeting` field on `session_started`. |
| 5.4 | A wake with no usable utterance degrades to a normal wake — the island shows listening. Never an error. |
| 5.5 | The captured utterance enters the same untrusted-wrap discipline as tool output. It is text a human said in a room that may have had a television on. |

Files: `agent/agent.py` (the greeting branch, `session_started`), the new lifecycle
handoff, `runtime/tool_results.py` (the wrap, reused not reimplemented).

### W6 — Configuration

New namespaces in `lumine.config.json`, following the conventions in
`settings/validation.py` exactly:

```jsonc
{
  "wake": {
    "enabled": "on" | "off",
    "phrases": ["hey lumine", "lumine"],
    "threshold": 0.55,          // range-checked
    "suppressWhileSpeaking": true,
    "cooldownMs": 1200
  },
  "lifecycle": {
    "idleTimeoutSeconds": 45,   // 0 disables
    "greeting": "auto"          // "auto" honours the utterance; "always"/"never"
  }
}
```

- `wake.enabled` **grants** capability (it opens the microphone): an unrecognised
  value reads as `off` and is an **error**, exactly like `desktop.input` and
  `media.enabled`.
- `threshold`, `cooldownMs`, `idleTimeoutSeconds` are range-checked as **warnings**,
  like `permissions.timeout`.
- `wake.phrases` must be non-empty when enabled; an empty list is an error, because a
  wake detector with no phrases is a microphone with no purpose.

This is the only settings surface the vision justifies adding in this phase, and it
exists because a user who cannot wake her, or who needs a looser threshold on a noisy
machine, otherwise has a product they cannot use.

### W7 — Idle and sleep

| Step | Work |
| --- | --- |
| 7.1 | `IDLE → SLEEPING` on the configured quiet period, with the timer suspended by `THINKING`, `EXECUTING`, `SPEAKING`, a pending approval, a running task and error recovery. |
| 7.2 | Emit `idle_transition` with the cause, so "why did she stop listening?" is answerable from a log. |
| 7.3 | A wake never counts as a turn for the permission ledger, and never consumes an approval window. |

Sleep is Phase 3 in full; what lands here is the timeout and the distinction.

### W8 — Verification

Following the conventions already in the repository — the real boundary, not a fake
that cannot fail.

| Kind | What |
| --- | --- |
| Pure unit | The transition table (every legal and illegal pair); ring-buffer arithmetic; threshold/cooldown policy; the greeting branch; config validation. |
| CLI probe | The wake probe from 2.6 — read-only, no agent, no provider. |
| Staged integration | Extend the `desktop_smoke.py` convention: **read-only assertions first, self-audio assertions next, anything that speaks or moves last.** The ordering is the safety argument. |
| Contract check | Island bundle imports no dashboard modules; island state table is total; Phase 3's additive event rename did not break a subscriber. |
| Manual, on the target machine | The `VISION.md` success-criteria script, steps 1–8, with the two latencies recorded: wake → visible, utterance → first spoken word. |

---

## 5. Acceptance criteria

The phase is done when all of these are true on the target machine, not when the code
exists. These map one-to-one to Phase 1's exit criteria in the roadmap.

1. After a reboot, nothing appears. No window, no toast, no notification.
2. Idle resident CPU and memory are measured, published, and inside the agreed budget.
3. A wake phrase produces visible acknowledgement within the stated latency.
4. **"Hey Lumine, what's the weather?" is answered as one interaction**, with no
   greeting and no repetition of the request.
5. At least three invocation styles work, at the configured threshold.
6. Suppression holds: she does not wake on her own voice, on silence, or on background
   media. Both false-accept and false-reject rates are recorded.
7. A bare wake ("Hey Lumine", nothing after) shows listening and waits — it does not
   error and does not greet over the user's next sentence.
8. Follow-up without a wake phrase works within the idle window.
9. The full UI never opened during any of the above.
10. A second launch focuses the existing window instead of starting a second runtime.
11. The existing Python suite, `npm run check` and `cargo check` all still pass, with
    no regression in any currently verified behaviour.

## 6. Kill criteria — signals to stop and reconsider

- The island cannot acknowledge a wake within the latency budget in any reasonable
  implementation. That is an architecture signal, not a tuning problem.
- The idle resident cost cannot be brought inside a budget that is honest about "she
  is always on". Then the always-available premise is wrong, and the vision needs
  revisiting rather than the implementation.
- Wake false-accepts cannot be controlled with suppression plus a threshold. Then
  detector choice, not detector tuning, is the problem.
- Local utterance transcription is unusable and the cloud fallback is unacceptable on
  privacy grounds. Then the one-shot design needs a different shape, and ADR-0007
  should be reopened rather than worked around.

Each of these is a decision point, not a setback. Record it, supersede the ADR, and
say so.

## 7. Files this phase will touch

Existing, named in advance so the diff is reviewable:

- `lumine-ui/src-tauri/src/lib.rs` — runtime setup, new commands
- `lumine-ui/src-tauri/src/agent_manager.rs` — ownership, state rename
- `lumine-ui/src-tauri/tauri.conf.json`, `capabilities/default.json` — island window
- `lumine-ui/src/features/voice/voice-manager.ts` — handoff source, wake wake-path
- `lumine-ui/src/pages/home/types.ts` — derived `LumineState`
- `agent/agent.py` — greeting branch, `session_started`, session-open handshake
- `agent/settings/config_store.py`, `agent/settings/validation.py` — `wake`,
  `lifecycle`
- `agent/runtime/` — the lifecycle machine's Python side, `idle_transition`
- `agent/tools/tool_results.py` — the utterance wrap, reused

New, with a name each:

- the wake service module, owned by the runtime
- the lifecycle machine, owned by the runtime
- the island entry point and its state tables
- the wake probe CLI

Untouched, deliberately: every tool, the browser, WhatsApp, desktop control, the
permission gate's decision logic, the persona, the avatar artwork.

## 8. Open decisions inside the phase

These are for the Phase 1 session to settle with evidence, and to write into
`docs/decisions/` rather than into code comments:

- The resident process shape (supersedes ADR-0002 if a separate binary wins).
- The wake engine and the STT engine, with the evaluation that chose them.
- Whether the island is a webview or a native overlay.
- The primary-monitor rule and the no-primary-monitor fallback.
- The default idle period.
- Whether the tray ships with a menu in this phase or only a quit action.