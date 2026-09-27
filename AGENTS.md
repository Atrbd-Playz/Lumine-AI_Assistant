# Lumine repository instructions

## Scope and source of truth

- This repository has two independently runnable parts: `agent/` is the Python LiveKit worker; `lumine-ui/` is the React/Vite UI inside a Tauri/Rust shell. There is no root workspace or task runner.
- The root `package.json` has no app scripts. Run frontend/Tauri commands from `lumine-ui/`; the app entry is `src/main.tsx` → `src/App.tsx` → `src/pages/Home.tsx`.
- Treat manifests, environment examples, and current source as authoritative when prose conflicts. The root README is empty, and some checked-in `docs/`/nested `AGENTS.md` notes describe earlier scaffold states.

## Runtime shape

- `agent/agent.py` is the worker entrypoint. It loads the persona and `agent/.env` by file path, runs a persistent LiveKit `AgentServer` named `lumine`, and creates a new `AgentSession` for each dispatched room.
- The current React voice flow is in `lumine-ui/src/features/voice/voice-manager.ts`: make sure the Python worker is running, create a fresh room, request a token through Tauri, connect, enable the microphone, dispatch `lumine`, consume agent audio/transcripts/emotion/notice events, then remove listeners, stop media, disconnect, and delete the room.
- `lumine-ui/src-tauri/src/agent_manager.rs` implements child-process start/stop/restart and worker-readiness commands. The React voice path now calls `start_agent` and then `wait_for_agent_worker` before dispatching, so the app owns the worker's lifecycle and the worker reads the config the UI writes plus any keyring credentials. A readiness timeout is logged rather than fatal: a worker started outside Tauri is invisible to Rust and can still serve the room. When testing settings changes, stop any `lk agent dev` worker first — the two briefly coexist and LiveKit dispatches to only one of them.
- A separately started worker still reads settings saved from the UI. `config_store.config_path()` searches `LUMINE_CONFIG_PATH`, then the desktop app's local data directory, then `agent/lumine.config.json`; the desktop directory is what the Tauri side writes to. `validate_config.py --describe` reports `source`, `configPath`, and `searchedPaths`, and the worker emits `config_path` on `config_applied`/`config_rejected`, so a worker that is reading the wrong file says so instead of silently using `agent/.env`.
- Python worker stdout uses `LUMINE_EVENT <json>` records. Rust re-emits them as Tauri events; current event names use underscores (`agent_started`, `agent_state_changed`, `agent_stopped`, `agent_error`, `agent_runtime`), not dotted names.
- `ConversationPanel` stays backend-agnostic. `useConversation` has a `ConversationService` seam and currently defaults to the empty mock service; live voice messages enter through the callbacks wired in `Home.tsx`.

## Agent tools

