# Lumine Python Agent Architecture

## How does Lumine think, listen, and speak?

The Python backend is a LiveKit agent worker. A **persistent** `AgentServer` registers
once, and each dispatched room gets its own `AgentSession` with a VAD, STT, LLM and TTS
pipeline.

The current implementation is centered in [agent/agent.py](agent/agent.py). It does the following:

- loads the always-sent persona from [agent/prompts/persona_core.md](agent/prompts/persona_core.md)
- loads environment variables via `dotenv`, unless a saved `lumine.config.json`
  validates and takes precedence
- starts the `AgentServer` and waits for dispatches
- per dispatched room, builds the session's pipeline from the job's profile
- runs a greeting generation on session start

The **full** persona at [agent/prompts/persona.md](agent/prompts/persona.md) is
read on demand by the `recall_persona` tool rather than prepended to every
request. It is unchanged on disk; the split is about token cost, not editorial
intent. See `docs/ai-control-center.md` for why.

The voice path is real. The desktop orchestration *around* it — tray, window
lifecycle, wake word, sidecar packaging — is not built yet.

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

The worker is **persistent**, not per-session. One process, one registration, many
rooms:

1. Python process starts.
2. `load_dotenv()` loads environment state if present.
3. A saved `lumine.config.json` takes precedence over the environment, if it
   validates. `config_applied` / `config_rejected` carries the path it used.
4. A persistent LiveKit `AgentServer` named `lumine` starts and registers.
5. For each **dispatched room**, a fresh `AgentSession` is built from the job's
   profile — stages, voice, toolset — and it opens and closes with the room.
6. `session.generate_reply()` sends the greeting.

Step 5 is why the profile can change between calls without a restart, and why
"the worker is running" is not the same statement as "Lumine is listening".

The desktop-sidecar lifecycle around this exists: `lumine-ui/src-tauri/src/agent_manager.rs`
provides idempotent start, stop, restart, and a readiness gate that blocks until
registration. There is still no explicit sleep/wake command and no crash
auto-restart.

## Video is not wired

`agent.py` has no video input. There is no `RoomInputOptions(video=...)` and no frame
handler, so nothing publishes camera or screenshare frames. The desktop camera control
is a **local preview only** — deliberately, because publishing nothing is also what
means the OS lights no recording indicator.

Until it is wired, `ModelDefinition.input_modalities` is what keeps the two honest:
it means "what Lumine can hand this model on the path it is reached by", and a
camera turned on beside a pipeline stage that declares no video input is offering
itself to nowhere. All four Gemini Live models declare
`(text, audio, image, video)`; `transport` declares none.

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

- [agent/agent.py](agent/agent.py) calls `load_dotenv()`, so environment variables are
  expected to be loaded from a local environment file if present.
- Precedence is **saved `lumine.config.json` > `agent/.env` > catalog defaults**,
  implemented in `agent/config_store.py`. `config_path()` searches
  `LUMINE_CONFIG_PATH`, then the desktop local-data directory, then
  `agent/lumine.config.json`.
- [agent/providers.py](agent/providers.py) is the schema. Every provider declares its
  credential variables as `KeySlot`s with a `kind` and a `label`, so the UI is handed
  one field per variable and holds no provider knowledge.
- `agent/.env.example` ships, and **every credential line in it is empty**. An empty
  line is a real absence; a `your-…` placeholder reads as a working key to every
  presence check in the product, which is the same class of bug as the
  credential-injection one.
- The default pipeline is `gemini_live`, so a working install needs **LiveKit's three
  values and `GOOGLE_API_KEY`**. Groq and Cartesia are only needed for
  `LUMINE_PIPELINE=legacy_cascade`.

### Status

- [x] A repo-managed configuration contract exists and is tested.
- [x] The required-variable list is derived from the catalog, so it cannot drift:
  `agent/tests/test_env_example.py` fails if the shipped file and the code disagree,
  and fails if a credential variable is deleted from the file rather than emptied.
- [ ] No secure packaging metadata for Python sidecar distribution is defined yet.
- [ ] Credential changes need a worker restart; the persistent worker does not re-read
  the keyring per job.

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
  credentials into its environment (`credential_injection.rs`), **one variable from
  its own slot**.
- `voice-manager.ts` calls `start_agent` then `wait_for_agent_worker` before
  dispatching a room, so voice no longer depends on a manually started process.
