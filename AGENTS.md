# Lumine repository instructions

## Scope and source of truth

- This repository has two independently runnable parts: `agent/` is the Python LiveKit worker; `lumine-ui/` is the React/Vite UI inside a Tauri/Rust shell. There is no root workspace or task runner.
- The root `package.json` has no app scripts. Run frontend/Tauri commands from `lumine-ui/`; the app entry is `src/main.tsx` → `src/App.tsx` → `src/pages/Home.tsx`.
- Treat manifests, environment examples, and current source as authoritative when prose conflicts. The root README is empty, and some checked-in `docs/`/nested `AGENTS.md` notes describe earlier scaffold states.

## Runtime shape

- `agent/agent.py` is the worker entrypoint. It loads the persona and `agent/.env` by file path, runs a persistent LiveKit `AgentServer` named `lumine`, and creates a new `AgentSession` for each dispatched room.
- The current React voice flow is in `lumine-ui/src/features/voice/voice-manager.ts`: create a fresh room, request a token through Tauri, connect, enable the microphone, dispatch `lumine`, consume agent audio/transcripts/emotion events, then remove listeners, stop media, disconnect, and delete the room.
- `lumine-ui/src-tauri/src/agent_manager.rs` implements child-process start/stop/restart and worker-readiness commands, but the current React voice path never invokes those commands. For this checkout, run the Python worker separately before testing voice; do not assume the Tauri app auto-starts it.
- A separately started worker still reads settings saved from the UI. `config_store.config_path()` searches `LUMINE_CONFIG_PATH`, then the desktop app's local data directory, then `agent/lumine.config.json`; the desktop directory is what the Tauri side writes to. `validate_config.py --describe` reports `source`, `configPath`, and `searchedPaths`, and the worker emits `config_path` on `config_applied`/`config_rejected`, so a worker that is reading the wrong file says so instead of silently using `agent/.env`.
- Python worker stdout uses `LUMINE_EVENT <json>` records. Rust re-emits them as Tauri events; current event names use underscores (`agent_started`, `agent_state_changed`, `agent_stopped`, `agent_error`, `agent_runtime`), not dotted names.
- `ConversationPanel` stays backend-agnostic. `useConversation` has a `ConversationService` seam and currently defaults to the empty mock service; live voice messages enter through the callbacks wired in `Home.tsx`.

## Agent tools

- `agent/agent.py` passes `get_tools()` to the `Lumine` agent. The registry in `agent/tools/tools_registry.py` is the source of truth for registered tools; tool docstrings are the model-facing schema.
- Current tool IDs are `get_weather`, `search_web`, `get_news`, and `open_app`. `LUMINE_DISABLED_TOOLS` accepts a comma-separated list of exact IDs; `LUMINE_ENABLE_APP_LAUNCH=false` disables the desktop-launch tool. Toggles are read after `agent/.env` loads.
- Keep network tools bounded to short, user-facing results and keep `open_app` shell-free and restricted to validated names. Tool tests exercise parsing/formatting/permissions locally; do not make the test suite depend on live external services.

## Setup and local commands

Use an existing repository `.venv` when possible. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r agent\requirements.txt
```

Create ignored local config files from `agent/.env.example` and `lumine-ui/.env.example`, then fill in real values. The backend file needs the LiveKit URL/key/secret plus Groq and Cartesia credentials; `GROQ_MODEL` is optional and defaults to `openai/gpt-oss-20b`.

```powershell
# terminal 1: persistent LiveKit worker
.\.venv\Scripts\python.exe agent\agent.py dev

# terminal 2: Tauri plus its Vite dev server
Push-Location lumine-ui
npm ci                 # first checkout or dependency reset
npm run tauri dev
Pop-Location
```

`npm run tauri dev` starts Vite itself on strict port `1420`; do not start a second Vite server on that port. `npm run dev` is useful for the UI only, but Tauri `invoke`/microphone flows require the Tauri app. Tauri resolves Python in this order: `LUMINE_PYTHON_BIN`, `VIRTUAL_ENV`, the repository `.venv`, then `python`/`python3`. `LUMINE_AGENT_PATH` and `LUMINE_AGENT_ARGS` affect the Rust worker manager.

For focused LiveKit helper debugging, run `agent/livekit_token.py <room> <identity>`, `agent/livekit_dispatch.py <room> <agent_name>`, or `agent/livekit_room.py <room>` with the repository Python environment. These helpers load `agent/.env` themselves.

## Verification

- Run the Python suite from the repository root (the test imports `agent.emotion_contract` as a package):
  ```powershell
  .\.venv\Scripts\python.exe -m unittest discover -s agent\tests -p "test_*.py"
  ```
- Run one test with, for example:
  ```powershell
  .\.venv\Scripts\python.exe -m unittest agent.tests.test_emotion_contract.EmotionContractTests.test_normalize_emotion_keeps_canonical_values
  ```
- Build/typecheck the frontend with `npm run build` from `lumine-ui/` (`tsc && vite build`). There are no configured frontend test, lint, or standalone typecheck scripts.
- Check Rust with `cargo check` from `lumine-ui/src-tauri/`. `npm run tauri build` from `lumine-ui/` runs the frontend build first; `tauri.conf.json` currently has `bundle.active: false`, so do not assume an installer is produced.

## Environment and security boundaries

- The only frontend-safe LiveKit variable is `VITE_LIVEKIT_URL`; `VITE_LUMINE_DEBUG_EMOTION=true` enables frontend emotion diagnostics, while backend `LUMINE_DEBUG_EMOTION=true` enables agent-side emotion logging. Never put LiveKit, Groq, or Cartesia secrets in a `VITE_*` variable or commit `.env` files; Tauri invokes the Python helpers so credentials stay out of the browser bundle.
- Keep LiveKit/provider details behind the Tauri/`features/voice` integration boundary and presentation components backend-agnostic. Preserve the local-first goal and the persistent Python worker; do not replace the desktop app with a cloud-only service.
- Preserve the persona in `agent/prompts/persona.md`, the light/dark `AppearancePreset` contract in `lumine-ui/src/pages/home/constants.ts`, and the existing SVG-based avatar engine; these are product contracts, not incidental styling.

## Where to look next

- Agent/runtime contract: `agent/agent.py`, `agent/emotion_contract.py`, `agent/livekit_{token,dispatch,room}.py`.
- Desktop process/IPC contract: `lumine-ui/src-tauri/src/lib.rs` and `agent_manager.rs`.
- Voice/session lifecycle: `lumine-ui/src/features/voice/voice-manager.ts` and `useLumineVoice.ts`.
- Conversation integration seam: `lumine-ui/src/pages/home/conversation/`.
- `opencode.json` enables the LiveKit documentation MCP; consult current LiveKit docs when changing SDK integration. The nested `agent/AGENTS.md` and `lumine-ui/AGENTS.md` contain area-specific background but are not a substitute for the executable source.
