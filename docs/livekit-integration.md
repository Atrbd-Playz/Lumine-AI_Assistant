# Lumine LiveKit integration

## Flow

The microphone control creates a unique session UUID, `lumine-session-<uuid>` room, and matching `user-<uuid>` identity. Tauri starts the persistent worker once during application setup and waits for its installed Agents `worker_registered` event before sessions begin. The worker executable is resolved from the repository `.venv` before PATH, and `agent.py` loads `agent/.env` by file location rather than process working directory. This matches the verified manual worker environment.

After the browser connects to the fresh room, Tauri invokes `agent/livekit_dispatch.py`, which calls the installed `LiveKitAPI.agent_dispatch.create_dispatch` service with agent name `lumine` and that exact room. The signing and dispatch scripts load `agent/.env`; secrets are never exposed through Vite or logged.

The React voice manager connects `livekit-client@2.22.3`, publishes the microphone, waits for a remote participant whose LiveKit kind is `AGENT`, and attaches that participant's audio tracks. The default worker profile is native Gemini Live (`gemini-3.1-flash-live-preview`); setting `LUMINE_PIPELINE=legacy_cascade` retains the Silero VAD, Groq STT/LLM, and Cartesia TTS pipeline. The settings dialog persists the interruption choice and sends it as dispatch metadata (`barge_in` or `finish_response`); the choice is applied when the next voice room is created. Tool lifecycle records are published on the reliable `lumine.tool` data topic and are also emitted as `LUMINE_EVENT` records for the desktop runtime.

LiveKit transcription events populate the existing conversation panel. Stable segment IDs update interim messages instead of creating duplicate rows. Audio and listeners are detached on disconnect, unmount, timeout, or failed connection.

## Environment

Frontend-safe `lumine-ui/.env`:

```text
VITE_LIVEKIT_URL=wss://your-project.livekit.cloud
```

Backend-only `agent/.env`:

```text
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
LUMINE_PIPELINE=gemini_live
GOOGLE_API_KEY=...
GEMINI_MODEL=gemini-3.1-flash-live-preview
GEMINI_MAX_OUTPUT_TOKENS=1024
# Used only when dispatch metadata is absent (for example, manual dispatches).
LUMINE_INTERRUPTION_MODE=barge_in
```

For the preserved legacy profile, also set `GROQ_API_KEY`, `CARTESIA_API_KEY`, and `GROQ_MODEL`, then use `LUMINE_PIPELINE=legacy_cascade`.

Never use `VITE_` for a secret. Rotate any credentials that have been exposed outside the local environment.

There is no `VITE_LIVEKIT_KEY`. The only frontend variable is `VITE_LIVEKIT_URL`. The Tauri `get_livekit_token` command invokes `agent/livekit_token.py` and keeps `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` out of the Vite bundle.

## Run locally

Install frontend dependencies:

```powershell
Push-Location lumine-ui
npm install
Pop-Location
```

Start the Vite frontend:

```powershell
Push-Location lumine-ui
npm run dev
Pop-Location
```

Start the Python agent manually for worker debugging:

```powershell
.\.venv\Scripts\python.exe agent\agent.py dev
```

Start the Tauri app. It starts the worker automatically when the microphone is pressed:

```powershell
Push-Location lumine-ui
npm run tauri dev
Pop-Location
```

Set `LUMINE_PYTHON_BIN` when Tauri should use a specific Python executable. Otherwise it uses `VIRTUAL_ENV`, the repository `.venv`, or `python`.

## Common failures

- **Gemini startup fails:** check `GOOGLE_API_KEY`, `GEMINI_MODEL`, and that `livekit-plugins-google>=1.8.2` is installed.
- **Tool result missing from the UI:** confirm the worker publishes `tool_status` records on `lumine.tool`; tool failures should not disconnect the voice session.
- **Token generation fails:** check backend-only variables and that the selected Python environment has `livekit-agents` installed.
- **LiveKit URL missing:** set `VITE_LIVEKIT_URL` in `lumine-ui/.env` and restart Vite.
- **Agent timeout:** confirm the worker is registered with the same LiveKit project and that the microphone participant can join the generated room.
- **Replies stop mid-sentence:** compare the active interruption mode in the worker log with the settings dialog. `finish_response` uses Gemini `NO_INTERRUPTION` (or disables legacy interruption); `barge_in` permits provider VAD to cancel a response. The worker also emits `speech_finished`, `false_interruption`, `session_usage`, and `response_token_limit` diagnostics. Check those `LUMINE_EVENT` records and the browser console for `Audio attached`, `ended`, and `play_rejected` events before changing the token limit.
- **No audio:** grant microphone permission to the browser/Tauri WebView and allow playback from the microphone click gesture.
- **Tauri build issues:** run the browser flow with `npm run dev` first, then test `npm run tauri dev`; WebView permissions and autoplay behavior can differ.

Each connection is independent: the room, participant identity, session ID, and dispatch are newly generated. Disconnect increments a generation counter, removes listeners, stops microphone publishing, detaches remote audio, disconnects the room, and asks the backend to delete that room. On the agent side, the current job receives `ctx.shutdown()` when its last non-agent participant leaves; its shutdown callback calls `session.aclose()`. Neither operation stops the registered worker. A production package should bundle the Python worker and keep credentials in OS-managed configuration.

The first-click failure was caused by two Tauri-only startup defects: dotted event names such as `agent.started` are rejected by Tauri 2, causing `start_agent` to return an error after spawning the child; and Tauri previously resolved plain system `python`, whose installed Agents environment lacked the Cartesia plugin. The next click then reused the already-spawned child, making the failure appear intermittent.

## Session lifecycle

1. Generate session UUID, room name, and participant identity.
2. Start or reuse the worker process.
3. Sign a room-scoped user token and connect the client.
4. Explicitly dispatch the `lumine` agent to that room.
5. Wait for the agent participant and attach only agent audio tracks.
6. On end, invalidate the generation and perform idempotent cleanup.

The worker process is a dispatcher worker, not the session itself. Its health does not imply that an agent is attached to the current room. LiveKit documents that the worker remains registered while jobs are created and ended independently; a room closes automatically when its last non-agent participant leaves, or can be explicitly deleted.

Worker readiness is distinct from process existence: the Tauri manager waits up to 20 seconds for the registration callback and reports `ready` only after LiveKit acknowledges the worker. Session stages have independent token, connection, dispatch, agent-join, and audio-track timeouts.
