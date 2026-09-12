# Lumine Python Agent Architecture

## How does Lumine think, listen, and speak?

The Python backend is a LiveKit agent worker that starts an `AgentSession` with a VAD, STT, LLM, and TTS pipeline.

The current implementation is centered in [agent/agent.py](agent/agent.py). It does the following:

- loads the persona from [agent/prompts/persona.md](agent/prompts/persona.md)
- loads environment variables via `dotenv`
- connects to a LiveKit room
- starts an `AgentSession`
- runs a greeting generation on session start

This is a voice-first agent prototype, not yet a full desktop orchestrator.

## Verified current architecture

```python
session = AgentSession(
    vad=silero.VAD.load(min_speech_duration=0.4),
    stt=groq.STT(),
    llm=groq.LLM(
        model="llama-3.3-70b-versatile",
        temperature=0.7,
    ),
    tts=cartesia.TTS(
        model="sonic-2",
        voice="002622d8-19d0-4567-a16a-f99c7397c062",
        language="en",
        speed=0.95,
    ),
    min_endpointing_delay=0.4,
)
```

This is the actual verified implementation and should remain the baseline unless a future architectural change is explicitly approved.

## Agent lifecycle

Current lifecycle:

1. Python process starts.
2. `load_dotenv()` loads environment state if present.
3. `entrypoint(ctx)` connects to the LiveKit room.
4. `AgentSession.start()` begins voice processing.
5. `session.generate_reply()` sends a greeting.

This is a minimal worker lifecycle. It is not yet a desktop-sidecar lifecycle with explicit management commands, health checks, restart logic, or shutdown signals.

## VAD, STT, LLM, and TTS

### VAD

- Verified dependency: `livekit-plugins-silero`
- Verified runtime use: `silero.VAD.load(min_speech_duration=0.4)`
- Status: implemented and active

### STT

- Verified dependency: `livekit-plugins-groq`
- Verified runtime use: `groq.STT()`
- Status: implemented and active

### LLM

- Verified dependency: `livekit-plugins-groq`
- Verified runtime use: `groq.LLM(model="llama-3.3-70b-versatile", temperature=0.7)`
- Status: implemented and active

### TTS

- Verified dependency: `livekit.plugins.cartesia` via `cartesia.TTS`
- Verified runtime use: `model="sonic-2"`, `voice="002622d8-19d0-4567-a16a-f99c7397c062"`, `language="en"`, `speed=0.95`
- Status: implemented and active

## Persona

The personality is defined in [agent/prompts/persona.md](agent/prompts/persona.md) and includes the intended Lumine traits:

- sweet
- adorable
- intelligent
- caring
- supportive
- cheerful
- playful
- loyal
- emotionally aware
- not excessively formal

The persona also explicitly treats the user with respect and affection and uses "Master" when appropriate. This is a product voice decision and should not be randomly removed or replaced without intent.

## Configuration and environment variables

### Verified current state

- [agent/agent.py](agent/agent.py) calls `load_dotenv()`, so environment variables are expected to be loaded from a local environment file if present.
- [agent/requirements.txt](agent/requirements.txt) contains Python dependencies only; it does not define a complete environment schema.
- There are no committed `.env` files or `.env.example` files in the repository.

### Status

- [~] Environment configuration is partially implemented, but no repo-managed configuration contract is currently defined.
- [ ] There is no verified explicit list of required env vars in the repository.
- [ ] No secure packaging metadata for Python sidecar distribution is defined yet.

This should be treated as UNKNOWN until a configuration contract is introduced.

## Error handling and logging

Current verified implementation:

- `logging.basicConfig(level=logging.INFO)`
- `logger = logging.getLogger("lumine")`
- a startup log line prints the room name

Status:

- [~] Basic logging exists
- [ ] Structured startup/shutdown logging is not implemented
- [ ] Health checks, retry loops, and robust error metrics are not implemented
- [ ] Recovery logic for crashed or disconnected agent processes is not implemented

## Communication with Tauri

The Python agent does not currently communicate with the Tauri desktop app in a defined IPC protocol.

This is a major gap:

- `agent.py` connects to LiveKit directly and does not receive desktop lifecycle commands.
- There is no Rust-to-Python agent manager yet.
- There is no typed daemon protocol for state transitions such as sleeping, listening, thinking, speaking, or errors.

The intended future architecture is:

```text
Tauri app
  ↓
Rust agent manager
  ↓
Python sidecar process
  ↓
LiveKit worker / agent session
```

The key rule is that the UI should not know about Groq, Cartesia, or LiveKit internals. It should consume high-level application state instead.

## Packaging and platform concerns

The repository does not currently package the agent as a standalone executable or sidecar.

The desired future model is:

```text
Windows -> lumine-agent.exe
macOS   -> lumine-agent
Linux   -> lumine-agent
```

This should be bundled alongside the Tauri desktop app as a sidecar or managed child process, not as an end-user manual Python install step.

Platform-specific considerations that should be isolated later include:

- microphone permissions
- audio device selection
- OS startup behavior
- tray / app lifecycle integration
- wake-word detection
- crash recovery and auto-restart

## Backend roadmap

### Current state

- [x] Python agent exists
- [x] LiveKit integration exists
- [x] Groq STT exists
- [x] Groq LLM exists
- [x] Cartesia TTS exists
- [x] Silero VAD exists
- [x] Persona exists
- [~] dotenv-based configuration exists, but not fully specified
- [~] logging exists, but not production-grade

### Planned backend work

- [~] agent manager / lifecycle API
- [~] start / stop / restart commands
- [~] health/status monitoring
- [ ] structured config contract
- [ ] app-sidecar packaging
- [~] graceful shutdown handling
- [ ] crash recovery
- [ ] cross-platform binary packaging
- [~] explicit backend-to-front-end state events

The Python agent remains intact and is still the voice service. The Tauri layer is now responsible for the development lifecycle contract, but the full desktop runtime is still not yet build-verified because the Tauri shell remains blocked by the Windows resource issue.

## Constraints and guardrails

- Do not replace LiveKit with a cloud-only architecture.
- Do not remove the local agent concept.
- Do not assume Windows-only packaging is final.
- Do not hardcode secrets into source files.
- Do not introduce a cloud backend as a replacement for local desktop logic unless specifically approved.
- Do not make the frontend depend on LiveKit internals.

## Important questions that still need confirmation

- What is the exact runtime contract between Tauri and the Python agent?
- Which environment variables are required for the Python process in development and packaging?
- Is the Python agent expected to run as a sidecar process, a child process, or a separate bundled binary?
- Will the wake-word engine run inside Tauri, inside the Python agent, or in a dedicated native module?

## Summary

The backend is currently a LiveKit voice agent prototype that already uses VAD, STT, LLM, and TTS. The next step is not to rewrite the agent, but to turn it into a proper desktop-managed sidecar with explicit lifecycle control, structured state, and packaging for cross-platform deployment.
