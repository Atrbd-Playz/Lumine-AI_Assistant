# Lumine LiveKit integration

## Flow

The microphone control creates a unique `lumine-session-<uuid>` room and a temporary `user-<uuid>` identity. The Tauri command starts the existing LiveKit worker, then signs a ten-minute participant token by invoking `agent/livekit_token.py`. The signing script loads `agent/.env`; the token is never exposed through Vite or logged.

The React `useLumineSession` hook connects `livekit-client@2.22.3`, publishes the microphone, waits for a remote participant whose LiveKit kind is `AGENT`, and attaches that participant's audio tracks. Runtime verification on this machine uses the installed `livekit-agents==1.8.1` with Silero VAD, Groq STT/LLM, and Cartesia TTS. Its `generate_reply` greeting uses the same TTS pipeline as normal replies.

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
GROQ_API_KEY=...
CARTESIA_API_KEY=...
GROQ_MODEL=openai/gpt-oss-20b
```

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

- **Token generation fails:** check backend-only variables and that the selected Python environment has `livekit-agents` installed.
- **LiveKit URL missing:** set `VITE_LIVEKIT_URL` in `lumine-ui/.env` and restart Vite.
- **Agent timeout:** confirm the worker is registered with the same LiveKit project and that the microphone participant can join the generated room.
- **No audio:** grant microphone permission to the browser/Tauri WebView and allow playback from the microphone click gesture.
- **Tauri build issues:** run the browser flow with `npm run dev` first, then test `npm run tauri dev`; WebView permissions and autoplay behavior can differ.

The current prototype still relies on the LiveKit worker's normal dispatch behavior. A production package should bundle the Python worker and keep credentials in OS-managed configuration.
