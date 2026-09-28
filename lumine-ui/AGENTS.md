# Lumine Frontend and Tauri Architecture

## How does Lumine appear and behave?

The frontend is a React/Vite application inside a Tauri shell. The current UI is intentionally designed to feel like a dedicated AI companion rather than a conventional chatbot.

The current implementation already shows the intended direction:

- desktop-oriented layout
- sidebar and main command space
- presence/avatar area, animating on its own
- theme controls with light/dark palettes
- chat activity panel that can be toggled
- a call bar rather than a microphone dock
- animated avatar experimentation through the Avatar Lab
- conversation state abstraction using a service hook

This is a working voice session wrapped in a strong visual prototype. What is missing
is the desktop shell around it — tray, window lifecycle, wake word, sidecar packaging.

## Verified current architecture

### Tauri shell

The app is configured in [lumine-ui/src-tauri/tauri.conf.json](lumine-ui/src-tauri/tauri.conf.json).

Verified implementation details:

- Tauri app identifier: `com.art.lumine`
- app title: `Lumine`
- window dimensions: `800x600`
- Vite dev server on `http://localhost:1420`
- `bundle.active: false`. Do not assume `npm run tauri build` produces an installer.
- default build runs `npm run build` before packaging
- `bundle.publisher` / `bundle.copyright` name the owner, and reach the binary's
  Windows version resource rather than only the installer. The Cargo package is
  `lumine`, so the executable is `lumine.exe`.

The identifier moved from `com.art.lumine-ui`. It is not cosmetic: it names
`app_local_data_dir()` and the OS keyring service, so a rename orphans both. Each
half reads the old name as a fallback instead — `credentials.rs` promotes a hit on
`LEGACY_SERVICE`, `settings_store` resolves to the legacy file, and
`config_store.py` searches `LEGACY_APP_IDENTIFIER`. `tauri build` writes
`lumine.exe`; a stale `lumine-ui.exe` in `target/release` is an abandoned artifact,
not a second app.

### Rust layer

The Rust layer is a real orchestrator for the process, and still a placeholder for
the desktop shell. What exists:

| Module | Owns |
| --- | --- |
| `agent_manager.rs` | The Python child process: start (idempotent), stop, restart, and a readiness gate that blocks until the worker registers with LiveKit or reports an error. Re-emits `LUMINE_EVENT` records as Tauri events. |
| `credentials.rs` | The OS keyring, **slot-addressed**. One entry per environment variable, not per provider. |
| `credential_injection.rs` | Builds the child's environment, one variable from its own slot. |
| `credential_probe.rs` | The secret's path — keyring, then environment, then one authenticated request. |
| `setup_status.rs` | Whether this install can hold a conversation, per slot, failing closed. |
| `settings_store.rs` | The versioned settings document, written atomically with a `.bak`. |
| `python_env.rs` | Shared interpreter and agent-directory resolution. |

What does not exist:

- tray management
- window visibility controls
- wake-word events
- an audio or mic permission flow

`credentials.rs` is write-only from the frontend's side on purpose: there is
deliberately no command that reads a stored secret back. See
`../docs/ai-control-center.md` for the slot-addressing decision.

### React app

The main UI shell is in [lumine-ui/src/pages/Home.tsx](lumine-ui/src/pages/Home.tsx).

It owns the page-level state for:

- navigation (`nav`)
- Lumine UI state (`idle`, `listening`, `thinking`, `speaking`)
- settings modal
- conversation panel visibility

This is a real integration layer for the voice path and a UI shell for everything
else. `Home.tsx` wires `useLumineVoice`, `useLocalMedia` and `useAgentRuntime` into
`MainSpace`, so the avatar, the transcript, the call bar and the toasts all reflect a
running session. What is still a shell is everything that is not a voice turn.

## Component organization

The home experience is organized as follows:

