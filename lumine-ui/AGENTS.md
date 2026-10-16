# Lumine Frontend and Tauri Architecture

## This is not the whole product

The full UI is **Lumine's home, not her existence**. She must be reachable by voice
with no window open, and she must appear in a small transient island rather than
opening this dashboard.

Read [`../VISION.md`](../VISION.md) and [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
first. Both of them describe the target; the lists lower down in this file record
what exists today, and the gap between the two is deliberate and measured in
[`../docs/gap-audit.md`](../docs/gap-audit.md).

Consequences that apply to work in this directory *now*, before Phase 1 lands:

- **A feature must be routed to a surface before it is built.** Island, full UI,
  tray, or voice — see the routing table in [`../VISION.md`](../VISION.md). The full
  UI is the comfortable place to put things and it is explicitly not the place for
  anything a user wants during a spoken exchange.
- **`AgentState` is the process, not the companion.** `sleeping` there means "no
  worker process"; the companion's `SLEEPING` means the opposite, and the two must
  not share names or be collapsed
  ([ADR-0012](../docs/decisions/0012-process-vs-companion-state.md)).
- **`LumineState` becomes a derived view** of the lifecycle event, and its state
  tables stay total `Record`s so a new state fails the build
  ([ADR-0005](../docs/decisions/0005-single-lifecycle-authority.md)).
- **The island will be a separate webview with its own entry point**, and it must
  never import from `pages/` or `features/settings`. The dashboard bundle is not
  the island's bundle ([ADR-0006](../docs/decisions/0006-island-as-primary-surface.md)).
- **The island will not be able to reuse a Tauri window API call, because there
  isn't one.** The frontend today imports `invoke`, `listen` and `convertFileSrc`
  and nothing else — no `@tauri-apps/api/window` anywhere. Window geometry, the
  always-on-top island and monitor selection are new Rust work.
- **Do not make this app a prerequisite for anything.** If a capability can only be
  reached from here, it is misfiled.

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
- Vite dev server on `http://localhost:15173`. It is neither the Tauri-conventional
  `1420` nor Vite's own `5173`: Hyper-V/WSL redraws 100-port exclusion blocks inside
  the Windows dynamic port range (measured here as `1024-15000`) on every boot, and an
  excluded port refuses to bind with `EACCES` rather than `EADDRINUSE`, so `strictPort`
  surfaces it as a bare "permission denied". `1420` fell to `1351-1450` and `5173` to
  `5156-5255`; `15173` sits above the range, so it survives the redraw. The HMR port
  is `15174`. This must stay in step with `build.devUrl` in `tauri.conf.json`, which
  is static JSON with no env interpolation.
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

- [lumine-ui/src/pages/Home.tsx](lumine-ui/src/pages/Home.tsx) — page shell, and the
  `Suspense` boundary every non-home destination loads behind
- [lumine-ui/src/pages/home/components/MainSpace.tsx](lumine-ui/src/pages/home/components/MainSpace.tsx) — command surface, call bar, self-view
- [lumine-ui/src/pages/home/components/VisionDeck.tsx](lumine-ui/src/pages/home/components/VisionDeck.tsx) - the one corner that answers "what is Lumine looking at": self-view, browser, desktop
- [lumine-ui/src/pages/home/components/CallBar.tsx](lumine-ui/src/pages/home/components/CallBar.tsx) — idle handset, then mute / camera / screen / end
- [lumine-ui/src/pages/home/components/SelfView.tsx](lumine-ui/src/pages/home/components/SelfView.tsx) - the user's camera, and whether Lumine can see it
- [lumine-ui/src/pages/home/components/Presence.tsx](lumine-ui/src/pages/home/components/Presence.tsx) — animated avatar and state mapping
- [lumine-ui/src/pages/home/components/ConversationPanel.tsx](lumine-ui/src/pages/home/components/ConversationPanel.tsx) — conversation activity panel, and the transcript's follow-the-tail rule
- [lumine-ui/src/pages/home/components/ActivityTimeline.tsx](lumine-ui/src/pages/home/components/ActivityTimeline.tsx) — the Activity page: counters, category filter, dated rows
- [lumine-ui/src/pages/home/components/WorkspaceView.tsx](lumine-ui/src/pages/home/components/WorkspaceView.tsx) — Memory and the tools grid
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

## Music is a widget and a permission, not a settings tab

There is **no Music section in Settings**, and nothing on the music path fetches a
track list. The card is `pages/home/widgets/LocalWidgets.tsx::MusicWidget`, beside
Weather and News; the folder grant lives in `pages/settings/system/PermissionsPage.tsx`.

Two reasons, and the second is the one that matters.

- **A filename is not a setting.** The Music tab was a library browser — a search
  box, a playlist rail, and every track in the folder — and the reported complaint
  was *"there's so much loading in the music settings"*. Most of that screen was a
  list of files a person cannot change.
- **A text box matching characters is worse than Lumine matching words.** She
  searches by what you *said*, tolerates spelling, and reads "vocal" as a version
  rather than as part of a title. Typing was strictly worse at the only job the
  widget could do.

**The widget reads nothing from the worker.** The folder's name is the last path
segment of a string the saved document already gave it, so the card starts **no
subprocess** — which is the entire loading fix, and the reason the card can be on
the home page at all. A widget that called `inspect_music_folder` on every mount
would put a ~400 ms interpreter start in front of the avatar. The *count* is still
real and is paid exactly once, on the Permissions screen where the folder is
chosen: a mistargeted folder (`D:\Music` when the music is in `D:\Music\Library`)
is otherwise completely silent.

`queue_music` is the card's **Play all**, and it queues rather than launches — one
`.m3u8` handed to the player's own window, so it plays in order and skip/pause/stop
are that window's controls. The card says so underneath the button, because a widget
that claimed "playing" while the user hunted for the window would be lying. The
webview only ever supplies a **subfolder name**, never a path; the script resolves
the library root from the saved document and `scanner.is_inside_library` is what
stands between the page and a filename.

## The activity log survives a reload, and holds far more

`features/activity/activityFeed.ts` restores from `localStorage` in its `useState`
**initialiser** — which is what makes yesterday's rows appear in the first paint
rather than a render later — and persists on a trailing one-second write, flushed
on `pagehide`. Every access is guarded: `localStorage` throws in a private window
and on a full quota, and a telemetry view must not be the thing that takes the app
down, the same rule `runtime_events.write_event_line` follows on the other side of
the pipe.

The cap went from 200 to **2,000**, and the reason is arithmetic rather than taste.
Counting one real session's `LUMINE_EVENT` stream, a *single* user turn produces
about ten `latency` records plus a `session_usage`, a `conversation` pair and four
`emotion` records — so 200 entries is roughly **twenty turns**, and the Activity
page silently discarded everything earlier in the same session with no indication
it had. "The activity logs are capped to few logs" was that, plus the fact that
quitting the app discarded the lot.

`clear` empties the saved copy too. A Clear that left the rows on disk would bring
them straight back on the next launch.

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

### The transcript follows the tail, and only when the reader is there

`ConversationPanel` owns one rule: it scrolls to the newest row whenever the reader
is already at the bottom, and otherwise says how many rows arrived. Three pieces
make that work, and each of them was a bug first.

- **`pinned` is a sticky boolean with a `scroll` listener**, not a number recomputed
  when `items` changes. Recomputing inside the growth effect means the reader's own
  scroll is not observed at all.
- **`useLayoutEffect` and `behavior: "auto"`**, never `useEffect` and
  `"smooth"`. Streaming transcription re-runs this several times a second; a smooth
  animation restarted each time always chases a target that just moved, so the tail
  visibly lags behind the speaker.
- **Every measurement is guarded on `clientHeight > 0`.** The panel is only
  `hidden`, not unmounted, so while it is shut every measurement is zero and
  "am I at the tail" answers *yes* — which is how messages used to arrive into a
  hidden transcript, be counted as seen, and never raise the jump button.

`useConversation.updateMessage` returns the *same array* when no row actually
differs. Streaming calls it per interim chunk, and a fresh identity each time
re-rendered the whole transcript and re-ran the scroll effect for a change to one
line.

## Where the notification stack lives

`components/ui/toast.tsx`, mounted by `App.tsx`, is the only notification surface.
Three decisions are load-bearing and none of them is cosmetic.

- **Bottom-left, measured from the layout's own numbers.** `--sidebar-width` and
  `--call-h` are the two rows it has to clear, and both are declared on `:root` so
  a portalled stack can read them; `useDocumentTheme` mirrors the collapsed rail
  there for the same reason it mirrors the palette. The top centre was the previous
  home and it sat on the topbar's controls.
- **A flow column, and `ToastContent` deliberately ignores `data-behind`.**
  base-ui's peek folds every toast behind the frontmost one away; wired to
  `opacity: 0` inside a flow column that produced a stack of *blank* coloured
  cards. The cap on how many there are is the provider's `limit`.
- **A repeat of a message already on screen refreshes that card.** `useNotify`
  passes a content-derived id, and base-ui's `toast.add` treats a repeat id as an
  update — so a failure circuit that emits the same line per turn gets one card
  with a restarting timer rather than a column of identical ones.

## Lazy loading, and why only two pages still use it

**Two destinations are behind `Suspense`: `WorkspaceView` (Tools) and
`ActivityTimeline`.** Everything else — the avatar lab, the settings overlay
and all five settings pages — is a static import again, and the settings overlay's
own `Suspense` boundary is gone with it.

This used to be the opposite. The original reasoning was sound: `Home.tsx` is a
ternary chain rather than a router, so every destination was in the initial bundle
on a launch whose first screen is the avatar, and splitting them shrinks the first
chunk.

It was right for a *heavy* module and wrong as a blanket, and the cost was visible:
every small page cost a request, a parse of an extra chunk and a suspense
boundary, and bought a blank frame on the way in. The reported symptom was tabs
that visibly stalled when opened. So the rule is now **weight, not count**, and two
modules clear it — `WorkspaceView`, because `get_tool_catalog` costs ~6 s of
`python agent/tool_catalog.py` importing `livekit-agents`, and `ActivityTimeline`,
the largest single view in the app. `PAGE_FALLBACKS` keeps its entries so a page
name cannot silently drift, but only those two boundaries render anything.

**Do not add lazy loading back to a small page.** The home subtree
(`MainSpace`, transcript, rail, avatar) was never split either: it is the first
screen, so deferring it would only move the wait.

This is not what made the Tools page freeze. `get_tool_catalog` was a *synchronous*
Tauri command, and Tauri runs those on the main thread — a cold
`python agent/tool_catalog.py` spends ~6 s importing `livekit-agents`, so the whole
window stopped repainting and navigating during it produced a Windows "not
responding" dialog. Every command that shells out is `async` and hands its blocking
half to `spawn_blocking` now, the provider catalog and effective configuration are
cached in `lib.rs` (the latter dropped by `save_config`), and `python_env.rs` bounds
every helper with a deadline and drains its pipes on their own threads.

Two more rules came out of it. `aiConfigClient.call` coalesces concurrent identical
invocations, so React StrictMode's double mount and the two hooks that want the same
catalog share one read rather than paying for it twice. And `Home.tsx` prewarms the
tool catalog on idle — the six seconds are spent during launch rather than in front
of the first person who opens Tools.

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
  `agent/settings/providers.py` and published. The UI holds no provider knowledge, including
  no knowledge of which environment variable a key goes in.
- **The Tools page has one accent, and it is a minority by measurement.** A card's
  colour comes from `tools_registry.TOOL_CARDS` — `effect` (`answers`/`acts`/`opens`/
  `sends`) and `permission` (`ask`/`auto`) — and the accent fires on **`sends` only**.
  Both earlier rules were wrong and both read as obvious improvements in a diff: on
  "asks you first" (**9 of 17** cards, so the accent meant "a tool") and on "can act at
  all" (**11 of 17**, worse). `agent/tests/test_tool_card_accent.py` counts the catalog and
  fails if the accented partition stops being a minority or stops being `send_whatsapp`,
  because a stylesheet cannot count and the next person to move this accent will have the
  same reason I did. It is also the only authority a *label* cannot tell you: "Send a
  WhatsApp" says nothing about what it does to another person.
- **A card is a label, not a paragraph, and 22 words is the budget.** `CardSummaryBudgetTests`
  fails a longer one. Seven of seventeen were over it — the longest at 71 words — and two
  named verbs that no longer existed, because `TOOL_CARDS` is written separately from the
  docstring, so removing a verb does not touch it. The same class asserts `desktop_control`
  offers exactly `ops.VERBS`. Parameters live behind a `<details>` for the same reason:
  `web_control`'s twelve settings made its card three times its neighbours' and the grid
  unreadable. The tool id is on hover, kept in the accessibility tree via `opacity`.
- **Toggle one switch and every card reconciles.** `toggle` re-reads the whole catalog
  after a save. Updating only the clicked card left every *other* card stale, so turning the
  desktop control on did not visibly silence `browse` and `web_control` until the next app
  run — the second-order effect is decided by the worker, not by the click. Two Rust-side
  facts make the read honest: `save_config` drops `CATALOG_CONFIG` **and** `CATALOG_TOOL`
  (every card's `enabled` is computed from that document, so clearing only the config cache
  made a written switch appear to revert), and `clear_tool_catalog` exists for the same
  reason.
- **The exclusion needs the tool it protects to be live.** `desktop.input` and
  `tools.desktop_control` are separate keys, so a document can carry
  `desktop.input = on` beside `tools.desktop_control = off`; reading the switch alone
  silenced two browser tools on the authority of a capability nobody has — the user turned
  it on, turned it off again, and lost the tools they had. `disabled_reason` re-checks the
  desktop tool before firing. Found by enumerating the precedence, because each end alone
  behaves.

## Summary

The voice path is real: a live LiveKit session, a driven avatar, a transcript, and
failures that name their cause. The remaining architectural gap is the desktop shell
around it — tray, window lifecycle, wake word, and packaging the worker as a sidecar
so nobody has to install Python.