- `agent/agent.py` passes `get_tools()` to the `Lumine` agent. The registry in `agent/tools/tools_registry.py` is the source of truth for registered tools; tool docstrings are the model-facing schema.
- Current tool IDs are `get_weather`, `get_news`, `search_web`, `recall_persona`, and `open_app`, registered cheapest-first. `LUMINE_DISABLED_TOOLS` accepts a comma-separated list of exact IDs; `LUMINE_ENABLE_APP_LAUNCH=false` disables the desktop-launch tool. Toggles are read after `agent/.env` loads.
- `agent/tools/tool_results.py` owns the one rule for what a tool may return: a central `MAX_RESULT_CHARS` ceiling, keyed JSON rather than prose, a truncation marker, and a delimiter around anything retrieved from the internet. New tools go through it — a tool result is re-sent on every later turn, so its size is paid for repeatedly.
- `agent/prompts/persona.md` is the **full** persona, read on demand by `recall_persona`. `persona_core.md` is what every request carries. The split is about when the persona is read, never what it says; `agent/tests/test_persona_split.py` asserts the full file survives.
- `agent/context_trim.py` trims the chat context to `LUMINE_MAX_CONTEXT_ITEMS` (default 40, floor 24) on `conversation_item_added`. It is a backstop — `ChatContext.truncate` preserves the system message, which is the persona and the tool policy.
- `agent/llm_errors.py` classifies a failed LLM call, and `agent/failure_gate.py` opens a circuit after three consecutive failures. Lumine speaks one short apology per opening. Do not add a retry loop: a rate-limited session that keeps trying spends the quota it has left.
- Notices reach the UI on the `lumine.notice` data channel, not only on stdout — a notice that depends on how the process was started is a notice that silently does not arrive.
- **Never let a telemetry write raise.** `runtime_events.write_event_line` catches `OSError`/`ValueError` for exactly this reason: the desktop app pipes the worker's stdout, and a write to a dead pipe raises `OSError: [Errno 22] Invalid argument` on Windows. That unguarded `print` was killing every session on its first line. `agent/broken_stdout_check.py` reproduces it.
- `agent.py` sets `logging.raiseExceptions = False`. LiveKit's logger fails on the same broken stream, and without this every log record prints a full traceback.
- The Rust side sets `LIVEKIT_LOG_LEVEL=info` on the child unless already set, because `agent.py dev` enables LiveKit's DEBUG level and a desktop app should not have to swallow that through an OS pipe.
- Do not insert a module-level `def` into the middle of a class body in these modules. It silently truncates the class — `RuntimeEventPublisher.emit` vanished that way and every session died on an `AttributeError` while all the writer's unit tests still passed. `test_stdout_resilience.PublisherSurfaceTests` guards it.
- Tool payloads are delivered to a toast via `onToolResult`, never through `ConversationToolEvent`. Keep it that way: a retrieved page rendered inline in the transcript reads as though the model had been handed it by the user, which is the shape a prompt injection wants.
- `GROQ_MODEL` defaults to `openai/gpt-oss-20b`; `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` are Enterprise-only as of August 2026 and are marked deprecated in the catalog. `GROQ_REASONING_EFFORT` defaults to `low`, and `max_completion_tokens` to 900 — GPT-OSS is a reasoning model, and thinking is drawn from the same budget, so a low cap produces an internal monologue and no audio.
- Keep network tools bounded to short, user-facing results and keep `open_app` shell-free and restricted to validated names. Tool tests exercise parsing/formatting/permissions locally; do not make the test suite depend on live external services.

## Setup and local commands

Use an existing repository `.venv` when possible. From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r agent\requirements.txt
```

Create ignored local config files from `agent/.env.example` and `lumine-ui/.env.example`, then fill in real values. The backend file needs the LiveKit URL/key/secret plus Groq and Cartesia credentials; `GROQ_MODEL` is optional and defaults to `openai/gpt-oss-20b`.

```powershell
# terminal 1: Tauri plus its Vite dev server. It starts the Python worker itself
# (start_agent + wait_for_agent_worker) and injects the keyring credentials.
Push-Location lumine-ui
npm ci                 # first checkout or dependency reset
npm run tauri dev
Pop-Location
```

To run the worker standalone instead — useful when debugging the worker alone —
use `.\.venv\Scripts\python.exe agent\agent.py dev` in a second terminal. Do not
leave one running while testing settings changes: the app cannot see it, and
LiveKit dispatches a room to a single agent, so the worker that answers may not be
the one holding your saved config.

`npm run tauri dev` starts Vite itself on strict port `1420`; do not start a second Vite server on that port. `npm run dev` is useful for the UI only, but Tauri `invoke`/microphone flows require the Tauri app. Tauri resolves Python in this order: `LUMINE_PYTHON_BIN`, `VIRTUAL_ENV`, the repository `.venv`, then `python`/`python3`. `LUMINE_AGENT_PATH` and `LUMINE_AGENT_ARGS` affect the Rust worker manager.

For focused LiveKit helper debugging, run `agent/livekit_token.py <room> <identity>`, `agent/livekit_dispatch.py <room> <agent_name>`, or `agent/livekit_room.py <room>` with the repository Python environment. These helpers load `agent/.env` themselves. `agent/multisession_check.py` dispatches three sequential sessions at one running worker and reports whether all three start — use it when a session joins once and then never again. `agent/broken_stdout_check.py` starts a worker whose stdout is a dead pipe and asserts the job still starts.

Both check scripts kill the process tree on exit. Stop any leftover `python agent.py dev` before running either: a worker left registered as `lumine` absorbs the next run's dispatches, which looks exactly like a broken worker.

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