- [lumine-ui/src/pages/Home.tsx](lumine-ui/src/pages/Home.tsx) — page shell
- [lumine-ui/src/pages/home/components/MainSpace.tsx](lumine-ui/src/pages/home/components/MainSpace.tsx) — command surface, call bar, self-view
- [lumine-ui/src/pages/home/components/CallBar.tsx](lumine-ui/src/pages/home/components/CallBar.tsx) — idle handset, then mute / camera / screen / end
- [lumine-ui/src/pages/home/components/SelfView.tsx](lumine-ui/src/pages/home/components/SelfView.tsx) - the user's camera, and whether Lumine can see it
- [lumine-ui/src/pages/home/components/Presence.tsx](lumine-ui/src/pages/home/components/Presence.tsx) — animated avatar and state mapping
- [lumine-ui/src/pages/home/components/ConversationPanel.tsx](lumine-ui/src/pages/home/components/ConversationPanel.tsx) — conversation activity panel
- [lumine-ui/src/pages/home/components/WorkspaceView.tsx](lumine-ui/src/pages/home/components/WorkspaceView.tsx) — Memory, Activity and the tools grid
- [lumine-ui/src/pages/home/conversation/useConversation.ts](lumine-ui/src/pages/home/conversation/useConversation.ts) — conversation state logic and message lifecycle
- [lumine-ui/src/pages/home/hooks/usePreferences.ts](lumine-ui/src/pages/home/hooks/usePreferences.ts) — theme and appearance preferences
- [lumine-ui/src/pages/home/constants.ts](lumine-ui/src/pages/home/constants.ts) — default theme values
- [lumine-ui/src/components/avatar/useAvatarMontage.ts](lumine-ui/src/components/avatar/useAvatarMontage.ts) — the shared idle-montage queue driver

`Camera and screenshare are published to the room.` The frontend owns the capture
and `voice-manager.ts` owns the publication, and the split is deliberate: LiveKit's
own `setScreenShareEnabled` calls `getDisplayMedia` itself, which would mean a second
capture with a second permission prompt and a teardown this app cannot see. One
capture, one teardown, one `ended` handler.

Three rules hold it together:

- **One source, not two.** `useLocalMedia` exposes a single `active`, never two.
  LiveKit uses *only the most recently published video track*, so two independently
  toggleable sources have a state where the app shows the camera while Lumine looks
  at the screen — and the badge would be true of the track and false of the picture
  beside it.
- **The model gates publication, and the platform gates capture.** These are two
  different failures and `CallBar` takes two reasons for them. "This operating system
  cannot share a screen" is nonsense advice to fix by changing model, which is what a
  single combined reason would tell someone on macOS.
- **`SelfView`'s badge is driven by `isPublished`, not by what the model *can* do.**
  A model that cannot see leaves the preview honest about being local; a publish that
  failed must not claim a share it is not making.

`inputModalities` from the provider catalog is what decides both, and it is the same
field the Diagnostics matrix draws.

## UI state model

Two vocabularies, and they are not interchangeable.

**The room's state** — [lumine-ui/src/pages/home/types.ts](lumine-ui/src/pages/home/types.ts) — is what drives the avatar and the transcript:

- `idle`
- `listening`
- `thinking`
- `speaking`

**The worker's state** — `AgentState` in [lumine-ui/src/lib/agentRuntime.ts](lumine-ui/src/lib/agentRuntime.ts) — is the process's lifecycle, mirroring `agent_manager.rs`:

- `booting`, `ready`, `sleeping`, `listening`, `thinking`, `speaking`, `error`, `shutting_down`, `stopped`

Collapsing these is what made the original agent bug invisible: "the worker is
running" is not "Lumine is listening", and a worker can be up while its LiveKit
registration is missing. Diagnostics reports `running` and `connected` separately for
the same reason. `canServeRooms()` in `lib/agentRuntime.ts` is the honest test for
"can this process answer a room".

## Avatar and presence

The avatar behavior is implemented in [lumine-ui/src/pages/home/components/Presence.tsx](lumine-ui/src/pages/home/components/Presence.tsx).

It currently uses:

- an SVG avatar asset
- a custom avatar engine (`LumineAvatarEngine`)
- the shared idle montage, driven by `useAvatarMontage`
- emotion/activity mapping from the room state
- a debug Avatar Lab for managing emotion, activity, and animation overrides