- `lumine-ui/src/lib/agentRuntime.ts` is subscribed on the React side, so a worker
  that fails between two status reads is no longer invisible.
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
- [x] LiveKit integration exists, and the worker is a persistent server rather than a
      process per room
- [x] Google STT and realtime Live models exist; Groq STT/LLM exist for the cascade
- [x] Cartesia TTS and Gemini TTS exist
- [x] Silero VAD exists
- [x] Persona exists, split into an always-sent core and an on-demand reference
- [x] A saved settings document takes precedence over `agent/.env`
- [x] Tool results are compressed, centrally capped, and injection-guarded
- [x] The chat context is trimmed to a budget
- [x] Failures are named, spoken, toasted, and gated by a circuit
- [x] Every provider's credential is slot-addressed, so a multi-variable provider such
      as LiveKit stores three values instead of one
- [x] Every provider has a connectivity probe that uses the credential it is
      actually given
- [x] Each model declares its input modalities, and validation refuses a pipeline that
      would send input nothing can read
- [x] A locally hosted server is supported as a provider (`ollama`), with a discovery
      action rather than an automatic list
- [x] The shipped `.env.example` agrees with the code, and that agreement is tested
- [~] dotenv-based configuration still works as a fallback, but is no longer the
      primary path
- [~] logging exists, but not production-grade

### Planned backend work

- [x] agent manager / lifecycle API
- [x] start / stop / restart commands, invoked by the voice path
- [x] health/status monitoring — a readiness gate in Rust, and a live event
      subscription in `lib/agentRuntime.ts`
- [x] structured config contract (`lumine.config.json` + `agent/validation.py`)
- [ ] app-sidecar packaging
- [~] graceful shutdown handling
- [ ] crash recovery
- [ ] cross-platform binary packaging
- [~] explicit backend-to-front-end state events — failures, tool status and the
      worker's lifecycle are structured and delivered; agent sleep/wake is not
- [ ] video input, which is what would make the camera control real

The Python agent remains intact and is still the voice service, and the Tauri layer
owns the development lifecycle contract. `npm run tauri build` now completes and writes
`src-tauri/target/release/lumine-ui.exe`, with the Windows icon and version resources
correctly embedded.

There was never a Windows resource icon problem, and this file claimed there was for a
while. The icons under `src-tauri/icons` are complete, git-tracked, and valid;
`tauri-winres` embeds `icon.ico` without complaint in both profiles. What actually
broke the release build was a poisoned `target/release` cache left by an interrupted
run, which made `core` resolve to a metadata stub and produced 270 cascading errors in
`jsonptr` — every one of them a "cannot find `Option`/`Result`/`From`" that looked like
a dependency fault and was not one. `cargo clean --release` is the fix, and it recurs
after any interrupted release build.

Because `bundle.active` is `false`, a successful build still produces no installer. That
is the packaging decision, not a failure.

## Constraints and guardrails

- Do not replace LiveKit with a cloud-only architecture.
- Do not remove the local agent concept. `local` stays **reserved** for inference whose
  weights ship *with the app*; `ollama` is the honest version of the same idea, a
  server the user started. Shipping the first under the second's name would be a
  promise the app cannot keep.
- Do not assume Windows-only packaging is final.
- Do not hardcode secrets into source files, **or into a template**. Every credential
  check in the product treats "non-empty string" as "you have this key", because
  telling a placeholder from a real key would cost a billable request. So a shipped
  `your-…` placeholder declares a credential nobody has. Ship it empty.
- Do not introduce a cloud backend as a replacement for local desktop logic unless specifically approved.
- Do not make the frontend depend on LiveKit internals.

## Important questions that still need confirmation

- Is the Python agent expected to run as a sidecar process, a child process, or a separate bundled binary?
- Will the wake-word engine run inside Tauri, inside the Python agent, or in a dedicated native module?
- What should the sleep/wake contract look like — a command, or a room that carries no audio?

Answered since this was written: the runtime contract is `LUMINE_EVENT` on stdout plus
the `agent_*` events Rust re-emits; and the required variables are derived from the
catalog, so `agent/tests/test_env_example.py` is the authority rather than this file.

## Summary

The backend is a working LiveKit voice agent: a persistent worker, per-room sessions,
VAD, STT, LLM and TTS, a failure path that names its cause, and a catalog that is the
single source of truth for what can be configured. The next step is not to rewrite the
agent, but to package it as a real sidecar and finish the desktop shell around it.

`../docs/ai-control-center.md` is the decision record, and `agent/providers.py` is
the executable authority. Where the three disagree, the code wins.
