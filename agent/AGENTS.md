# Lumine Python Agent Architecture

## The worker's place in the companion

The worker's long-term shape is set by the product vision, not only by LiveKit's
API. Read [`../VISION.md`](../VISION.md) and [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
before changing anything about how the worker starts, stops, or reports itself.

The load-bearing consequences for this directory:

- **The worker stays a separate process, and its lifecycle is not the product's
  lifecycle.** The app currently starts and stops it, and Phase 1 inverts that:
  a resident runtime will own the wake listener and start the worker only when
  someone addresses Lumine. The persistent-server design here is correct for that
  and must not be replaced by a per-room process.
- **The worker is never the wake detector.** Wake is local, continuous and lives
  outside this process ([ADR-0003](../docs/decisions/0003-wake-layer-separation.md)).
  Nothing in `agent/` may import or configure a wake engine, and the microphone is
  *not* opened here — audio arrives over the room.
- **The greeting becomes conditional.** `generate_reply` on every session start is
  correct for a room the user opened, and wrong for a wake that carried a request
  ([ADR-0013](../docs/decisions/0013-greeting-conditional.md)).
- **The agent publishes a companion lifecycle**, it does not infer one. One record,
  one authority, every surface rendering the same thing
  ([ADR-0005](../docs/decisions/0005-single-lifecycle-authority.md)).
- **The product speaks, not the model.** A line from `runtime/voice_line.py` is
  Lumine speaking and must read like her; the WhatsApp send confirmation is the
  existing example of a fact the product states once.

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

## Directory layout

The module list was a flat folder of thirty files, so the ones that answer a
common question now live together. Two constraints shaped the split, and both are
load-bearing:

- **Everything Tauri runs by filename stays at `agent/` root.** `lib.rs`,
  `credential_probe.rs` and `setup_status.rs` call
  `python_env::run_agent_script("<name>.py")`, which resolves against the agent
  directory. Moving `validate_config.py`, `provider_catalog.py`, `tool_catalog.py`,
  `voice_preview.py`, `local_models.py` or `provider_probe.py` would break the
  settings, providers and voice screens at runtime, not at build time.
- **A module is loaded two ways**, so each import is a `try: from .x import` /
  `except ImportError: from x import` pair. Through a subpackage the relative
  branch is the live one; as a bare top-level module — `python agent/agent.py`,
  with `agent/` on `sys.path` — the fallback is. `tests/test_import_shapes.py`
  exercises both, and its module list names regrouped modules *through* their
  subpackage (`settings.config_store`, not `config_store`).

```text
agent/
├── agent.py                  worker entrypoint: AgentServer, per-room sessions
├── settings/                 what a profile says, and whether it is valid
│   ├── config_store.py       precedence: saved document > agent/.env > defaults
│   ├── validation.py         pure rules over plain dicts -> diagnostics
│   ├── providers.py          the catalog: providers, models, voices, capabilities
│   ├── llm_config.py         Groq/cascade budget and retry settings
│   ├── pipeline_config.py    env-backed pipeline settings
│   └── session_preferences.py  dispatch metadata -> per-job preferences
├── pipeline/
│   └── pipeline_factory.py   capability-driven stage builders
├── runtime/                  behaviour that only matters during a live session
│   ├── runtime_events.py     LUMINE_EVENT records and the tool-lifecycle bridge
│   ├── progress.py           a tool narrating its own steps while it works
│   ├── history_consent.py    the one question about reading somebody's messages
│   ├── failure_gate.py       consecutive-failure circuit and the spoken apology
│   ├── llm_errors.py         status + message -> a named, sayable failure
│   ├── context_trim.py       chat context trimmed to an item budget
│   ├── latency.py            turn latency recorder
│   ├── emotion_contract.py   canonical emotion vocabulary and extraction
│   ├── emotion_voice.py      the same signal as a per-turn TTS control
│   ├── verification.py       requested/dispatched/accepted/observed/failed/…/uncertain
│   ├── router.py             which environment a request belongs in (pure)
│   ├── task_model.py         Task/Step, dependencies, retry policy, cancellation
│   └── task_engine.py        deterministic progression, checkpoints, concurrency
├── tools/                    model-facing tools (registry is the source of truth)
│   ├── task_runner.py        run_task + cancel_task: route, plan, execute, report
│   ├── app_resolver.py       a spoken name -> one target, then verify the launch
│   ├── desktop/              the mouse and keyboard, for every other application
│   │   ├── control.py        desktop_control: the tool, and the refusals in front of it
│   │   ├── ops.py            the operation table: class, tier, verify, retry hold
│   │   ├── capture.py        what is on screen, and the generation token for it
│   │   ├── input.py          SendInput: move, click, drag, scroll, type, hotkey
│   │   └── uia.py            the UI Automation harness -- shared with WhatsApp
│   └── messaging/            the WhatsApp surface
│       ├── whatsapp.py       the send tool, and the resolve/draft/ask/send order
│       ├── whatsapp_ops.py   the operation table: class, paths, verify, retry hold
│       ├── whatsapp_web.py   WhatsApp Web, in a tab of its own -- reads and verbs
│       ├── whatsapp_desktop.py  UI Automation -- UNREACHABLE, kept as the record
│       ├── whatsapp_read.py  chats, unread, messages, find, contact
│       ├── whatsapp_chats.py archive, pin, mute, read, star, delete, block
│       ├── whatsapp_account.py presence, linked devices, about, unlink, call
│       ├── whatsapp_refs.py  ChatRef/MessageRef, and the freshness guarding them
│       ├── whatsapp_files.py the allowlist a model-supplied attachment path must pass
│       └── whatsapp_flow.py  contact search and chat selection -- see its docstring
│   └── media/                 the music library: one folder, and what plays from it
│       ├── control.py         play_media: find, play, next, previous, stop, folders
│       ├── library.py         the allowlist a model-supplied path must pass
│       ├── match.py           what was *said* versus what the file is *called*
│       ├── scanner.py         what exists, in natural order, folders as playlists
│       ├── player.py          the Player seam; SystemPlayer is os.startfile
│       ├── queue.py           one ordered .m3u8 for a folder, so a queue is not nine launches
│       └── state.py           the last track, so "the next one" means something
├── prompts/                  persona_core.md (always sent) and persona.md (recall)
├── tests/                    the suite; discovers test_*.py from the repo root
├── validate_config.py        CLI -- describe and validate a configuration
├── provider_catalog.py       CLI -- redacted provider catalog
├── media_probe.py            CLI -- what is in a music folder, for the Settings screen
├── media_search.py           CLI -- rank a library against a typed query, for the search box
├── media_check.py            check -- every music verb, against a real folder, opening nothing
├── media_rank.py             report -- how each filename is read, and what each request would do
├── media_queue.py            queue -- the folder's tracks as ONE ordered playlist, for the widget
├── whatsapp_probe.py         CLI -- which WhatsApp selectors are live right now
├── whatsapp_message_probe.py CLI -- the send button and the bubbles, in a live chat
├── desktop_probe.py          CLI -- what the current window exposes, and how
├── desktop_smoke.py          CLI -- end-to-end checks, staged so nothing moves until asked
├── tool_catalog.py           CLI -- tool registry for the Tools page
├── voice_preview.py          CLI -- speak one phrase with a candidate TTS
├── local_models.py           CLI -- ask a local server which models it has
├── provider_probe.py         CLI -- one cheap authenticated request per provider
├── livekit_token.py          helper -- mint a participant token
├── livekit_dispatch.py       helper -- dispatch an agent to a room
├── livekit_room.py           helper -- inspect or delete a room
├── multisession_check.py     check -- three sequential sessions at one worker
└── broken_stdout_check.py    check -- a dead stdout must not kill the job
```

The grouping is a reading order, not a layering rule: `settings` resolves a
profile, `pipeline` builds stages from it, `runtime` behaves while it runs.

## Verified current architecture

The session components are no longer a single hardcoded block in `agent.py`. They
are built per job by `agent/pipeline/pipeline_factory.py`, driven by a **voice profile**:
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
declared per model in `agent/settings/providers.py` and overridable per profile, and
`agent/settings/validation.py` refuses a combination the runtime would reject. The factory
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

## Video input

The agent receives video frames. `agent.py` passes `room_options=components.room_options`
to `session.start`, and the factory derives that option from the catalog rather than
from configuration.

This section used to say the opposite, and it was wrong in a way that would have
produced a larger and broken implementation. It described `RoomInputOptions(video=...)`
and a "frame handler". On the pinned 1.8.3:

- `RoomInputOptions` is **deprecated** — it logs "use RoomOptions instead" on every
  session — and is no longer importable from `voice.agent_session`. `RoomOptions`
  lives in `livekit.agents.voice.room_io`. It belongs on `session.start()`, not on
  `AgentSession()`.
- **There is no frame handler to write.** `RoomOptions(video_input=True)` plus the
  built-in sampler is the whole agent-side feature. `AgentSession(video_sampler=...)`
  takes a `_VideoSampler` protocol — `__call__(frame, session) -> bool` — and the
  default is `VoiceActivityVideoSampler(speaking_fps=1.0, silent_fps=0.3)`.

The rule that matters, and LiveKit documents it exactly: *"Enabling `video_input`
with an audio-only realtime model silently ignores the video frames — no error is
raised but the model won't process video."* So `video_input` is derived from
`ModelDefinition.input_modalities` via `_stage_sees_frames` and can never be a
constant. All four Gemini Live models declare `(text, audio, image, video)`;
`transport` declares none; the cascade's LLM stage declares `("text",)`.

`PipelineComponents.room_options` is **always** a real `RoomOptions`, never `None`,
because `session.start` treats a supplied-but-wrong type as an error and raises.
"Off" has to be an options object that says off.

Frame rate is declared explicitly in `_build_realtime_session` rather than
inherited. The numbers match the library default, so nothing changes — but Gemini
tokenizes every frame by its dimensions, so a rate nobody chose is a rate nobody can
reason about when the bill arrives. Video is opt-in from the frontend, which is what
keeps the cost at zero when nobody is sharing.

The cascade path does **not** subscribe. LiveKit can inject the latest frame into a
chat conversation as an image message on each user turn, and that genuinely works,
but its LLM stage is handed a history of strings and declares `("text",)`. Turning
the room subscription on before that is true would let a profile validate and then
quietly do nothing with every frame.

macOS cannot screenshare at all (WKWebView has no `getDisplayMedia`), which the
frontend feature-detects and reports as a platform problem rather than a model one.
Camera is unaffected.

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
- The catalog in `agent/settings/providers.py` is checked against the plugin's own
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
  implemented in `agent/settings/config_store.py`. `config_path()` searches
  `LUMINE_CONFIG_PATH`, then the desktop local-data directory, then
  `agent/lumine.config.json`.
- [agent/settings/providers.py](agent/settings/providers.py) is the schema. Every provider declares its
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
- `agent/runtime/llm_errors.py` classifies a failed LLM call into `rate_limited`,
  `quota_exhausted`, `context_length`, `auth`, `provider_down`, or `unknown`
- `agent/runtime/failure_gate.py` counts consecutive failures, opens a circuit after
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
- [x] Video input, for a model that declares image input. The subscription is derived
      from the catalog and cannot be a constant, because LiveKit silently discards
      frames a model cannot read
- [x] The persona knows Lumine can see, and `recall_persona` can reach the guidance
      for the questions that actually arrive
- [x] WhatsApp reads, not just sends — chats, unread, a chat's messages, message search,
      and a contact's about text. It is the first tool that shows the user what *other
      people wrote*, so it carries its own consent: `messaging.whatsapp.history` is
      `off`/`ask`/`auto`, defaults to `ask`, asks **before** the chat is opened, and the
      grant is released with the session. History results use a tighter budget than
      `tool_results.MAX_RESULT_CHARS` because they are third-party text paid for on every
      later turn
- [x] WhatsApp chat management and account verbs — archive, pin, mute, mark read,
      star, delete-for-everyone, block, presence, linked devices, unlink, calls
- [x] Every WhatsApp operation declares how it proves it happened, in one table that
      the permission floor, the Tools-page badge and the path chooser all read
- [x] A send types the message and reads it back out of the composer **before** asking,
      so the confirmation is about something on the user's own screen; a declined card
      clears the draft so one "no" does not break the next send
- [x] Long operations narrate their steps instead of going quiet, and the WhatsApp tab
      is pre-warmed during the greeting — **in the background, with no window on the
      screen.** `prewarm_whatsapp` passes `focus=False`, so nothing is brought forward
      and a browser it has to start comes up minimized over CDP. `--start-minimized`
      is inert here (measured: a headed launch with it came up `normal` every time),
      and creating a tab re-raises the window (measured), so the WhatsApp tab hides it
      again after it is created. `_reveal` is the only thing that un-hides it, on
      `open_page`, `switch_tab` and the send flow. A browser we *joined* is never
      hidden. The latency win is kept; the popup is gone.
- [x] A warm-up never hides a window the user already had open. `_hid_window`
      answers "may I hide this?" and is set only when *this process* launched the
      browser and put it away; `_reveal` clears it. The first version asked the
      wrong question and hid whatever it found — measured with a video playing in
      it, `whatsapp_page(focus=False)` on an already-visible browser left the
      window MINIMIZED, which is worse than the popup it was written to remove
- [x] A tab request actually reaches the browser. `WINDOW_VERBS` contains
      `switch`, so "switch to the other tab" was routed to `manage_window` on the
      desktop, which has no notion of a tab; `TAB_WORDS` returns a `tab` family
      first, and `Route.action` carries the verb so the model is told `tab` rather
      than left to guess `open` — the destructive one. Messaging still outranks it
- [x] Every read says which tab it read (`session.tab_context()` on `observe`,
      `read`, `state`, `open`), capped at `MAX_REPORTED_TABS`. Asking costs a turn
      she does not have; and `open` says when it replaced a page, naming `tab new`
      as the way not to
- [x] Both browser tools offer the same tab control — `web_control` gained `tab`,
      and its page-tab verb is `pagetabs` with `tabs` kept as an alias. One word
      meant two things; the collision is removed rather than moved
- [x] Tabs understand **how people say it**, and that was the layer that was broken.
      `actions.parse_tab_request` finds the operation and the target anywhere in a
      sentence. Measured against real phrasings, the first version — which wanted an
      exact word — refused "delete the 1st tab", "delete the first tab", "close the
      first tab", "tab 2", "close tab 2", "switch to tab 3", "what tabs", "how many
      tabs", "next tab", "close this tab" and "remove tab 2". Three were silently
      wrong rather than refused: "open a new tab" became a new tab *addressed to the
      string* "a new tab", and navigated to `https://a new tab`. A refusal there is
      not a dead end — the model retries, and the retry is where a second approval
      card comes from
- [x] **The one refusal is a close that named a tab it cannot resolve** ("1.5",
      "one"): reading those as "the tab in use" destroys a page nobody mentioned,
      which is the reported failure itself. `last` is its own marker, not `-1`,
      because *the last tab* is a specific tab and *the previous one* is a direction;
      `+1`/`-1` resolve against the registry
- [x] **Switching a tab does not ask.** `TAB_ACTION_CLASS` makes `list`, `switch` and
      `new` reads and only `close`/`close_others` acts, on the same test `browse`
      uses everywhere: does the act change anything *off* the screen? Switching
      changes which of the pages the agent already opened is in front, exactly as
      `scroll` and `back` do. The table lives in `actions.py` and both browser tools
      import it, so they cannot price one request differently
- [x] **Both browser tools ask with the same verb**, which is what stops two cards
      for one request. `permission_gate` collapses pending cards by (tool, verb,
      summary) and cannot collapse two that name themselves differently;
      `web_control` used to say `switchtab` where `browse` said `tab`
- [x] `close_others` is its own operation, and tab numbers identify a tab rather than
      counting the list — `close 1` leaves 2 and 3, so a stale number cannot come to
      mean a different page
- [x] `_window_args()` disables `CalculateNativeWinOcclusion`. Chromium asks the
      window server whether a window is fully covered and answers yes for one
      behind the Lumine app; occluded windows get throttling the timer flags do
      not cover. Playwright already passes the timer, occluded-window and
      renderer backgrounding switches — read out of the driver's own switch list —
      but nothing about native occlusion
- [x] The browser has **tabs**, and each keeps the references it was read with.
      `_Browser._tabs` holds every page the model may look at; the old rule adopted a
      new page and closed the previous one, so a popup was a *replacement* and "open
      this in a new tab" was impossible. One verb, `browse tab`, takes its operation in
      `text` — nothing to list, `new`, a number, `close` — parsed purely by
      `actions.parse_tab_request`, the same shape `scroll` uses. The load-bearing part
      is `_Tab.generation`: a shared counter would invalidate the first tab's
      references the moment a second tab opened, and switching deliberately mints
      nothing so "read tab 1, switch away, come back, act" works. `MAX_WORK_TABS` is
      6, refused rather than silently reshaped. `web_control`'s `tabs` verb is a
      different thing — a form's own tab strip — and the two are not interchangeable
- [x] Creating a browser tab no longer destroys the user's page. `context.new_page()`
      raises Playwright's `page` event *before* it returns, so `_on_page` saw the
      WhatsApp tab as an unregistered popup, adopted it as the working page, and closed
      the page the user had open; `_reserve` reclassifies it after the page object
      exists, which cannot miss. Closing the last tab opens a blank one on the live
      context rather than reaching `_start`, which would launch a second Chromium and
      report the profile as locked
- [x] Attachments are limited to an allowlisted folder by resolved path, with a
      whitelist of extensions and no URL downloads
- [~] The WhatsApp **Desktop** adapter still only searches and sends. Every other verb
      is Web-only, because a UIA accessibility tree offers geometry where the operations
      need identity. `whatsapp_probe.py` reports which Web selectors are live, so a
      WhatsApp layout change is a specific line rather than a silent no-op
- [x] The Web selectors are verified **against a live account**, and the live DOM was not
      what any of them assumed. Measured with `whatsapp_probe.py` (closed composer) and
      `whatsapp_message_probe.py` (open one), because the send button and the bubbles do
      not exist until text is typed:
  - Message rows carry **hashed class names** (`x1liijdw xu342n7 …`) and no `data-id`,
      so `div.message-in` / `div.message-out` match *nothing* and no additional spelling
      can fix it. Six scripts used them and every symptom was silent — reads returned
      nothing, and `delivery_seen` never confirmed a send, which is the whole of "she
      cannot send even after I said yes": the send worked and the confirmation was blind.
  - The bubble is `[data-testid="msg-container"]` (200px); `[role="row"]` is a **full-width
      container** (920px of a 932px pane). Direction is bubble centre against **pane**
      centre, not the viewport — the pane starts at x=491 of a 1422px window, so a
      viewport midpoint makes a sent message indistinguishable from a received one.
  - `_JS_ROWS_FN` is the single definition, shared by all seven message scripts, and it
      trusts the test id alone. A structural fallback once matched the encryption banner
      (no `msg-container`) and an empty chat reported one *delivered* message; the
      fallback is gone rather than tuned, because an empty read is a refusal and a wrong
      read is not
  - A **click inside the page is not a click** — `element.click()` is untrusted and
      WhatsApp's list ignores it. Rows and menu triggers are marked and clicked through
      Playwright. This was the real cause of "she can search but cannot select", and the
      probe showed it as `opened: False` after a click that reported success
  - The sign-in check **waits for evidence** and reports `loading` separately from
      `logged_out`, because one immediate read called a signed-in account signed out and
      every selector missing. A cold start needs ~90s
  - The probe **releases the browser in a `finally`**; leaking it made the tool
      single-use and buried its report under an `EPIPE` stack trace
- [x] Mouse and keyboard control for every application without its own tool.
      `desktop_control` observes a window through UI Automation and clicks, types,
      moves the mouse and presses keys by the name on a control. Ships **off**
      (`desktop.input`), and the **Tools page card is the switch** — `desktop.input`
      is the explicit override underneath it, not a second switch the user has to
      know about. It refuses only its **own** window, because an agent that can click
      anything can click the Tools page and switch itself off mid-turn
- [x] **Five verbs, and that is the point.** `observe`, `move`, `click`, `type`,
      `press`. It was nine; `double_click` / `right_click` / `drag` / `scroll` were
      four ways to ask for one of two intentions, and every extra verb is a coin flip
      the model can lose on a voice turn's budget of attention. The report was that she
      "cannot click anything", which was never this tool's fault and is exactly what
      an ambiguous surface feels like from outside. `input.py` keeps the mechanics — a
      right button, a drag, a wheel notch — tested and cheap; they are simply not verbs
- [x] **Turning it on silences the browser tools.**
      `tools_permission.SUPPRESSED_BY_DESKTOP_INPUT` = `("browse", "web_control")`.
      `desktop_control` can drive a browser by coordinate, so three tools that can click
      the same page is how the model ends up in the weakest of them. An **exclusion**,
      not a preference, and one-directional: turn it off and both come straight back.
      Each card's reason names the switch responsible
- [x] **The Tools page card is the switch, and it was dead twice over.** It wrote
      `tools.desktop_control` while the tool read `desktop.input`, *and*
      `lib.rs::save_config` cleared `CATALOG_CONFIG` but not `CATALOG_TOOL` — every
      card's `enabled` is computed by the worker from that same document, so the write
      succeeded and the next read returned the catalog from **before** it. A switch
      that reverts on its own is indistinguishable from a broken feature. Precedence:
      `desktop.input` → card → environment → off
- [x] **The foreground lock is worked around, not complained about.** Windows refuses
      to hand the foreground to a background process while the user is interacting,
      and enforces that against the *calling thread*, so a bare `SetForegroundWindow`
      silently does nothing on exactly the turns a user is watching — which is how
      "she can't type" happens from a terminal. `uia.bring_to_front` uses
      `AttachThreadInput`: the documented way past it, and what
      `windows_control._focus` already did. `ensure_foreground` (strict, for typing)
      uses it; a click keeps its own position, so it may ignore the result
- [x] **"The screen" is not the foreground window.** It was, and that was the whole
      of "she can't click anything": while you are *talking* to Lumine the foreground
      window **is Lumine**, so every request read her own chrome — and an always-on-top
      Rainmeter clock widget takes the foreground without the user doing anything,
      exposes **zero** controls, and produced *"I can only see a blank page"* with
      Notepad right behind it. `capture.candidate_windows()` filters for usefulness
      rather than z-order and `best_window()` takes the first candidate that actually
      exposes a control, bounded by `CANDIDATE_TRIES`
- [x] **An integrated window is a hint, not a refusal.** Refusing every browser and
      WhatsApp window made the "fallback" a wall: anything the dedicated tools could
      not phrase for became unreachable. `ops.INTEGRATED_WINDOWS` now *steers* — the
      router still prefers `web_control` / `send_whatsapp`, and the approval card
      carries a note ("web_control would be steadier here") so a user deliberately
      asking for the desktop sees the alternative at the moment they decide. Only
      destructive-by-name and `press` ask
- [x] **`uia.is_our_own` covers the desktop shell, not just the worker.** They are
      different processes, so a pid-only comparison against `os.getpid()` never matched
      the app's own window — the guard existed and did nothing. `_own_process_ids()`
      walks the parent chain while the process is named `lumine`/`lumine-ui` and stops
      at anything else, so a worker run from a terminal does not adopt the terminal
- [x] **Freshness is a second reading, not a token.** `capture.confirm_target` re-walks the
      tree and re-finds the target *by role and name* before any irreversible act. A
      generation comparison cannot do this: the tool supplies the number itself, so
      `check_generation` inside a single call is `x == x` and always passes — it earns its
      place only in `run_task`, whose steps genuinely span a gap, and its docstring says
      so. `move` skips the re-read via `control.UNVERIFIED_VERBS`, decided in one place
      because a rule kept at a call site is a rule the next caller forgets
- [x] `run_task` drives it. `desktop_observe` and `desktop_act` are step verbs, and the act
      `depends_on` the observe because it needs the generation the observe produced.
      `router.py` sends an interaction naming no target to the desktop instead of asking
      "which website?", which was the wrong question for "click the save button".
      `desktop_act` is declared `sensitive` in `STEP_ACTION_CLASS` because its class is
      computed from the element, and a plan must not be able to call a Delete an ordinary act
- [x] A window can be **named**, which is what makes a second monitor reachable:
      `capture.find_window` / `resolve_window`, and a `window` parameter on the tool. Every
      observation publishes the whole virtual desktop, because every coordinate in it lives
      in that space and a monitor left of the primary one starts at a *negative* x. Typing
      into a named window takes the foreground strictly (`uia.ensure_foreground`) and
      **after** the gate — it may steal focus, and a question asked mid-steal is a question
      about something that may not happen
- [x] A smoke test, staged so the ordering *is* the safety argument. `desktop_smoke.py` is
      read-only by default and its `refuse` stage runs **before** anything that moves the
      mouse: a regression that sends a WhatsApp message unasked matters more than any
      successful click. It asserts *no input was injected* by the refusal checks. It found
      the vacuous generation check, which no unit test did — they all fed it a number from
      `_LAST`
- [x] The app has a panel for it. `lumine.screen` carries metadata rather than a picture,
      because the tool reads the accessibility tree and has names instead of pixels;
      `DesktopPreview.tsx` lists what she can click and shows `screen #N`, the generation
      she is reasoning about and the one the approval card asks the user to agree to
- [x] **A music library, on by default, and the folder is the whole boundary.**
      `play_media` is six verbs over one folder the user picks in Settings (Music):
      `find`, `play`, `next`, `previous`, `stop`, `folders`. Five decisions carry it
      and each was a bug first
  - **`play` is in `INTERACT_VERBS`, so it routed to `desktop_control`.** "Play Love
    Is Gone" reached the mouse: Lumine trying to click a player window, with more
    authority than the request needed and no folder involved. `router.py` gained a
    `media` environment at step 4b — *behind* every named target (a site is a page, a
    program is a program) and *ahead* of the generic verb fallback, with
    `_SCREEN_NOUN` and `_ordinal_phrase` as explicit exclusions so "play the third
    video" and "play that clip" still mean a page
  - **The fuzzy matcher is imported from `messaging/whatsapp_flow.py`, not written
    again.** Speech recognition does not spell: "obosthan vocal song" has to reach
    `Obosthan (অবস্থান) - Train Poka Album - HIGHWAY`, and that tolerance was already
    tuned against real output for a contact called `Jubayed`. What *is* different for
    files — filename noise (`vocal`, `song`), brackets, trailing numbers, folder-as-
    playlist — lives in `match.py`; the spelling tolerance does not
  - **Ambiguity is only ambiguity *within a tier*.** `find` returns the best tier and
    drops the rest. A track whose name matches exactly and a track that matched only
    because its *folder* was named are not two equal answers — without this, a
    library with a folder called `Boro` answered "play boro theko" by asking a
    question nobody needed to be asked
  - **Folders are checked *after* name matches, and are never matched by fuzzing
    4,000 filenames.** "Play my vocal songs" arrives as `query="vocal"`, which has no
    significant words at all, so the name tiers skip it and the folder tiers (which
    deliberately keep the noise words, because a folder is *named* `vocal`) catch it
  - **`comparable_name` compares `rsplit(".")[-1]` — which is `"mp3"` — against an
    allowlist holding `".mp3"`, so it never stripped.** The extension stayed in the
    name and a query containing "mp3" matched every file in the library. Caught by
    the first unit test written for it
  - **`scan(root=…)` never resolved the caller's root.** The child resolved and the
    root did not, so `root in child.parents` was False for any path under a short
    `8.3` name — measured on a Windows temp directory. Every file in a good library
    was reported outside it and the scan returned nothing, silently, reading exactly
    like an empty folder. Both sides are resolved now, in one place
  - **`_play_many` validated against `library_root()` rather than the root it
    scanned.** Two roots means validating a scan against a library it did not come
    from. `root` is threaded through, and `_play_one` is shared by `play` and
    `next`/`previous` so the gate call, the boundary check and the failure sentence
    exist once
  - **A folder becomes one ordered queue, not nine simultaneous launches.** A
    reported incident: *"when I said to play the vocal songs, it played all the 9
    songs at once, no queue or anything, I couldn't able to hear anything."*
    `os.startfile` takes **one** file and the OS owns it from there, so the only
    way to play nine was to start nine. `tools/media/queue.py` writes a single
    `.m3u8` from already-validated paths and opens **that**, which every media
    player already queues — and from that moment skip, pause and stop are the
    *player's* controls over its own list. Five decisions carry it, each a bug first
    - **`.m3u8` not `.m3u`**, because the latter is read as the local codepage and
    turns this library's Bengali titles into mojibake in the player's own playlist;
    - **a hash of the body for the filename**, so the same request twice writes one
    file, and `prune` keeps the newest few — the first version counted only the
    *other* files and kept `KEEP + 1`, a slow leak in the temp directory;
    - **written outside the library**, since a `.m3u` dropped in the music folder
      would be picked up by the next scan and offered as a track, and it is still
      absent from `ALLOWED_SUFFIXES`, so `resolve_track` refuses to *play* one — a
      playlist is a file of paths, and honouring one hands playback a directory
      nothing checked;
    - **a title's newlines stripped, not escaped**, because there is no escape in
      this format and one would stand up a second `#EXTINF`;
    - **one track opened directly**, never through a playlist, or `queued: false`
      would be a flag that means nothing.
    `play_many` also now *raises* rather than returning `[]` on a failed hand-over:
    it swallowed per-file errors and reported how many started, which is only
    meaningful while there are N launches, and there is now one hand-over and one
    answer.
  - **`agent/media_queue.py` is the widget's second caller of the same builder.**
    The Music widget runs in the app process and the worker's tools are not
    callable from there, so the button is a separate script — and two
    implementations of "make a queue" would be the same class of bug as a search
    box that ranked differently from the matcher. It sits at the `agent/` root
    because Rust runs it by filename.
  - **`os.startfile` gives the file away, so she cannot stop, pause or skip** — and
    the tool says so rather than claiming otherwise. What she *can* do is play the
    next *file*, which is a real answer to "play the next track" and is why `state.py`
    exists; it records the **last** file handed over, so `next` after a folder does
    not replay one the user has already been given
  - **Moving, renaming and deleting are not built, not merely gated.** A half-built
    delete is a delete that can be got wrong, and you cannot undo a file Lumine moved
    because she read a filename slightly wrong. `find` and `play` are the whole of
    what makes this useful
- [x] **She searches like a search engine, and it is measured against a real
      library.** Nine files on the developer's machine, every one of them a vocal:
      `Akhon Ami এখন আমি Vocal song`, `Husn (Without Music Vocals Only) _ Anuv Jain`,
      `Maand _ Bayaan _ Without Music _ Vocals Only`, Bengali script, `_` and ` - ` as
      separators. The requested case — *"play the Akhon Ami vocal song"* — now plays
      that file, and five design decisions are why
  - **A version is a tag, not a word in the title.** `tags_of` pulls `vocal`,
    `no-music`, `acapella`, `instrumental` out of the filename and `title_of`
    *removes* them, so "Husn (Without Music Vocals Only) _ Anuv Jain" is the title
    "Husn Anuv Jain" with the tag `vocal`. A query word is then matched against
    **title + tags + folder** (`_searchable`), which is the whole difference: the
    word "vocal" was deliberately removed from the title, so a title-only matcher
    reported it *missing* on a file that is a vocal — and against nine such files
    that is nine equal 67% matches and a question nobody needed to be asked
  - **`vocal` stopped being a noise word.** It was, because every file carries it
    and a word on every file carries no information. That made "play all my vocal
    songs" resolve to *no query words at all* and return nothing — the library looked
    empty to the one request that describes it best. Noise words now stay noise and
    version words are answered on the tag axis
  - **Coverage is a number, not a boolean, and it is read strictly.** `MIN_COVERAGE`
    is a floor used with `>`: *more than half* the words, so 2 of 3 qualifies and 1
    of 2 does not. "play boro throat" matching "Boro Theko" on one word is the case
    this refuses, and it is the case that plays the wrong song
  - **The act-or-ask comparison is on one number.** `Hit.confidence` folds tier and
    coverage together (`TIER_TRUST`), because subtracting two *coverages* compares
    a title match against a tag match as if they were the same evidence — and the
    tag won every time, which is why the exact request in the brief came back as
    "which of these nine?" `AUTO_MARGIN` (0.25) is the margin above which she acts
  - **Substring matching on a folder name matched a single letter.** The library is
    called "Lumine Media Folder", so `"a" in "lumine media folder"` is true and
    "play a" matched all nine files — then `best_folder` matched a folder called
    `vocal` the same way and offered to play it. Both are word-based now, in `score`
    and in `best_folder`. A committed version of this also replaced `sys.stdout`
    with a new `TextIOWrapper` at *import* time, which closed the process's real
    stdout when the old one was collected and broke the print of every later test
  - **`media_search.tighten` is the search box's rule and not hers.** She gets every
    match; a list gets the closest three quarters of the best score. Measured: the
    brief's sentence goes from 9 rows to 1, "play bayan" keeps both (two songs by
    one artist is a real choice), and "vocal" keeps all 9 (a tag query was never
    narrow). `shown` and `total` are both reported so the screen can say "2 of 9"