The montage is a **queue driver**, not a `play()` call: each tick awaits the current
animation and then waits a per-state gap, so timing is self-adjusting. It yields to
a held reaction, and it *filters* the five gaze-touching animations rather than
switching the montage off when cursor gaze is on. The Avatar Lab runs the same
driver, so the two can no longer disagree about what "the montage" is.

This is a strong visual foundation for the future state-driven avatar system. The UI is intentionally not a generic chat bubble layout; it is more like a companion presence layer.

## Theme system and preset compatibility

The theme system is implemented in [lumine-ui/src/pages/home/constants.ts](lumine-ui/src/pages/home/constants.ts) and uses the expected preset structure:

- `light` -> preset fields
- `dark` -> preset fields

The preset keys include:

- accent
- icon
- canvas
- surface
- stage
- text
- avatar
- chatSurface
- chatUser
- chatAssistant
- chatText
- chatAccent

This matches the architecture described in the product requirements and should remain compatible.

## Conversation architecture

`ConversationPanel` and `useConversation` stay **backend-agnostic**. `useConversation`
keeps its `ConversationService` seam and still defaults to the empty mock, and
`ConversationPanel` still has no import from `features/voice`.

That constraint is still load-bearing, and the reason the voice path works at all is
that live messages enter through the **callbacks `Home.tsx` wires in**, not through a
panel that knows where they came from. The seam is the right place for a future
text-turn backend; it is not where the existing one goes.

The one thing that must not cross it is a tool payload. Those go to a toast via
`onToolResult`, never through `ConversationToolEvent` — a retrieved page rendered
inline in the transcript reads as though the model had been handed it by the user,
which is the shape a prompt injection wants.

## How the UI should communicate with the agent

This is the architecture, and it is the one that is implemented:

```text
React UI
  ↓
Tauri commands / events
  ↓
Rust orchestration layer
  ↓
Python agent manager / sidecar
```

The UI should not directly know about:

- Groq
- Cartesia
- LiveKit
- Python runtime internals
- wake-word implementation details
- VAD wiring

One honest exception, and it is deliberate: `features/voice/voice-manager.ts`
connects to the LiveKit room itself. The browser side has to hold the audio
connection, and the token is minted by a Tauri command precisely so no LiveKit
secret reaches the bundle. Everything *about* LiveKit — URLs, ids, worker names —
is confined to that module.

Instead, it should consume high-level state and events such as:

- `agent_started`
- `agent_state_changed`
- `agent_stopped`
- `agent_error`
- `agent_runtime`

`lumine-ui/src/lib/agentRuntime.ts` is where those are declared, together with the
`useAgentRuntime` hook that subscribes to them. Two rules about it:

- The names are **underscores**. An earlier draft of that file used `agent.state`
  and wrapped payloads in `{ type, payload }`; Rust emits `agent_started` with the
  `AgentStatus` object *as* the payload. Nothing caught it because nothing imported
  the file, which is why it is now wired rather than kept as a sketch.
- `agent_*` is the **desktop layer's** view of the child process; `agent_runtime` is
  the **worker's own** `LUMINE_EVENT` stream, forwarded verbatim. Do not collapse
  them -- "the worker is running" is not "Lumine is listening", and reading it as
  such is what made the original agent bug invisible.

## System tray, window management, and wake word

Agent lifecycle management is done: `agent_manager.rs` starts, stops and restarts
the worker, `voice-manager.ts` calls `start_agent` then `wait_for_agent_worker`
before dispatching, and the app reports what happens next if either fails. The rest
is planned, not implemented.

Current gaps:

- [ ] system tray support
- [ ] hide/show main window
- [ ] minimize to tray
- [ ] wake-word listener
- [ ] local desktop activation events
- [ ] a typed daemon protocol for sleep/wake — `AgentState` carries the state, but nothing
      *commands* the worker to enter one. LiveKit dispatch metadata plus the
      `LUMINE_EVENT` stream remain the seam.

So the app is not yet a true “Lumine desktop companion” experience. It is a working
voice session with a presentation prototype around it.

