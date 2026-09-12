# Lumine Architecture

## What is Lumine?

Lumine is a cross-platform personal AI companion desktop application intended to feel like a quiet, always-available desktop presence rather than a generic chatbot.

The current repository is an early prototype of that vision:

- a Python LiveKit agent is configured and running as a voice AI service,
- a React/Vite app is scaffolded inside Tauri,
- the visual direction is a desktop companion UI with an animated avatar, focus-style panels, and theme presets,
- the full desktop orchestration model is not yet implemented.

The product goal is a local desktop experience in which Lumine can sit in the system tray, wake on a local wake phrase, open its UI, and then engage in voice-first interaction while remaining lightweight and responsive.

## How is Lumine architected?

The repository currently reflects a split between an AI backend and a desktop frontend, but the runtime boundary is not yet fully connected.

```text
LUMINE DESKTOP (target architecture)
┌──────────────────────────────────────────────┐
a│                                              │
│  Tauri desktop app                           │
│  ├─ React UI                                 │
│  ├─ Rust orchestration layer                 │
│  ├─ tray / window / lifecycle management     │
│  └─ wake-word + IPC bridge                  │
└───────────────────────┬──────────────────────┘
                        │
                        ▼
            Python AI agent sidecar / service
            ├─ VAD (Silero)
            ├─ STT (Groq)
            ├─ LLM (Groq)
            ├─ TTS (Cartesia)
            └─ LiveKit Cloud realtime channel
```

### Current repository reality

```text
CURRENT REPO (verified)
├─ Python agent: agent/agent.py
│  └─ LiveKit worker with VAD + STT + LLM + TTS
├─ Persona: agent/prompts/persona.md
├─ UI shell: lumine-ui/src/
│  └─ React app with desktop companion design
├─ Tauri shell: lumine-ui/src-tauri/
│  └─ scaffolded app with default Rust entrypoint
└─ Integration: not yet implemented end-to-end
```

## Local versus cloud boundary

The preferred architecture is local-first desktop orchestration with cloud AI services used as dependencies.

### Local responsibilities (desired)

- Tauri application lifecycle
- system tray behavior
- window show/hide management
- local microphone access and wake-word listening
- agent process management
- local IPC between UI and Rust core
- user-facing state and avatar animation

### Cloud responsibilities (current / intended)

- LiveKit Cloud realtime communication
- Groq STT and LLM APIs
- Cartesia TTS APIs
- optional future account sync or cloud memory services

### Explicit design rule

Lumine should not be replaced by a cloud-only backend. The desktop app remains useful as a locally running companion even when AI processing relies on cloud APIs.

## Cross-platform requirements

Lumine is intended to be a cross-platform desktop application.

- Primary target: Windows
- Future targets: macOS, Linux
- Potential future targets: Android, iOS

Platform-specific responsibilities such as tray icons, startup integration, microphone permissions, audio devices, wake-word detection, and native notifications should be abstracted behind platform-aware boundaries rather than hardcoded into the UI or agent logic.

## Repository map

- [README.md](README.md) — root repository overview; currently minimal placeholder
- [agent/](agent/) — Python AI backend and persona assets
- [agent/agent.py](agent/agent.py) — LiveKit-based agent entrypoint
- [agent/requirements.txt](agent/requirements.txt) — backend Python dependencies
- [agent/prompts/persona.md](agent/prompts/persona.md) — Lumine persona
- [lumine-ui/](lumine-ui/) — Tauri + React desktop frontend
- [lumine-ui/src-tauri/](lumine-ui/src-tauri/) — Rust/Tauri desktop shell and package config
- [lumine-ui/src/](lumine-ui/src/) — React source and UI implementation
- [lumine-ui/package.json](lumine-ui/package.json) — frontend scripts and dependencies

## Major technologies in use

Current verified technologies:

- Python
- LiveKit Agents
- LiveKit Cloud
- Groq STT
- Groq LLM
- Silero VAD
- Cartesia TTS
- Tauri 2
- React 19
- Vite
- TypeScript

## Current project phase

Current project phase: PHASE 1 — Current AI Agent

This means the backend voice agent is the most mature implemented piece. The desktop shell exists but is not yet a true orchestrator or production companion app.

The next required milestone is Phase 2: Tauri ↔ Agent Integration.

## Roadmap and status

### PHASE 0 — Foundation

- [x] Define repository architecture
- [x] Establish architecture documentation
- [~] Establish frontend/backend boundaries
- [~] Establish development commands

### PHASE 1 — Current AI Agent

- [x] Python agent
- [x] LiveKit integration
- [x] Groq STT
- [x] Groq LLM
- [x] Cartesia TTS
- [x] Silero VAD
- [x] Persona
- [~] Environment configuration
- [~] Error handling

