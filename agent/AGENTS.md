# Lumine Python Agent Architecture

## How does Lumine think, listen, and speak?

The Python backend is a LiveKit agent worker that starts an `AgentSession` with a VAD, STT, LLM, and TTS pipeline.

The current implementation is centered in [agent/agent.py](agent/agent.py). It does the following:

- loads the always-sent persona from [agent/prompts/persona_core.md](agent/prompts/persona_core.md)
- loads environment variables via `dotenv`
- connects to a LiveKit room
- starts an `AgentSession`
- runs a greeting generation on session start

The **full** persona at [agent/prompts/persona.md](agent/prompts/persona.md) is
read on demand by the `recall_persona` tool rather than prepended to every
request. It is unchanged on disk; the split is about token cost, not editorial
intent. See `docs/ai-control-center.md` for why.

This is a voice-first agent prototype, not yet a full desktop orchestrator.

## Verified current architecture

The session components are no longer a single hardcoded block in `agent.py`. They
are built per job by `agent/pipeline_factory.py`, driven by a **voice profile**:
the saved `lumine.config.json` if one exists, otherwise the environment-derived
profile. The two stacks look like this.

Native-audio realtime (the environment default):

```python
llm = google.RealtimeModel(
    model="gemini-3.8-live",
    voice="Sulafat",
)
```

Separate-stage pipeline (the legacy cascade, still selectable):

```python
session = AgentSession(
    vad=silero.VAD.load(min_speech_duration=0.4),
    stt=groq.STT(),
    llm=groq.LLM(
        model="openai/gpt-oss-20b",
        temperature=0.7,
        max_completion_tokens=900,
        reasoning_effort="low",
    ),
    tts=cartesia.TTS(
        model="sonic-3",
        voice="002622d8-19d0-4567-a16a-f99c7397c062",
        language="en",
        speed=0.95,
    ),
    min_endpointing_delay=0.4,
)
```

This is the actual verified implementation and should remain the baseline unless a future architectural change is explicitly approved.

Note the two LLM arguments. GPT-OSS is a **reasoning** model and its thinking is
drawn from `max_completion_tokens`, so a cap that is generous enough to think with
leaves nothing to say with — the turn produces an internal monologue and no audio.
`reasoning_effort="low"` is deliberate for the same reason: a voice turn should not
pay latency and tokens for reasoning the user will never hear.

The values above are the **defaults**, not hardcoding. Every one of them is
declared per model in `agent/providers.py` and overridable per profile, and
`agent/validation.py` refuses a combination the runtime would reject. The factory
itself has no provider branches.

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

- Verified dependency: `livekit-plugins-groq`, `livekit-plugins-google`
- Verified runtime use: `groq.STT()`, or `google.STT()` — which is non-streaming and
  therefore requires a VAD
- Status: implemented and active, selectable per profile

### LLM

- Verified dependencies: `livekit-plugins-groq`, `livekit-plugins-google`, `livekit-plugins-openai`
- Verified runtime use: `groq.LLM(model="openai/gpt-oss-20b", temperature=0.7)`, or
  `google.RealtimeModel(model="gemini-3.8-live", ...)` for a realtime stack
- Status: implemented and active, selectable per profile

### Realtime

- Verified dependency: `livekit-plugins-google`
- Verified runtime use: `google.RealtimeModel(...)` with `gemini-3.8-live`,
  `gemini-3.8-live-extended-thinking`, or a deprecated `gemini-3.1-flash-live-preview`
- Status: implemented and active. All Gemini Live models are native-audio, so a
  realtime profile cannot also use a separate TTS; validation blocks that pairing.
- The catalog in `agent/providers.py` is checked against the plugin's own
  `KNOWN_GEMINI_API_MODELS` by a test, because the plugin rejects a model id it
  does not recognise.

### TTS