## Tauri responsibilities to be built

Still to build:

- tray icon and context menu
- hide/show and minimize-to-tray
- native permission handling
- platform-aware window lifecycle
- wake-word event propagation
- crash recovery and auto-restart
- sidecar packaging

Already built:

- agent process start/stop/restart, with an idempotent start
- startup and session management
- the event bridge between frontend and Rust
- worker readiness gating, so a dispatch never happens before registration

## Frontend roadmap

### Current status

- [x] React + Vite app scaffolded
- [x] Tauri shell scaffolded
- [x] desktop companion visual direction implemented
- [x] avatar presence and animation prototype implemented, and the montage actually running on the home screen
- [x] light/dark theme and preset system implemented
- [x] architecture comments and feature separation exist
- [x] backend IPC: agent lifecycle commands, credential slots, probes, setup status, and the provider catalog
- [x] typed agent state events (`lib/agentRuntime.ts`)
- [~] UI state is present but only the voice path is backend-driven
- [ ] system-tray/desktop orchestration not implemented
- [ ] wake-word integration not implemented

### Planned work

- [ ] tray and window lifecycle
- [ ] wake-word integration
- [ ] a daemon protocol for sleep/wake, distinct from `LUMINE_EVENT` plus dispatch metadata
- [ ] sidecar packaging, so end users never install Python
- [ ] final desktop companion flow

The frontend invokes Tauri agent lifecycle commands and listens for high-level
runtime events. `useLumineVoice` drives a real LiveKit session; the transcript,
emotion, notice and tool events reach the panel and the toasts.

`npm run tauri build` completes and writes `src-tauri/target/release/lumine.exe`,
with the Windows icon and version resources correctly embedded.

The Windows resource icon step was never the blocker, and this file claimed it was
for a while. `tauri.conf.json` sets no `bundle.icon`, and none is needed: the icons
under `src-tauri/icons` are complete, git-tracked and valid, and `tauri-winres`
finds `icon.ico` on its own. What actually failed was a poisoned `target/release`
cache from an interrupted run, which made `core` resolve to a metadata stub and
cascaded into 270 errors in `jsonptr` that read like a dependency fault. Run
`cargo clean --release` — the debug artifacts, and therefore `tauri dev`, are
untouched by it.

Because `bundle.active` is `false`, a successful build produces no installer. That is
the packaging decision, not a failure.

## Cross-platform and packaging constraints

- The app must remain cross-platform and not assume Windows-only logic.
- Native desktop features should be isolated behind abstractions.
- The Python agent should eventually be packaged as a sidecar or bundled executable.
- The app should be installable as a normal desktop application without requiring end users to manually install Python.

## Security and config boundaries

- Do not expose API keys to the frontend.
- Keep secrets outside the repo or in secure local environment configuration.
- Keep public UI config separate from private credentials and server-side secrets.
- Any third-party secret that cannot safely ship with the app should be treated as a packaging/security issue, not as a hidden default.

## Guardrails for future frontend changes

- Do not replace the current React/Vite/Tauri stack.
- Do not convert the project to a generic chatbot app.
- Do not break the light/dark preset schema.
- Do not hardcode desktop automation or wake-word assumptions into the UI.
- Do not couple React components directly to Python or LiveKit implementation details.
- Keep state and presentation separated.
- **One picker.** Every chooser is `components/ui/dropdown.tsx`. No native
  `<select>`, no `<datalist>`, no segmented button row, no wall of cards where a
  list belongs. A native popup is drawn by the OS, so it does not belong to the app.
- **The catalog decides, the UI draws.** Whether a setting exists, whether it is
  advanced, what its default is, and whether it is hidden are all declared in
  `agent/providers.py` and published. The UI holds no provider knowledge, including
  no knowledge of which environment variable a key goes in.

## Summary

The voice path is real: a live LiveKit session, a driven avatar, a transcript, and
failures that name their cause. The remaining architectural gap is the desktop shell
around it — tray, window lifecycle, wake word, and packaging the worker as a sidecar
so nobody has to install Python.