- [x] **`tool_results.as_payload` returns parseable JSON now.** It used to cut the
      *rendered* string at the budget and append `" (truncated)"`, under a comment
      claiming it was "rather than returning a broken object" — and returning exactly
      that. A 900-character result came back as `{"results":[{"name":"a"},{"name":"b"`
      plus a note, so the model could not read the two results inside it and no
      caller could detect it. The budget is now applied to the **structure**: the
      largest list is halved until the object fits, `truncated`/`omitted` are
      recorded inside it, and the caller's dict is never mutated
- [~] dotenv-based configuration still works as a fallback, but is no longer the
      primary path
- [~] logging exists, but not production-grade
- [ ] The video frame rate is declared and tested, but is not yet configurable per
      profile — it is a real recurring cost, and a per-profile option is the honest
      way to expose it

### Planned backend work

- [x] agent manager / lifecycle API
- [x] start / stop / restart commands, invoked by the voice path
- [x] health/status monitoring — a readiness gate in Rust, and a live event
      subscription in `lib/agentRuntime.ts`
- [x] structured config contract (`lumine.config.json` + `agent/settings/validation.py`)
- [ ] app-sidecar packaging
- [~] graceful shutdown handling
- [ ] crash recovery
- [ ] cross-platform binary packaging
- [~] explicit backend-to-front-end state events — failures, tool status and the
      worker's lifecycle are structured and delivered; agent sleep/wake is not
- [x] video input, which is what makes the camera control real
- [ ] video *output* — Lumine sending frames back. That is `AvatarSession` plus a
      plugin, and the SVG avatar engine stays the answer for now

The Python agent remains intact and is still the voice service, and the Tauri layer
owns the development lifecycle contract. `npm run tauri build` now completes and writes
`src-tauri/target/release/lumine.exe`, with the Windows icon and version resources
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

`../docs/ai-control-center.md` is the decision record, and `agent/settings/providers.py` is
the executable authority. Where the three disagree, the code wins.