- Verified dependency: `livekit.plugins.cartesia` via `cartesia.TTS`
- Verified runtime use: `model="sonic-3"`, `voice="002622d8-19d0-4567-a16a-f99c7397c062"`, `language="en"`, `speed=0.95`
- Status: implemented and active. `sonic-2` retires 2026-10-20 and is no longer the
  default; the model is overridable per stage.

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

It is sent in two parts, and both are load-bearing:

- `prompts/persona_core.md` — the traits above, condensed. Prepended to every
  request. `test_persona_split.py` fails if it grows past half the original size.
- `prompts/persona.md` — the full text, unchanged. Returned by the `recall_persona`
  tool when a turn needs worked examples, vocabulary, or edge-case guidance.

The full file must stay on disk intact. If it is ever "tidied" to match the core,
the recall tool has nothing left to recall and the optimisation has silently become
a deletion of the persona's detail.

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
- `agent/llm_errors.py` classifies a failed LLM call into `rate_limited`,
  `quota_exhausted`, `context_length`, `auth`, `provider_down`, or `unknown`
- `agent/failure_gate.py` counts consecutive failures, opens a circuit after
  three, and speaks one short apology per opening
- classified failures are published on the `lumine.notice` data channel so the
  desktop app can show a toast

The classification exists because a failure the user cannot perceive is
indistinguishable from a broken microphone. Two rules are worth preserving:

- **A 400 is never reported as a quota problem**, whatever the body claims. Google
  answers an invalid key with 400 and a message, so believing the body would blame
  the user's quota for our own malformed request.
- **`rate_limited` and `quota_exhausted` are both HTTP 429** and are separated only
  by the message. "Try again shortly" is wrong advice when the day's allowance is
  gone.

There is deliberately **no retry loop**. A session that has hit a rate limit and
keeps trying spends the quota it has left and recovers more slowly. Backing off and
telling the user is the useful behaviour.

Status:

- [~] Basic logging exists, plus named failure classification and a failure circuit
- [x] The user is told when a turn fails — spoken and toasted
- [~] Structured startup/shutdown logging is not implemented
- [~] Health checks exist as the Tauri worker's readiness gate
- [ ] Robust error metrics are not implemented
- [ ] Recovery logic for crashed or disconnected agent processes is not implemented

## Communication with Tauri

The Python agent communicates with the Tauri desktop app through the Rust agent
manager, plus a `LUMINE_EVENT <json>` stream on stdout that Rust re-emits as Tauri
events.

The agent manager now exists and the desktop app drives it:

- `lumine-ui/src-tauri/src/agent_manager.rs` owns the child process. `start_agent`
  is idempotent, points the child at the app's config path, and injects keyring
  credentials into its environment (`credential_injection.rs`).
- `voice-manager.ts` calls `start_agent` then `wait_for_agent_worker` before
  dispatching a room, so voice no longer depends on a manually started process.
- There is still no typed daemon protocol for state transitions such as sleeping or
  waking; LiveKit dispatch metadata plus the `LUMINE_EVENT` stream remain the seam.

The architecture is:

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
- [x] Persona exists, split into an always-sent core and an on-demand reference
- [x] A saved settings document takes precedence over `agent/.env`
- [x] Tool results are compressed, centrally capped, and injection-guarded
- [x] The chat context is trimmed to a budget
- [x] Failures are named, spoken, toasted, and gated by a circuit
- [~] dotenv-based configuration still works as a fallback, but is no longer the
      primary path
- [~] logging exists, but not production-grade

### Planned backend work

- [x] agent manager / lifecycle API
- [x] start / stop / restart commands, invoked by the voice path
- [~] health/status monitoring
- [x] structured config contract (`lumine.config.json` + `agent/validation.py`)
- [ ] app-sidecar packaging
- [~] graceful shutdown handling
- [ ] crash recovery
- [ ] cross-platform binary packaging
- [~] explicit backend-to-front-end state events — failures and tool status are
      structured and delivered; agent sleep/wake is not

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
