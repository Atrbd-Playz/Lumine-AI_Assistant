# Lumine Frontend and Tauri Architecture

## How does Lumine appear and behave?

The frontend is a React/Vite application inside a Tauri shell. The current UI is intentionally designed to feel like a dedicated AI companion rather than a conventional chatbot.

The current implementation already shows the intended direction:

- desktop-oriented layout
- sidebar and main command space
- presence/avatar area
- theme controls with light/dark palettes
- chat activity panel that can be toggled
- animated avatar experimentation through the Avatar Lab
- conversation state abstraction using a service hook

This is a strong visual prototype, but it is not yet a desktop companion runtime.

## Verified current architecture

### Tauri shell

The app is configured in [lumine-ui/src-tauri/tauri.conf.json](lumine-ui/src-tauri/tauri.conf.json).

Verified implementation details:

- Tauri app identifier: `com.art.lumine-ui`
- app title: `lumine-ui`
- window dimensions: `800x600`
- Vite dev server on `http://localhost:1420`
- bundle enabled for release build
- default build runs `npm run build` before packaging

### Rust layer

The Rust layer is not yet a real orchestrator. It only contains a placeholder command:

- `greet(name: &str) -> String`
- registered with `tauri::generate_handler![greet]`

This means the Tauri app currently has a scaffolded Rust layer, but no desktop orchestration logic for:

- tray management
- agent lifecycle
- wake-word events
- audio or mic permission flow
- window visibility controls

### React app

The main UI shell is in [lumine-ui/src/pages/Home.tsx](lumine-ui/src/pages/Home.tsx).

It owns the page-level state for:

- navigation (`nav`)
- Lumine UI state (`idle`, `listening`, `thinking`, `speaking`)
- settings modal
- conversation panel visibility

This is a UI shell and not yet a real backend integration layer.

## Component organization

The home experience is organized as follows:

- [lumine-ui/src/pages/Home.tsx](lumine-ui/src/pages/Home.tsx) — page shell
- [lumine-ui/src/pages/home/components/MainSpace.tsx](lumine-ui/src/pages/home/components/MainSpace.tsx) — command surface and voice controls
- [lumine-ui/src/pages/home/components/Presence.tsx](lumine-ui/src/pages/home/components/Presence.tsx) — animated avatar and state mapping
- [lumine-ui/src/pages/home/components/ConversationPanel.tsx](lumine-ui/src/pages/home/components/ConversationPanel.tsx) — conversation activity panel
- [lumine-ui/src/pages/home/conversation/useConversation.ts](lumine-ui/src/pages/home/conversation/useConversation.ts) — conversation state logic and message lifecycle
- [lumine-ui/src/pages/home/hooks/usePreferences.ts](lumine-ui/src/pages/home/hooks/usePreferences.ts) — theme and appearance preferences
- [lumine-ui/src/pages/home/constants.ts](lumine-ui/src/pages/home/constants.ts) — default theme values

## UI state model

The current implementation defines the basic UI state in [lumine-ui/src/pages/home/types.ts](lumine-ui/src/pages/home/types.ts):

- `idle`
- `listening`
- `thinking`
- `speaking`

This matches the product direction, but there is no full state machine for sleep, wake, error, boot, or shutdown. Those remain future states.

## Avatar and presence

The avatar behavior is implemented in [lumine-ui/src/pages/home/components/Presence.tsx](lumine-ui/src/pages/home/components/Presence.tsx).

It currently uses:

- an SVG avatar asset
- a custom avatar engine (`LumineAvatarEngine`)
- idle montage randomness
- emotion/activity mapping from the UI state
- a debug Avatar Lab for managing emotion, activity, and animation overrides

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

The conversation layer is intentionally backend agnostic and currently defaults to a mock service in [lumine-ui/src/pages/home/conversation/useConversation.ts](lumine-ui/src/pages/home/conversation/useConversation.ts).

This is a real constraint:

- the UI does not currently talk to the Python agent
- conversation is mock-driven
- the backend event contract is planned, not implemented

The design comment in [lumine-ui/src/pages/home/README.md](lumine-ui/src/pages/home/README.md) points to the correct future direction: replace the mock service with a backend adapter when the real desktop agent is available.

## How the UI should communicate with the agent

The intended architecture is:

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

Instead, it should consume high-level state and events such as:

- `agent.state`
- `agent.connected`
- `agent.listening`
- `agent.thinking`
- `agent.speaking`
- `agent.error`

## System tray, window management, and wake word

These are all planned, not implemented.

Current gaps:

- [ ] system tray support
- [ ] hide/show main window
- [ ] minimize to tray
- [ ] sleep/wake transitions
- [ ] wake-word listener
- [ ] local desktop activation events
- [ ] agent lifecycle management

This means the app is not yet a true “Lumine desktop companion” experience. It is still a presentation prototype with a voice-state UI shell.

## Tauri responsibilities to be built

The future Tauri responsibilities should include:

- tray icon and context menu
- startup and session management
- agent process start/stop/restart
- event bridge between frontend and Rust
- native permission handling
- platform-aware window lifecycle
- wake-word event propagation
- logging and crash recovery

## Frontend roadmap

### Current status

- [x] React + Vite app scaffolded
- [x] Tauri shell scaffolded
- [x] desktop companion visual direction implemented
- [x] avatar presence and animation prototype implemented
- [x] light/dark theme and preset system implemented
- [x] architecture comments and feature separation exist
- [~] UI state is present but not backend-driven
- [ ] system-tray/desktop orchestration not implemented
- [ ] wake-word integration not implemented
- [ ] backend IPC not implemented

### Planned work

- [ ] Tauri IPC contract
- [ ] typed agent state events
- [ ] tray and window lifecycle
- [ ] agent manager commands
- [ ] wake-word integration
- [ ] state synchronization with avatar animation
- [ ] final desktop companion flow

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

## Summary

The UI currently behaves like a polished desktop companion prototype but remains a front-end shell. The important missing architectural leap is the Tauri orchestration layer: tray, agent manager, wake-word integration, and actual IPC/state synchronization with the Python agent.