### PHASE 2 — Tauri ↔ Agent Integration

- [~] Agent manager
- [~] Start agent
- [~] Stop agent
- [~] Restart agent
- [~] Agent health/status
- [~] IPC protocol
- [~] Agent lifecycle events
- [~] Graceful shutdown

Current verified status: the Tauri side has a Rust agent manager and IPC command contract in place, but the application is still blocked from a clean build by a Tauri Windows resource issue in the bundle configuration.

### PHASE 3 — Desktop Experience

- [ ] System tray
- [ ] Hide/show main window
- [ ] Minimize to tray
- [ ] Tray context menu
- [ ] Sleep state
- [ ] Wake state
- [ ] Startup behavior

### PHASE 4 — Wake Word

- [ ] Select wake-word technology
- [ ] Local microphone listener
- [ ] "Hey Lumine"
- [ ] Wake-word → Tauri event
- [ ] Wake-word → UI
- [ ] Wake-word → agent
- [ ] False-positive handling
- [ ] Cross-platform audio compatibility

### PHASE 5 — Agent State / Avatar

- [ ] Unified Lumine state machine
- [ ] UI state synchronization
- [ ] Avatar state synchronization
- [ ] Wake animation
- [ ] Listening animation
- [ ] Thinking animation
- [ ] Speaking animation
- [ ] Sleeping animation
- [ ] Error animation/state

### PHASE 6 — Packaging

- [ ] Package Python agent
- [ ] Create platform-specific agent binaries
- [ ] Configure Tauri sidecar/resources
- [ ] Windows build
- [ ] macOS build
- [ ] Linux build
- [ ] Installer
- [ ] Environment/configuration strategy
- [ ] Logging
- [ ] Crash recovery
- [ ] Agent auto-restart

### PHASE 7 — Production Desktop Application

- [ ] Start with OS
- [ ] Settings
- [ ] Permissions
- [ ] Update mechanism
- [ ] Diagnostics
- [ ] Logging
- [ ] Secure configuration
- [ ] Versioning
- [ ] Release pipeline

### PHASE 8 — Optional Cloud Platform

- [ ] Authentication
- [ ] Cloud memory
- [ ] Sync
- [ ] Remote configuration
- [ ] Multi-device support
- [ ] Optional web/mobile companion

## Build and packaging philosophy

- Keep the Python agent as a managed service rather than as the application controller.
- Treat Tauri as the desktop orchestration layer.
- Keep the desktop application locally useful even when it depends on cloud AI APIs.
- Do not bundle a full LiveKit server unless there is an explicit future need.
- Prefer a sidecar/bundled Python executable model for the agent over a cloud-only solution.
- Do not require an end user to manually install Python.

## Security principles

- Never hardcode API keys, LiveKit secrets, Groq keys, Cartesia keys, or auth tokens.
- Keep secrets in environment variables or OS-secured configuration only.
- Do not expose private credentials to the React frontend.
- Distinguish clearly between public config and user secrets.
- If a third-party API cannot be safely embedded in a desktop app, document that limitation instead of pretending it is secure.

## Development rules for AI coding agents

- Do not rewrite working code just to improve architecture in isolation.
- Do not replace the existing LiveKit architecture.
- Do not replace the current React/Vite/Tauri stack.
- Do not replace the Python agent unless there is a clear reason.
- Do not assume Windows-only behavior is required unless unavoidable.
- Do not add cloud backend layers unless there is a specific architectural reason.
- Do not implement major feature work without first checking the actual repo state.
- Keep the UI and backend responsibilities separated.
- Preserve the theme preset schema and existing UI contract where it already exists.

## References

- Backend architecture: [agent/AGENTS.md](agent/AGENTS.md)
- Frontend architecture: [lumine-ui/AGENTS.md](lumine-ui/AGENTS.md)

## What should an AI coding agent not break?

- The LiveKit worker entrypoint in [agent/agent.py](agent/agent.py)
- The persona file and its tone in [agent/prompts/persona.md](agent/prompts/persona.md)
- The Tauri + React layout in [lumine-ui/](lumine-ui/)
- The existing light/dark preset schema in [lumine-ui/src/pages/home/constants.ts](lumine-ui/src/pages/home/constants.ts)
- The desktop companion UI intent, even though it is still mock-driven today
- The product requirement that Lumine remains local-first and cross-platform

## Current verified reality

This repository is not yet a full companion desktop product. It is a strong backend prototype plus a styled UI prototype. The most important missing architectural step is the desktop orchestration layer that connects Tauri to the Python agent and eventually handles tray, wake words, lifecycle management, and state synchronization.
