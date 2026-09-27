# Lumine AI Control Center — architecture and contracts

This document pins down the contracts that the settings redesign depends on, and
records the decisions taken so far. It is the reference for the remaining
phases; the executable source stays authoritative where prose and code disagree.

Status: **Phases 0-6 complete, plus the token-budget, failure-visibility, and
settings-re-architecture work.** The catalog, validation rules, shared types,
configuration precedence, the capability-driven factory, the Tauri command surface,
the settings surface, profile collections, credential injection, the credential
connectivity check, slot-addressed multi-variable credentials, the model-capability
matrix, and the local-model path all exist and are tested.

Beyond the original phases, four things landed together because they are one
problem seen from four sides - a voice turn that costs too many tokens, fails
silently, and takes the conversation down with it:

- the always-sent system prompt is a fraction of its former size
  (`agent/prompts/persona_core.md`, with the full persona on demand),
- tool results are compressed at the source and never shown inline
  (`agent/tools/tool_results.py`),
- the conversation context is trimmed to a budget rather than left to fail
  (`agent/context_trim.py`),
- a failed turn says so, out loud and in the app
  (`agent/llm_errors.py`, `agent/failure_gate.py`).

`agent.py` now honours a saved configuration when one is valid and falls back to
`agent/.env` otherwise. A fresh install with nothing saved behaves exactly as
before, so the voice path is unchanged until a user saves something.

The closing pass over the whole app changed three things that are not new features
but are the difference between the settings being usable and being documented:

- **Option metadata is the provider's, not a guess.** Every model option now
  declares its widget and its numeric bounds from the plugin's own signature
  (`OptionDefinition.control/minimum/maximum/step`). Three real bugs were latent
  behind this: Cartesia `temperature`, Google TTS `voice`/`speed`, and Google STT
  `language` are not parameters the plugins accept. Each would have saved,
  validated clean, and then raised `TypeError` on the first spoken turn.
- **Voice is a control section, with an audition.** Speed, emotion and volume are
  rendered from that metadata, and a preview button speaks a phrase with the
  *unsaved* draft through the worker's own stage builder
  (`agent/voice_preview.py`, the `preview_voice` command). A slider you cannot
  hear is a slider you drag once and never revisit.
- **One notification per tool call, and one line of it.** A call used to raise two
  toasts and print 320 characters of JSON in the second. There is now a single
  signal per call and `features/toast/toolToast.ts` reduces the payload to one
  readable line.
- **Nothing is outlined, anywhere.** The borders-to-depth pass covered the whole
  app, not only the settings screen, and it is verified by walking the rendered
  DOM on every route and counting non-zero computed border widths. The count is
  zero outside three elements: the avatar's presence halo and its two orbit
  rings, which are drawn geometry from the avatar engine rather than UI chrome.
  Three elevation steps (`--elevation-1/2/3`, surfaced to Tailwind as
  `shadow-elev-1/2/3`) and one control convention — flat at rest, raised on hover,
  accent and inset-ring when chosen — now carry all of it.
- **Explanation is a hover, not a paragraph.** The settings pages put their prose
  in a `?` affordance (`components/ui/hint.tsx`) and the profile and appearance
  lists became dropdowns. A preset dropdown keeps the accent swatch, because a
  saved palette is a colour first and a name second.

## Decisions taken

Recorded so later phases do not relitigate them:

| Question | Decision |
| --- | --- |
| Credential backend | `keyring` crate (verified), Stronghold held in reserve. |
| Settings shape | Overlay shell, no router dependency. |
| Interruption mode | Moves into `AI → Voice & Models`; it is part of the active profile, not appearance. |
| Persona | Read-only until the settings surface exists. |
| Settings file owner | Rust writes it atomically; the JS store plugin is not the authoritative copy. |
| LLM on failure | Speaks a short apology *and* raises a toast. Silence is indistinguishable from a muted microphone. |
| Tool result text | Never in the conversation transcript; delivered on a toast instead. |
| Tool result content | Wrapped in a delimiter and labelled as data, so retrieved text cannot act as an instruction. |
| Persona on a token budget | Split into an always-sent core plus an on-demand reference. The full text is unchanged on disk. |
| History growth | Trimmed to an item budget, as a backstop. Compression at the source is the actual fix. |
| Retries on failure | None. Back off and tell the user; hammering a rate-limited endpoint spends the remaining quota. |
| Multi-variable credentials | Addressed by slot. A single-variable provider keeps the bare provider id; a multi-slot one never falls back to it. |
| One picker | Every chooser is `components/ui/dropdown.tsx`. No native `<select>`, `<datalist>`, or segmented button row anywhere. |
| Advanced settings | Classified in the catalog, not the UI. Temperature, token caps and `top_p` go behind a disclosure; voice, speed, emotion and thinking level do not. |
| Settings navigation | `Appearance · Voice · Models · Providers · Diagnostics`, with `Models` carrying a sub-tab per stage. One route object, not two pieces of state. |
| `local` vs `ollama` | `local` stays **reserved** for weights that ship with the app. `ollama` promises a server the user started, and shipping the first under the second's name would be a promise the app cannot keep. |
| Local model discovery | A button, not an automatic list. A network round trip on a tab somebody came to for something else, against a server that may not be running, is not a list. |
| Camera and screenshare | Local preview only. Nothing is published, so no frames reach the agent and the OS lights no recording indicator. |
| Model capabilities | `inputModalities` means "what Lumine can hand this model on the path it is reached by" — not what it could accept in principle. |

## What exists today

The only configuration that crosses from the desktop app to the worker is the
interruption mode, carried in LiveKit dispatch metadata. Provider, model, voice,
and credential settings come exclusively from `agent/.env` and are resolved by
`agent/pipeline_config.py` and `agent/llm_config.py` into the hardcoded
constructors in `agent/pipeline_factory.py`. There is no settings store, no
provider catalog, and no validation layer.

## Layer 1 — the provider catalog

`agent/providers.py` is the single source of truth for what Lumine can do.

* A provider is **not** an identity to branch on. It is a set of capabilities:
  `stt`, `llm`, `tts`, `realtime`, `vad`, `transport`.
* Each model carries the capability it satisfies and the flags that make
  combinations legal or illegal (see below).
* The catalog is **static and offline**. There is no cross-provider
  "list my models" API that every LiveKit plugin implements, so capability
  detection is curated from documentation rather than probed. A future provider
  test command can confirm entries against the live API.
* The catalog is **secret-free**. It exposes environment variable *names* (which
  already appear in `agent/.env.example`) but never values.

`agent/provider_catalog.py` prints a redacted JSON view for the desktop layer:

```powershell
python agent/provider_catalog.py            # compact JSON on stdout
python agent/provider_catalog.py --pretty   # indented
python agent/provider_catalog.py --check    # exit 1 if incoherent
```

`catalog_issues()` is a pure self-check used by `--check` and by
`agent/tests/test_providers.py`.

`local` is a **reserved** provider id, deliberately undefined. The abstraction
must not force a cloud-only assumption, but shipping a selectable provider with
no backend would be a lie in the UI.

## Layer 2 — the configuration document

Versioned JSON, described by `ConfigDocument` in
`lumine-ui/src/features/settings/aiConfigTypes.ts` and exemplified by the golden
fixture at `agent/tests/fixtures/lumine.config.example.json`. That fixture is
validated by the test suite, so it cannot drift away from the catalog.

Two profile kinds, matching the two architectures LiveKit supports:

* **pipeline** — `stt`, `llm`, `tts`, optionally `vad`.
* **realtime** — a speech-to-speech model, plus an `output` of either
  `model_voice` (the model speaks) or `custom_tts` (a separate TTS speaks).

`realtime` + `custom_tts` is LiveKit's half-cascade. Profiles reference
providers; they never contain credentials.

## Layer 3 — validation

`agent/validation.py` is a pure function over plain dictionaries. It imports no
provider and makes no network call, so it can run before a room is joined and
without any credential. It answers "could this work?", not "does this key work?".

Diagnostics are graded `error` (blocks activation), `warn` (runs, but something
is off), `info`. Only errors block.

The rules that matter, and why they exist:

| Code | Severity | Why |
| --- | --- | --- |
| `credential.missing` | error | The referenced provider needs a key and none is available. |
| `stage.capability_mismatch` | error | Asking a provider for a capability it does not have, e.g. Cartesia for an LLM. |
| `stage.unknown_model` | error | Model is not in the curated catalog for that provider and capability. |
| `model.retired` | error | The provider has removed the model; it is unreachable. |
| `model.deprecated` | warn | Names its replacement in `hint`. |
| `stt.requires_vad` | error | Non-streaming STT backends (Google, `gpt-realtime-whisper`) need a VAD to delimit segments, or the session raises. |
| `realtime.custom_tts_unsupported` | error | LiveKit's separate-TTS path needs a **non-native-audio** realtime model. Gemini Live models are native-audio, so pairing them with Cartesia is refused rather than silently downgraded. |
| `realtime.finish_response_unsupported` | error | A realtime model that owns turn detection cannot have `turn_handling.interruption.enabled=False`; LiveKit raises `ValueError` at session start. |
| `output.no_model_voice` | error | The realtime model publishes no voice, so `model_voice` output is impossible. |
| `voice.unknown` / `voice.missing` | warn | Custom and freshly cloned voices are legitimate, so this never blocks. |
| `realtime.*_unsupported` (options) | warn | The Gemini Live models do not support proactivity, affective dialog, or asynchronous function calling. |

### Two findings worth acting on

1. **`realtime + custom TTS` is currently impossible with the default model.**
   Lumine's default is `gemini-3.8-live`, a native-audio Gemini Live model, and
   so is every other Live model Google offers. The rule is enforced, not assumed,
   and a test proves both directions: blocked for native-audio, allowed once a
   model advertises `textOnlyModality`.

2. **The legacy cascade hardcodes a TTS model that is about to be withdrawn.**
   `agent/pipeline_factory.py` builds `cartesia.TTS(model="sonic-2", ...)`.
   Cartesia deprecates Sonic 2 with a retirement date of **2026-10-20**, after
   which it stops being served. The catalog marks it deprecated with
   `sonic-3.5` as its successor, and the golden-config test asserts that the
   warning is still emitted so the migration cannot be forgotten. This is the
   one Phase 0 finding that is time-boxed rather than hypothetical.

## Layer 4 — credentials (decided)

Non-secret configuration belongs in a Tauri-managed store, versioned, migrated,
and written atomically by Rust. Secrets do not belong in that file.

The frontend must never be able to read a credential back, so the command surface
is write-only plus status: `set_credential`, `delete_credential`,
`get_credential_status` returning `present` / `last4` / `updatedAt`. There is
deliberately no command that returns plaintext.

### Credentials are addressed by slot, not by provider

`set_credential`, `delete_credential` and `get_credential_status` each take an
optional `slot`: the environment variable the value belongs in. The keyring
account name is the bare provider id when no slot is given, and
`{provider}:{VARIABLE}` when one is.

This exists because the first version stored one secret per provider id and
`credential_injection.rs` wrote that single value into **every** variable the
provider declared. Harmless for a provider with one key. Fatal for LiveKit,
which needs three: a user who pasted their API key got `LIVEKIT_URL=<api key>`,
`LIVEKIT_API_KEY=<api key>` and `LIVEKIT_API_SECRET=<api key>`. The worker could
not register, `wait_for_agent_worker` timed out at 20 s, and the agent never
joined the room.

Nothing surfaced it. There was no LiveKit probe — the Providers page said "not
testable" for the one credential that decides whether a session can happen — and
`setup_status.rs` asked only whether *any* value existed for the provider, which a
single pasted value satisfied.

Two properties make the rekey safe and the gap visible:

- **A single-variable provider keeps the bare provider id**, and
  `secrets_for_worker` falls back to it. Every key stored before this change is
  found again with no migration. Google, Groq, Cartesia and OpenAI are untouched.
- **A multi-variable provider never falls back to the bare entry.** One value
  cannot stand in for three, so reading it would reinstate the bug rather than
  work around it. Its absence is reported instead.

An existing bare `livekit` entry therefore has to be re-entered once. That is a
real, one-time inconvenience and it is the honest outcome: the stored value was
never a usable LiveKit credential, and there is no way to split it into three.

The catalog publishes `keySlots` — one entry per variable, with a label a person
can act on ("Server URL", not `LIVEKIT_URL`) and a `kind` of `secret`, `url` or
`text`. The UI renders that list and holds no provider knowledge of its own. A
`url` slot is drawn in the clear rather than masked: a hidden hostname hides the
exact typo that caused this, and `KeySlot.__post_init__` refuses to mark a
variable named like a credential as one.

### The LiveKit probe

`ProbeDefinition` gained `auth_kind` and `token_path`. `"header"` is the ordinary
case — one secret, one header. `"livekit_token"` is for LiveKit, which has no
bearer key at all: access is a short-lived JWT minted from the key and signed
with the secret, so the three values only mean anything together.

The probe POSTs to `{server}/twirp/livekit.RoomService/ListRooms` with that token.
`ListRooms` is the cheapest authenticated call there is — 200 and an empty list on
a new project — and it separates the three failure modes that matter: an
unreachable host (inconclusive), a refused token (rejected), and a working
credential (valid). A `wss://` server address, which is what the LiveKit console
hands out, is translated to `https://` rather than rejected.

Verified on this checkout against a real project: 200 with correctly slotted
values, and a plain "LIVEKIT_URL is not a server address" when all three
variables hold the same value, which is what the old injection produced.

### Decision: the `keyring` crate

The candidates were the `keyring` crate (Windows Credential Manager, macOS
Keychain, Linux Secret Service) and Tauri's Stronghold plugin (encrypted vault,
but requiring a vault password, carrying a documented upstream scrypt caveat,
and adding a JS-facing surface the webview could reach if capabilities are
misconfigured).

`keyring = "4"` was chosen and **verified on this checkout**, not assumed:

* `cargo check` passes at baseline, so the crate is a viable addition.
* Default features already select all three desktop stores, so no per-platform
  feature juggling is needed.
* `lumine-ui/src-tauri/src/credentials.rs` round-trips a value against the real
  Windows Credential Manager, confirms `status` returns only a four-character
  tail, and deletes the probe. Three tests, all passing.

Stronghold remains a plausible fallback if a future requirement calls for an
encrypted vault that does not depend on an OS service, but exactly one backend
should ever be implemented. `credentials.rs` is currently a spike: it is
compiled and tested but **not registered on the Tauri command surface**, so app
behaviour is unchanged.

Linux needs a fallback path: Secret Service is absent on headless machines.
`CredentialError::Unavailable` models exactly that, and the app should fall back
to `agent/.env` rather than fail.

Delivery to the worker in this phase is to inject the provider keys into the
worker's environment at spawn. A credential change therefore requires a worker
restart, which the UI must state. Secrets in a child process environment are
readable by same-user processes — an accepted, standard tradeoff compared with
shipping them to the webview.

## Precedence

Implemented in `agent/config_store.py`; the single place the rule lives.

```
a saved configuration document
      (LUMINE_CONFIG_PATH
       > the desktop app's local data directory
       > agent/lumine.config.json)
  >  agent/.env
  >  the built-in defaults in pipeline_config.py / llm_config.py
```

The first existing location wins, so the document the desktop app wrote is found
whether or not Tauri started the worker. See *The third smoke test* above for why
that second location exists.

The load path never raises. A missing file returns `None`; a corrupt, oversized,
or unsupported-version file returns `None` plus a diagnostic, so the worker falls
through to the environment and starts. Losing a bad config must not mean losing
voice.

`env_document()` synthesizes a read-only document from the resolved
environment. That is what the desktop layer will show as the "managed by
agent/.env" profile, so the active stack is visible without editing anything.
Its ids are prefixed `env-` so the UI can refuse to save over it.

### Where the file lives, and the one thing to watch

Rust writes to the app data directory; Python reads from the agent directory by
default. They agree only because `LUMINE_CONFIG_PATH` overrides both. **The
worker-wiring step must pass `LUMINE_CONFIG_PATH` into the child environment**,
otherwise the two runtimes will silently read different files. That is the first
thing to check when the worker starts reading a saved configuration.

## Phase 1: the Tauri command surface

Registered in `lib.rs`, and deliberately inert with respect to the worker:

| Command | Returns | Notes |
| --- | --- | --- |
| `get_provider_catalog` | catalog JSON | Runs `agent/provider_catalog.py`, so there is one list of providers. |
| `get_config` | `{source, document, diagnostics}` | `source` is `"ui"` or `"env"`; `"env"` means the document is a read-only report of `agent/.env`. |
| `save_config` | — | Atomic write with a `.bak` alongside. |
| `validate_config` | `{ok, errorCount, warningCount, diagnostics}` | Takes the document as a string, because it arrives untyped from the webview and may legitimately be malformed. |
| `set_credential` | redacted status | Write-only. Takes an optional `slot` (the variable name). |
| `delete_credential` | — | Deleting an absent credential is a success. Also takes `slot`. |
| `get_credential_status` | redacted status | `present` / `last4` only. Also takes `slot`. |

There is deliberately **no command that returns a credential value to the
webview**. The status shape is the whole contract with the UI.

`python_env.rs` centralises interpreter and `agent/` resolution for the new
commands. The three pre-existing inline copies in `lib.rs` were left alone: they
sit on the working voice path and consolidating them is a separate, low-risk
change.

`agent_manager.rs` passes `LUMINE_CONFIG_PATH` into the worker environment, which
is what makes the two runtimes agree on one file.

## Phase 2: the capability-driven factory

`agent/pipeline_factory.py` no longer hardcodes providers. Each capability has
its own builder that dispatches on the *provider id* from a
`ResolvedProfile`, which is what makes stages independently selectable:

| Stage | Builder | Providers wired |
| --- | --- | --- |
| STT | `_build_stt` | groq, google, openai |
| LLM | `_build_llm` | groq, google |
| TTS | `_build_tts` | cartesia, google, openai |
| VAD | `_build_vad` | silero |
| Realtime | `_build_realtime` | google |

Two entry points:

* `build_pipeline(profile, interruption_mode)` — the environment path used by
  `agent.py`. Unchanged signature and behaviour.
* `build_resolved(resolved, validate=True)` — the profile path, which validates
  before constructing and raises `ConfigurationRejected` carrying the diagnostics.

Half-cascade (realtime understanding plus a separate TTS) is assembled by
`_build_realtime_session` when `output_mode` is `custom_tts`. It is gated on
`textOnlyModality`, so Gemini's native-audio models are refused here *and* in
validation — the rule is not left to the settings UI.

Provider plugins are still imported at module scope, on the main thread, under
the names the tests patch (`_groq`, `_cartesia`, `_silero`). Builders resolve
them through `provider_module()` at call time so a patch is observed everywhere.

### Three bugs this phase caught in its own first draft

Worth recording, because each was a silent-wrong-answer rather than a crash:

1. `env_profile()` re-read `LUMINE_PIPELINE` from the environment, so
   `build_pipeline("legacy_cascade")` silently built a **Gemini** session. It now
   takes the already-resolved pipeline name.
2. `PipelineComponents.profile` started reporting the profile's display name
   instead of the pipeline id that logs and runtime events key on. `ResolvedProfile`
   now carries a separate `profile_id`.
3. In `resolve_profile`, the TTS options dict put the environment defaults *after*
   the profile values, so an explicit `language: "fr"` was overwritten by `"en"`.
   Precedence was simply inverted.

Plus one in the test suite: the legacy factory's fakes were
`STT=lambda: object()`, which cannot accept the `model=` the resolver now
passes. The fakes were updated to record their arguments, which also turned them
into a stronger assertion that the resolved model, voice, VAD threshold, and
Groq budget guards actually reach the plugins.

## Phase 3: the settings surface

An overlay shell with its own left rail rather than a router, as decided. Only
sections with something real behind them are listed, so there is no
"Notifications" or "Privacy" page teaching the user the app has features it does
not:

| Section | What it does |
| --- | --- |
| General → Appearance | The existing appearance controls, unchanged in behaviour. |
| AI → Voice & Models | The active-configuration card, per-stage pickers, interruption, live validation, save/discard. |
| AI → Providers | Capability badges and write-only credential handling. |
| System → Diagnostics | Worker state, configuration source, catalog counts, deprecated models. |

Three rules hold throughout:

1. **No provider name is hardcoded in JSX.** Every picker and label is generated
   from the catalog, so adding a provider is a Python change only.
2. **The worker decides validity.** The UI renders the diagnostics it is given
   and refuses to save while a blocking one exists, so a screen can never offer
   a combination the runtime refuses.
3. **Credentials are write-only.** There is no code path in the frontend that
   could display a stored secret.

A credential is entered with `window.prompt` rather than a bespoke form. That is
a deliberate placeholder: a real secret field needs paste handling, reveal
toggling, and autocomplete suppression, and it should be built once as a shared
primitive rather than inline on the first page that needs it. It is the one piece
of this surface that visibly wants a Phase 4 pass.

Interruption mode moved out of Appearance into **AI → Voice & Models**, where it
belongs: it is part of the active profile, not the palette. `Home.tsx` now reads
it from the active profile, so the value that reaches the voice session and the
value shown in Settings cannot drift.

## Phase 3: the worker honours a saved configuration

`build_session_components` in `agent.py` is the seam. It prefers a saved
profile, and falls back to the environment in every failure mode:

| Situation | Result |
| --- | --- |
| Saved document valid, profile resolves | The saved profile governs. Emits `config_applied` with `source: "ui"`. |
| Saved profile fails validation | Environment takes over. Emits `config_rejected` with the diagnostics. |
| `activeProfileId` points at nothing | Environment takes over. Emits `config_rejected`. |
| No saved document at all | Environment takes over. Emits `config_applied` with `source: "env"`. |

Falling back rather than failing is deliberate: losing a voice session to a bad
settings file would be far worse than ignoring it. `config_applied` and
`config_rejected` are ordinary `LUMINE_EVENT` records, so the decision is visible
in the terminal during a smoke test.

## The second smoke test: two defects, one cause

The report was that switching voice or model in the settings screen "always shows
Gemini Live (from agent/.env)", and that only two voices were available. Both
traced to decisions made while building Phase 3.

**The stack type could not be changed.** The Pipeline/Realtime toggle was
rendered `disabled`, with the note *"Create a profile of the other type to
switch"* — but profile creation is Phase 4 and does not exist. The UI was
instructing the user to perform an action that was impossible. Because the
environment profile is `kind: "realtime"`, every conversation was stuck on
Gemini Live, and the only voice picker on screen was Google's. The pipeline
voice list (Cartesia) was unreachable code.

The fix is `convertProfileKind` in
`lumine-ui/src/features/settings/profileOps.ts`. It rebuilds a profile for the
other architecture, seeding each stage from the catalog. Nothing from the
previous architecture is carried across, because there is nothing sensible to map
a Gemini Live model onto a speech-recognition call; the interruption mode is the
one exception, since it means the same thing in both.

**Seeding had to be a product decision, not a sort order.** The first version
picked the first provider alphabetically, which put Google in front of Groq for
speech recognition and the language model. Clicking one button would have moved
the conversation to a different vendor from the one `agent/.env` names. The
catalog now records the answer explicitly:

```python
ProviderDefinition(preferred_for=("stt", "llm"))   # groq
ProviderDefinition(preferred_for=("tts",))          # cartesia
ProviderDefinition(preferred_for=("vad",))          # silero
```

`realtime` deliberately has no preferred provider: a realtime stage is a single
model, and that choice comes from the model's own `default` flag. `transport` is
a reserved slot with no provider yet. `catalog_issues()` rejects two providers
claiming the same capability, and a provider claiming one it cannot do.

This field is the reason the catalog version moved to **2**. The desktop layer
refuses a catalog newer than it was written for, and a test compares the two
constants directly, because a forgotten bump would otherwise render a catalog
whose fields the screen does not have.

**Voices.** The curated list had two Gemini Live voices, which read as a broken
picker. Gemini Live and Gemini TTS draw on one named voice set, so all five
known voices are now offered. The two groups overlap, so the merge deduplicates;
`catalog_issues()` rejects a duplicate voice id, which is how the first version
of this list was caught shipping duplicates.

The picker is now a text input with a `<datalist>` rather than a `<select>`.
Providers publish far more voices than any curated list, and a cloned voice has
no name that could be listed. An earlier build displayed the hint *"a voice
outside this list can still be pasted by hand"* while offering no way to do
that; the control now matches the claim.

**The active card.** Its heading was the profile name, which for the environment
profile is the provenance label `Gemini Live (from agent/.env)`. After a voice
change that heading still read as if nothing had happened. The name is now
editable, and the badge distinguishes three states instead of two: `From
agent/.env` only while the draft is unmodified, `Unsaved edits` once it differs
from what is saved, and `Saved` afterwards. A badge that lies about where a
configuration came from is worse than no badge.

## The third smoke test: settings were saved and silently ignored

A settings change to the pipeline stack produced no change at all, and the
worker log said `config_applied ... source: "env"`. The save had worked: the
document on disk was a correct pipeline, seeded exactly as intended. The two
halves were looking in different places.

| Side | Default path when `LUMINE_CONFIG_PATH` is unset |
| --- | --- |
| Rust — the UI **writes** | `%LOCALAPPDATA%\com.art.lumine-ui\lumine.config.json` |
| Python — the worker **reads** | `agent\lumine.config.json` |

`agent_manager.rs` passes `LUMINE_CONFIG_PATH` to the worker, but only when
**Tauri** starts it. The documented development workflow is to run
`lk agent dev` in a terminal, where the LiveKit CLI starts the worker and no
such variable exists. So a worker started the documented way could never see
settings the app had saved.

The precedence rule is now, most specific first:

1. `LUMINE_CONFIG_PATH` — and *only* that, so a test pointed at a fixture can
   never fall through to the developer's real settings.
2. The desktop app's local data directory, computed from the Tauri identifier.
3. The agent directory, so a hand-placed file keeps working.

Step 2 is the fix. `app_local_data_dir()` in `config_store.py` mirrors Tauri's
`app_local_data_dir()` (`%LOCALAPPDATA%` on Windows,
`~/Library/Application Support` on macOS, `$XDG_DATA_HOME` on Linux). The shared
facts — the identifier and the filename — are asserted against
`tauri.conf.json` by a test, because a rename there without a matching change
here would split the two sides again, silently and in exactly the same way.

**The silent part was the real defect.** A fallback to the environment is
indistinguishable from "the user has no saved settings". Now every outcome names
the location: `config_applied` and `config_rejected` carry `config_path`,
`validate_config.py --describe` reports `configPath` and `searchedPaths`, and
`load_document` logs where it looked. A mismatch is answerable from the log
instead of requiring a guess about who started the worker.

`app_local_data_dir()` avoids `Path.home()` unless the platform's own variable
is absent, because `Path.home()` raises in a stripped environment and a worker
that cannot start over a missing home directory is worse than one that falls
back to the agent directory.

## Gemini 3.8 Live

The LiveKit plugin documentation only lists the 3.1 and 2.5 Live models.
Google's own model list is ahead of it, and the installed plugin
(`livekit-plugins-google` 1.8.2) is ahead of both — it already carries:

```python
KNOWN_GEMINI_API_MODELS = frozenset({
    "gemini-3.8-live",
    "gemini-3.8-live-extended-thinking",
    "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio-preview-12-2025",
})
MODELS_WITHOUT_REPLY_PLACEHOLDER = ("3.1", "3.8")
```

Google now labels `gemini-3.1-flash-live-preview` **legacy** and states *"we
recommend updating to Gemini 3.8 Live"*. So:

| Model | Status | Notes |
| --- | --- | --- |
| `gemini-3.8-live` | available, **default** | Google's default Live model |
| `gemini-3.8-live-extended-thinking` | available | high-reasoning variant |
| `gemini-3.1-flash-live-preview` | deprecated → 3.8 | kept so old profiles still work and can be told what to move to |
| `gemini-2.5-flash-native-audio-preview-12-2025` | deprecated → 3.8 | Google limits 2.5 access to prior users |

The 2.5 entry's id was also **wrong**: the catalog listed `gemini-2.5-flash` as
a Live model, but that id is the general model; the Live one is
`gemini-2.5-flash-native-audio-preview-12-2025`. It is not in the plugin's known
set, so it would have been rejected at session start.

All four remain native-audio, so the half-cascade rule is unchanged: realtime
plus a separate TTS is still impossible, and validation still blocks it.

`gemini-3.8-flash` replaces `gemini-3-flash-preview` as the default Google LLM.

A test now reads the plugin's own `KNOWN_GEMINI_API_MODELS` and asserts every
realtime model in the catalog is in it. The catalog is documentation of what the
plugin accepts; the plugin is what actually decides, so the catalog is checked
against it rather than the other way round. This is how the wrong 2.5 id would
have been caught.

## Phase 4: profiles and a real secret field

Phase 3 could only edit the *active* profile. Phase 4 adds the collection
operations, and replaces the `window.prompt` placeholder that Phase 3 left
behind.

### Profile collections

Every operation is a pure function in `profileOps.ts` taking and returning a
whole `ConfigDocument`. The draft is React state, so an in-place mutation would
not re-render, and a guessed id could collide with one already in the file. Each
operation returns its input **unchanged** when it refuses, which is how
`useAiConfig.mutateDocument` recognises a no-op and skips revalidation.

| Operation | Behaviour worth stating |
| --- | --- |
| `createProfile` | Seeds from the catalog, not from the active profile. A new profile is a new start; inheriting somebody else's models unasked is how a user stops knowing what a session runs. |
| `duplicateProfile` | An exact copy under a new id and name, then activated. This is how someone explores a change without risking a setup that already works. |
| `renameProfile` | Trims; an empty name or unknown id is refused. |
| `activateProfile` | Refuses an unknown id. |
| `deleteProfile` | The **last** profile cannot be deleted. Deleting the active one promotes the profile *before* it, so removing the first of three does not jump the user to the end of their own list. |

Ids and names are de-duplicated with a counter rather than a random suffix, so
creating twice from the same starting point yields the same id and the file stays
diffable instead of churning on every press.

`seedProfile` was extracted from `convertProfileKind` because the first version
of `createProfile` set `kind` on its seed and then called `convertProfileKind`,
which returns its input untouched when the kind already matches. That produced
**empty profiles** — a document that saved and then refused to run. `check:profile-ops`
now asserts every structural operation leaves ids unique, the active id
resolvable, at least one profile present, and every profile complete.

The environment-derived profile is editable like any other and is deletable when
another profile exists. It was already editable in Phase 3, so making it
read-only now would have been a regression.

Python gained one rule: `profile.duplicate_name`, a **warning** rather than an
error. Two profiles may legitimately share a name and both run; what breaks is
the settings list, where two identical rows make the active one ambiguous. The
diagnostic names where the other one is, and comparison is case-insensitive.

### The secret field

`SecretField` is a shared component, because a secret field has three
requirements that are easy to forget and are not cosmetic:

- **Masked by default.** A credential in a plain input is visible to anyone
  looking and ends up in a screenshot.
- **Autocomplete suppressed.** A browser filling the field from its own
  password store can put a *different account's* key in it, and nothing in the
  UI would show the difference.
- **Reveal is deliberate and self-resetting.** A key cannot be left on screen by
  someone who stepped away.

The value lives in component state only while the field is open. At most one
provider's field is open at a time, so two masked fields never sit on screen
together.

### Credential injection (this was Phase 5)

Phase 3 shipped a Providers page that stored a key in the OS keyring and then
did nothing with it. The page was honest about the split, but a key that is
reported as stored and never reaches the runtime is the same class of defect as
the configuration-file mismatch fixed above, so it was closed in the same phase.

`credential_injection.rs` sets each stored key on the worker's child process
environment at spawn:

1. Read `provider_catalog.py` for the provider-to-variable mapping. It is
   catalog knowledge — it lives beside the models in `agent/providers.py` — so it
   is not duplicated in Rust.
2. For every provider that `requiresKey` and has a stored secret, set each
   variable the catalog names for it.
3. Log the provider **ids** only. A value never reaches a log, an event, the
   webview, or disk.

Three deliberate decisions:

- **Injected values win over `agent/.env`.** `agent.py` calls `load_dotenv()`
  with the default `override=False`, which leaves an already-set variable alone.
  Verified directly rather than assumed.
- **A provider with no stored key is left to `agent/.env`.** That remains a
  supported way to configure the worker, and an unavailable keyring (a headless
  Linux box with no Secret Service) must not stop the worker from starting.
- **A blank or whitespace-only secret is not injected**, because it would blank
  out a working `agent/.env` entry.

A key change therefore needs a **worker restart** to take effect, which the
Providers page now says. This is a real limitation: the worker is a persistent
LiveKit process, and re-reading credentials per job would mean a keyring round
trip on every voice session.

## The fourth smoke test: a profile that produced silence

A new pipeline profile using `gemini-3.8-flash` started a session, requested a
greeting, and produced **no audio at all**. The log held one line of substance:

```
400 Bad Request — "Thinking level MINIMAL is not supported for this model."
```

The saved profile contained no thinking level. The factory had invented one:

```python
"thinking_config": {"thinking_level": settings.get("thinking_level", "minimal"), ...}
```

`minimal` was correct for **Gemini Live** and wrong for the **text** API. Google
publishes a per-model table, and the sets are not nested:

| Model | Levels accepted |
| --- | --- |
| `gemini-3.8-flash` | low, medium, high |
| `gemini-3.7-flash` | low, medium, high |
| `gemini-3.6-flash` | minimal, low, medium, high |
| `gemini-3.5-flash`, `-lite` | minimal, low, medium, high |
| `gemini-3.1-pro-preview` | low, medium, high |
| Live API (`gemini-3.8-live` and the rest) | minimal, low, medium, high |

So one default for the family is wrong by construction, and the failure mode is
the worst kind: the session starts, the room connects, the greeting is
requested, and audio never arrives. A 400 on the first LLM request is
indistinguishable from a muted microphone.

Three changes, in the order they matter:

1. **The catalog owns the set.** `ModelDefinition.thinking_levels` is empty when
   a model is not configured by level at all — Gemini 2.5 uses a token budget, and
   the plugin only translates a level for the Gemini 3 family. Empty means *send
   nothing*, which is different from *send minimal*.
2. **The factory never invents a value.** A level is sent only when the profile
   sets one and the catalog lists it. Absent means the model applies its own
   default, which is valid by construction. A level the model does not accept is
   downgraded to the lowest one it does, so a stale hand-edited profile still
   starts instead of failing silently.
3. **Validation blocks it up front.** `llm.thinking_level_unsupported` is a
   **blocking** error naming the levels the model does accept, so the settings
   screen refuses to save rather than letting the API refuse at request time.

The realtime path keeps a level, because the Live plugin documents `minimal` as
its lowest-latency default and omitting it would change the latency profile of a
session that already works. That value is now read from the catalog rather than
written into the factory.

## A second bug this found: Groq's clamps on every provider

`resolve_profile` built the pipeline LLM's options as `{**groq_llm, ...}` — so
selecting Google or OpenAI still inherited Groq's `max_completion_tokens: 300`,
`max_retries`, and `parallel_tool_calls`. Those exist to keep a voice turn inside
*Groq's* rate limit; they are not generic LLM settings. They are now applied only
when Groq is the chosen provider.

This is also why the log reported `response_token_limit: 300` for a Google model:
the number was read from Groq's options for a provider that never had it.

Google's own warning on `max_output_tokens` is worth keeping in mind for any
future tuning: it counts thought tokens, and a cap low enough to be hit while
reasoning returns **truncated or empty** output. Lower the thinking level instead
of the cap.

## Every model declares what it accepts

The thinking-level bug was the third of its kind, and that is the finding worth
recording. Each time, the same two facts were re-derived somewhere new:

| Where | What it knew |
| --- | --- |
| `pipeline_factory` | `if stage.provider == "google"` and a hardcoded `"minimal"` |
| `validation` | a rule for Google, written once the bug was reported |
| `providers.py` | the actual per-model level sets |

The project's own rule says *"no provider name is hardcoded in JSX; adding a
provider is a Python change only"* — and the factory quietly contradicted it. A
provider added tomorrow would have hit the same wall, because the knowledge was
in three places and only one of them was a catalog.

**`OptionDefinition` moves it into one.** Every model declares the settings it
accepts and the values each allows:

```python
OptionDefinition(
    name="thinking_level",
    values=(),                      # filled in from the model's own levels
    nest="thinking_config",          # this provider takes it inside an object
    companions=("include_thoughts",),
    notes="How much the model reasons before answering. Lower is faster.",
)
```

Three things follow from that, and none of them mention a provider:

- **The factory has no branches.** `_build_llm`, `_build_stt`, `_build_tts`,
  `_build_vad` and `_build_realtime` all call `_declared_options`, which asks the
  catalog what to forward. The realtime builder resolves its plugin from the
  stage too, so a second realtime provider is a catalog entry and a plugin.
- **Validation is one rule, not one rule per provider.** `stage.option_unsupported`
  blocks a value a model does not accept, whatever the provider.
  `stage.option_ignored` warns about a setting the model drops, so a value that
  would be silently discarded is at least visible.
- **The frontend can offer the right control.** `options` ships in the catalog, so
  `ModelOptions` renders a select of the legal values for whatever model is
  selected — and shows nothing at all for a model that takes no settings.

`nest` is what makes this general rather than Google-shaped. A provider that takes
`reasoning={"thinking_level": ...}` instead of `thinking_config={...}` is honoured
as declared, which is what the synthetic-provider tests check.

Two supporting decisions:

- **A companion is private to its group.** `include_thoughts` rides inside
  `thinking_config` and is never also a top-level option. Declaring it twice is
  how it reached `RealtimeModel(...)`, which does not accept it, and broke the
  realtime session. A test asserts no companion is also a standalone option.
- **Session wiring is named separately.** `silence_duration_ms`,
  `connect_max_retry` and `connect_timeout` are read by the factory, not passed
  to a plugin, so they are in `SESSION_OPTIONS` rather than a model's options.
  Declaring them as model options would pass them to a constructor that rejects
  them; leaving them undeclared would report them as ignored on every save.

An **uncatalogued** model gets its settings verbatim. Refusing to build would be
worse than trying — a model added to a provider without a catalog update should
still work, and the provider will complain far more clearly than we could.

### What is still hand-maintained

The catalog is a curated, offline description, not a live query. Google, Groq and
Cartesia do not share an API for "list your models and their options", so the
facts are transcribed and guarded rather than fetched:

- every realtime model is asserted to be in the installed plugin's
  `KNOWN_GEMINI_API_MODELS`;
- a thinking option exists **iff** the model declares levels, and carries that
  model's own values;
- a synthetic provider with an invented option shape is built and validated with
  no factory or validator code of its own.

So a new model is a catalog edit that the suite checks against the runtime, rather
than a web request. If a provider ever does publish a capability endpoint, this
catalog becomes its cache and the transcription stops.

## Phase 6: proving a credential works

Phase 3's Providers page could say a key was **stored**, because the OS keyring
said so. It could not say the key was **good**. Nothing in the tree had ever made
an authenticated request, so a revoked key, a key pasted with a stray character,
and a working key were indistinguishable.

### The endpoints were verified, not looked up

This is the part that changed the design. Documentation rarely states the
*failure* shape, and the failure shape is what the whole feature is about:

| Provider | Endpoint | Valid key | Invalid key |
| --- | --- | --- | --- |
| google | `GET /v1beta/models` | 200 | **400** "API key not valid" |
| groq | `GET /openai/v1/models` | 200 | 401 "Invalid API Key" |
| cartesia | `GET /voices` + `Cartesia-Version` header | 200 | 401 "must be logged in" |
| openai | `GET /v1/models` | — | 401 |
| livekit | `POST /twirp/livekit.RoomService/ListRooms`, JWT minted from all three values | 200 | 401 |
| silero | local, needs no key | not probeable | |

LiveKit's row was "not probeable" when this table was first written, and that gap
is the one that mattered: it is the credential that decides whether a session can
happen at all, and the page said so about the one provider that could not be
checked. It became probeable only once credentials were addressed by slot — see
[The LiveKit probe](#the-livekit-probe).

Three findings that documentation would not have given:

1. **Google answers an invalid key with 400, not 401.** A check that treated "any
   4xx" as a bad key would have been accidentally right for Groq and Cartesia and
   accidentally *wrong* for Google in the other direction — a 400 from a malformed
   request of ours would have been reported as a bad credential.
2. **Cartesia has no `/v2` path prefix.** The API version travels in a
   `Cartesia-Version` header, taken from the installed plugin's own constants.
   Every `/v2/...` guess returned 404 *before authentication*, so the bad-key test
   404'd too — a path error that looks exactly like a working key.
3. **LiveKit is not probeable this way.** Its twirp endpoint answered 401 with the
   *real* credentials, so the check cannot be built without minting a proper token.
   It is declared with no probe, and the page says so rather than implying success.

### Three-valued verdict

`ProbeDefinition.invalid_status` lists only the statuses that genuinely mean the
credential is at fault, per provider. Everything else is **inconclusive**:

```json
{"ok": false, "verdict": "inconclusive", "status": 404,
 "detail": "Unknown request URL: GET /openai/v1/nope. The provider answered 404,
            which does not indicate a credential problem. This test could not
            conclude either way."}
```

A rejected key, a provider outage, and a malformed request of ours are three
different things and only the first is the user's problem. Collapsing them would
send someone to re-enter a working credential because their provider was down —
the same class of harm as the config-file mismatch in Phase 3. The reassurance is
part of the message rather than something the caller has to remember, and the
Rust side treats an unrecognised verdict as inconclusive too: parsing a shape it
does not understand must never read as `valid`.

### The secret's path

`credential_probe.rs` reads the keyring, puts the secret in the environment of a
one-shot `provider_probe.py`, and reads back a verdict. It is never an argument
(arguments appear in the process list), never logged, never returned to the
webview, never written to disk. A key in a URL was rejected outright — URLs land
in access logs — and `catalog_issues()` refuses any probe that is not https, names
no auth header, or lists no invalid status.

`ProbeOutcome` has no field a key could occupy, and a test serialises one to prove
the JSON cannot carry a secret. A separate test classifies every string inside a
probe: the URL is https, the header is name-shaped, the prefix is `Bearer ` or
empty, and fixed header values match `^[A-Za-z0-9._-]{1,32}$` — a version, never a
token.

### A test that was quietly wrong

`test_describe_reports_the_selected_pipeline` asserted on whatever
`validate_config.py --describe` returned, and expected the environment profile. It
had only ever passed because no saved document existed. The Phase 3 path fix made
the command find the desktop app's real file, so the test began reading the
developer's own saved profiles. The path fix was correct; the test was
environment-dependent. It now points `LUMINE_CONFIG_PATH` at a path that does not
exist, so it tests what it means to test.

## A hazard the smoke test found: import shape

The first live voice session after Phase 3 crashed:

```
ImportError: attempted relative import with no known parent package
  agent/config_store.py, in resolve_profile
```

`config_store.py` imported `session_preferences` **inside the function body**
with a leading dot. Every other module in this repo puts such imports at module
scope inside a `try: from .x import` / `except ImportError: from x import`
pair, because the worker is loaded two different ways:

| How it is loaded | Import form | Relative import works? |
| --- | --- | --- |
| `python agent/agent.py` (the worker, and the Tauri helpers) | top-level modules | only via the `except` fallback |
| `from agent.config_store import ...` (every test) | package modules | yes |

**Every unit test passed and the worker still crashed**, because all 210 tests
took the package path. A function-level relative import is invisible to the
entire suite.

Two guards now exist, in `agent/tests/test_import_shapes.py`:

1. Every module is imported in a subprocess with `agent/` on `sys.path` and no
   parent package — the worker's actual path.
2. A structural `ast` check that fails on *any* relative import inside a function
   or method body, across every `agent/*.py`, and points at the module-scope
   pattern instead.

The second one is the cheap one; it would have caught this at commit time. Rule
for the rest of this repo: **no relative imports below module scope, ever.**

## What is deliberately not built yet

* No `ProviderConnection` object, no live model refresh, no embeddings or image
  capabilities. The `Capability` union is open-ended so these can be added
  without rewriting every provider, but nothing speculative is in the tree.
* Credential changes need a worker restart. The worker is a persistent LiveKit
  process; re-reading the keyring per job would mean a round trip on every voice
  session. The *test* does not have that problem — it is a one-shot child.
* The `providers` map in the document is now read: the setup gate derives which
  credentials the active profile requires from it, so a profile that swaps its
  speech synthesizer is asked for that provider's key rather than the old one.
  Per-provider *opt-out* still does not exist; `enabled` is not consulted.
* No bundled local model. The `local` id stays reserved and undefined, because
  shipping weights in the app is a packaging decision nobody has taken. `ollama`
  is the honest version of the same idea: a server the user started.
* The camera and screenshare controls are a **local preview only**.
  `agent.py` has no video input — no `RoomInputOptions(video=...)`, no frame
  handler — so nothing is published and the frames go nowhere. The self-view is
  labelled for what it is, and `inputModalities` is what stops the control
  offering itself to a stack that could not use the result. When the agent gains
  video input, the change is to publish what `useLocalMedia` already captures.
* The frontend has no listener for the worker's own `LUMINE_EVENT` stream beyond
  the two records it acts on. `lib/agentRuntime.ts` passes the whole stream
  through, so nothing is lost, but per-turn records are not yet rendered.
* `src/pages/tools/Tools.tsx` is a zero-byte file nothing imports. It was left
  alone deliberately: it is ambiguous enough that deleting it might have removed
  something somebody meant to fill in.
* A packaged build must resolve the Python worker as a sidecar rather than
  through the repository path that `python_env.rs` currently assumes.

## Known third-party warnings

Both are upstream and not fixable here, but they will become upgrade blockers:

* `google-genai` uses `typing.Union`, deprecated in Python 3.14 (removal 3.17).
* `livekit/rtc` uses `asyncio.iscoroutinefunction`, deprecated in 3.16.

`agent`'s own modules import cleanly under `-W error::DeprecationWarning`.

## Layout

```
agent/providers.py                     catalog: providers, models, voices, capabilities
agent/provider_catalog.py              CLI: redacted JSON + --check self-check
agent/provider_probe.py                CLI: one authenticated request per provider
agent/tool_catalog.py                  CLI: the tool registry as a redacted grid
agent/local_models.py                  CLI: discover a locally hosted inference server
agent/multisession_check.py            CLI: three sessions against one running worker
agent/broken_stdout_check.py           CLI: a worker whose stdout is a dead pipe
agent/validation.py                    pure validation rules -> diagnostics
agent/config_store.py                  precedence: saved document > agent/.env > defaults
agent/validate_config.py               configuration CLI: --describe and validate
agent/session_preferences.py           per-job metadata -> JobPreferences (mode + profile ref)
agent/pipeline_config.py               cartesia_tts_settings() (model no longer hardcoded)
agent/pipeline_factory.py              capability-driven builders; _PLUGIN_ALIASES
agent/llm_errors.py                    status + message -> a named, sayable failure
agent/failure_gate.py                  consecutive-failure circuit; the spoken apology
agent/context_trim.py                  chat context trimmed to an item budget
agent/prompts/persona_core.md          the always-sent persona (short)
agent/prompts/persona.md               the full persona, read on demand
agent/tools/tool_results.py            one ceiling, one payload shape, one injection guard
agent/tools/persona.py                 recall_persona: section lookup by topic
agent/tests/test_providers.py          catalog integrity + golden-config consistency
agent/tests/test_validation.py         every rule above, both directions
agent/tests/test_config_store.py       precedence, env profile, job metadata, deprecation guard
agent/tests/test_pipeline_capabilities.py  independent stage selection, half-cascade gating
agent/tests/test_config_wiring.py      saved-config governance and environment fallback
agent/tests/test_local_models.py       root derivation, discovery outcomes, exit-code contract
agent/tests/test_env_example.py        the shipped file agrees with the code that reads it
agent/tests/test_import_shapes.py      worker import path + no relative imports in functions
agent/tests/test_resilience.py         failure classification, circuit, tool budgets
agent/tests/test_persona_split.py      the core is small AND the full persona survives
agent/tests/fixtures/                  golden config, validated by the suite
lumine-ui/src/components/ui/           dropdown, disclosure, knob, hint, toast, bubble
lumine-ui/src/components/avatar/       engine, expressions, montage, useAvatarMontage
lumine-ui/src/features/settings/       TS mirror of the contracts, IPC client, state hook
lumine-ui/src/features/settings/profileOps.ts  pure profile operations (kind conversion, defaults)
lumine-ui/src/lib/agentRuntime.ts      the worker event contract, and the hook over it
lumine-ui/src/lib/errors.ts            a raw failure string -> a cause and a next step
lumine-ui/src/pages/settings/          settings shell, nav, and pages
lumine-ui/scripts/                    check:profile-ops, check:conversion, check:tool-toast, the TS resolver hook
lumine-ui/src-tauri/src/settings_store.rs   versioned file, atomic write, .bak
lumine-ui/src-tauri/src/credentials.rs       OS keyring, slot-addressed, write-only surface
lumine-ui/src-tauri/src/credential_probe.rs  the secret's path: keyring -> env -> one request
lumine-ui/src-tauri/src/credential_injection.rs  every variable, from its own slot
lumine-ui/src-tauri/src/setup_status.rs  can this install hold a conversation?
lumine-ui/src-tauri/src/python_env.rs        shared interpreter/agent-dir resolution
```

## Verification

| Check | Result |
| --- | --- |
| `python -m unittest discover -s agent\tests` | 488 passed |
| `cargo test --lib` (src-tauri) | 70 passed |
| `cargo check` | zero warnings |
| `npm run build` (`tsc && vite build`) | clean |
| `npm run check:profile-ops` | passed |
| `npm run check:conversion` | passed |
| `npm run check:tool-toast` | passed |
| `python agent/provider_catalog.py --check` | ok (version 12) |
| `python agent/tool_catalog.py --check` | ok (5 tools) |
| `python agent/validate_config.py --describe` | reports the effective source and path |
| `python agent/multisession_check.py` | 3/3 sessions on one worker |
| `python agent/broken_stdout_check.py` | the job starts with stdout on a dead pipe |
| rendered-page border audit | zero CSS borders outside the avatar's presence rings |

### Checking the TypeScript side

The frontend has no test runner and the project rule is not to add one for
convenience. Three checks run against the real source instead, because Node can
strip type annotations itself. `scripts/ts-extension-resolver.mjs` teaches Node
the extensionless imports Vite resolves, so nothing is copied or duplicated:

| Script | What it proves |
| --- | --- |
| `check:profile-ops` | `convertProfileKind` produces a complete, valid profile in both directions, the interruption mode survives a round trip, a chosen name is not mistaken for an environment label, and the catalog's voice lists are duplicate-free. |
| `check:conversion` | The document the settings screen builds is accepted by `agent/validate_config.py`, and loading it through `LUMINE_CONFIG_PATH` — the same variable `agent_manager.rs` sets — reports `source: "ui"` with the selection intact. |
| `check:tool-toast` | One tool call produces at most one toast, one line long. A named field beats a shorter unnamed one, an 800-character retrieved page is cut, the untrusted-content wrapper is never shown to anyone, a success with nothing to say says nothing at all, and a failure with no reason still reads as a sentence. |

`check:conversion` exists because the seed values are chosen in TypeScript and
judged in Python, and nothing else in the build puts those two halves in the same
room. It has already earned its place: it is what showed that `--describe` reads
the *active* configuration and ignores its positional argument, so a check
pointing at a document by path was silently validating the environment profile
instead.

`scripts/` is inside the `tsconfig.json` include list, so `npm run build` typechecks
these files. That is how three real type errors in them were found.

## The token budget

The provider tiers Lumine runs on are metered in tokens per **minute**, not in
requests: Groq's free chat models allow 8,000 TPM. A voice turn therefore has a
hard ceiling on how much it may carry, and three things were quietly spending it
before the conversation even started.

| Cost | Before | After |
| --- | --- | --- |
| System prompt, every request | ~3,200 tokens | ~1,450 tokens |
| A news lookup, and every turn after it | 1,200 chars of prose | 3 headlines |
| A web search | 1,600 chars including snippets | titles and links |
| A conversation | grew until the provider refused it | trimmed at 40 items |

### The persona is split, not rewritten

`agent/prompts/persona.md` was 566 lines and about 2,700 tokens, prepended to every
single request. Splitting it in two:

- `agent/prompts/persona_core.md` — what every reply is shaped by. Always sent.
- `agent/prompts/persona.md` — **unchanged**, and read on demand by the
  `recall_persona` tool.

The full text is byte-for-byte what it was. `test_persona_split.py` asserts both
halves of that: the core must stay under half the original size, and the original
must still be on disk with its sections intact. Without the second assertion the
optimisation could quietly become a deletion, and nothing else would notice.

A model that has been told its own instructions are available on request uses them
far less often than one carrying them all the time — so the saving is larger than
the file-size difference suggests.

`recall_persona` matches on heading words, then on a small table of phrased intents
("how do you show happiness" → *Emotional Awareness*), with question words
filtered out first. Matching on "what" would send a question about religion to a
section titled *What Makes Her Feel Human*.

### Tool results are compressed at the source

A tool result is not a one-time cost. It joins the conversation and is re-sent on
every later turn, so a generous return is paid for repeatedly, for the rest of the
session. `agent/tools/tool_results.py` is the single place that decides:

- a **central ceiling** (`MAX_RESULT_CHARS`, 800), so a new tool cannot regress past
  it,
- **keyed JSON rather than prose**, because the model should not have to parse a
  sentence to find the third headline,
- a **truncation marker**, because a model told a list is complete will answer as
  though it is,
- and a **delimiter** around anything retrieved from the internet, labelled as data
  with an instruction not to obey it.

That last one is not theoretical. `search_web` and `get_news` read arbitrary
scraped text, and a page containing "ignore your instructions" is a real attack
rather than a hypothetical. The tool policy states the rule as well, because a
delimiter the model was never told about is just punctuation.

### The context is trimmed as a backstop

`agent/context_trim.py` calls `ChatContext.truncate(max_items=...)` on
`conversation_item_added`, which keeps the last N items, drops leading orphaned
function calls, and — the part that matters — **puts the system message back**.
Losing it mid-conversation would leave a model with no personality and no tool
policy.

Trimming is explicitly the fallback, not the strategy. It normally does nothing;
it exists so a long session degrades by forgetting old turns rather than by
failing. The floor (`MIN_ITEMS_BEFORE_TRIM`, 24) is there because trimming a short
conversation throws away context the model can still use.

`LUMINE_MAX_CONTEXT_ITEMS` overrides the cap, clamped to 8–400.

### The model that fits the tier

Groq withdrew `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` from the free
and developer tiers in August 2026. Both are marked `deprecated` in the catalog
with a `replaces` pointer, so a saved profile using one explains itself rather than
failing mysteriously.

`openai/gpt-oss-20b` stays the default: it is the fastest text model on Groq, the
cheapest, and it supports **prompt caching**, under which cached tokens do not count
toward the allowance. A byte-stable system prompt is therefore exempt from the TPM
ceiling once warm — which is the main reason the persona split is worth doing
carefully rather than approximately. `openai/gpt-oss-120b` was added as the
same-allowance alternative for a harder question.

## When a turn fails

The specific failure this fixes: the LLM request fails, nothing is spoken, and the
user cannot tell a rate limit from a muted microphone. A session that has hit a
rate limit also keeps trying, which burns the little quota that remains and makes
recovery slower.

### Naming the failure

`agent/llm_errors.py` classifies by status code and message into
`rate_limited`, `quota_exhausted`, `context_length`, `auth`, `provider_down`, or
`unknown`. The distinction that earns its keep is the first two: **both arrive as
HTTP 429**, and only the message separates "wait a moment" from "come back
tomorrow". Telling someone to retry shortly when their daily allowance is gone
sends them away to wait for something that will not help.

Two rules keep it from lying:

- a 400/404/405/422 is a malformed request of ours *whatever the body claims* —
  verified against Google, which answers an invalid key with 400 and a message, so
  believing the body would blame the user's quota for our mistake;
- an unrecognised failure is `unknown`, never a limit.

`classify()` cannot raise. It is called from an error handler, where raising would
turn a provider error into a crash inside the recovery path.

### Speaking, toasting, and stopping

`agent/failure_gate.py` counts consecutive failures and opens a circuit after three
(45s cooldown, injected clock so the tests do not sleep). It is deliberately **not**
a retry loop: backing off and telling the user is useful, hammering a rate-limited
endpoint is how an allowance is spent on requests that cannot succeed.

On the third consecutive failure, Lumine says one short sentence —
`"Sorry, I'm having trouble right now. Give me a moment."` — and only once per
opening, so a retry storm does not talk over itself. It goes through `session.say`
with a `SpeechHandle` rather than the LLM, because the LLM is what just failed and
asking it to explain its own failure is how a turn turns into silence.

In the app, `agent.py` publishes the classified failure on the `lumine.notice` data
channel as well as stdout, because a notice that depends on how the process was
started is a notice that silently does not arrive. `voice-manager.ts` parses it and
`Home.tsx` raises a toast: a **warning** titled "Request limit reached" for a
limit, an **error** for anything else. Showing both as a red error would train the
user to ignore the one that matters. Duplicates within 15s are dropped, or an open
circuit would produce one identical toast per failed turn.

The provider's own words travel in `providerDetail` for a diagnostics view and are
never spoken or displayed — a raw provider error can carry an account id, which is
fine in a log and noise in a toast.

### One more token cap

`DEFAULT_MAX_COMPLETION_TOKENS` was 300. GPT-OSS is a **reasoning** model: its
thinking is drawn from the same budget, and a cap generous enough to think with
leaves nothing to say with. That is how a turn produces a perfect internal monologue
and no audio at all. It is now 900, and `reasoning_effort` defaults to `low` —
a voice turn should not pay latency and tokens for reasoning the user will never
hear. Both are overridable per profile, and `reasoning_effort` is declared as a
Groq option in the catalog, so the settings screen offers it rather than a hidden
constant.

## What a tool result is allowed to show

The transcript accumulates and a toast does not, so the two get different content.

`tool_status` records carry `summary` — a status, computed from the shape of the
result ("Got 3 headlines", "Weather for Dhaka") — and `result`, the payload itself.
Only `result` is shown, and only on the toast. `ConversationToolEvent` has no
`payload` field at all, which is the enforcement: there is no path by which the
payload reaches the transcript even by accident.

`useLumineVoice` delivers the payload through a separate `onToolResult` callback
rather than a wider event, for the same reason.

This matters beyond tidiness. A `LUMINE_TOOL_DATA` block sitting inline in a
conversation reads as though the model had been handed it by the user, which is
exactly the shape an injection wants. Keeping it out of the rendered transcript
means the defence in `tool_results.py` is not being undermined by the UI.

## The app now starts the worker

`voice-manager.ts` calls `start_agent` and then `wait_for_agent_worker` before
dispatching. Previously the voice path invoked no lifecycle command at all, so
voice silently depended on a Python process someone remembered to start by hand,
and a saved settings change did nothing until it was restarted by hand too.

`start_agent` is idempotent — it returns the existing status if the managed child
is alive — and it hands the child the app's config path plus any keyring
credentials, so the worker reads the settings the UI is showing. Readiness is a
separate, bounded step because the worker has to import the SDK and register with
the server before a dispatch finds it, and dispatching into a room nobody is
listening for produces a room that sits silently connected.

A readiness timeout is **logged, not fatal**: a worker started outside Tauri is
invisible to Rust and can serve the room perfectly well. The agent-join timeout
further down is the real backstop, and its message is the one the user needs.

**Caveat worth knowing:** a worker already running from `lk agent dev` is not
visible to Rust, so the two will briefly coexist. LiveKit dispatches a room to a
single agent, so the second idles. Stop the manual one when testing settings, or the
worker that answers may not be the one holding your saved config.

## Does one worker serve many sessions?

A reported symptom was "the agent joined the first session, then never joined
another, and only restarting the server helped". That was tested rather than
reasoned about, with `agent/multisession_check.py`, which dispatches three
sequential sessions at one already-running worker.

**It works.** Eight consecutive sessions ran on a single worker process, each
producing `session_started` and a clean `job exiting`. The worker is a persistent
`AgentServer` and handles many rooms; that part of the design is sound.

The first version of that check reported a false failure by polling
`list_participants` for the agent. An agent participant is not always listed the
instant it starts, so the poll timed out while the worker log plainly showed the
session running. The check now asserts on the worker's own `session_started`
record — the event the app's agent-join timeout is really waiting for.

If the symptom is ever seen again, the cause is almost certainly a worker that
**died** rather than one that cannot multitask: before this phase nothing called
`start_agent`, so a dead worker stayed dead and the only cure was a manual
restart. It is now respawned automatically, because `start_agent` calls
`sync_if_exited` on every session start.

The one frontend shape that could still produce "the button does nothing and only a
restart helps" was a silent no-op: `start()` returned early when the operation lock
was held, with no log and no user feedback. It now logs when it refuses, and treats
a lock held past 90s as stuck and clears it. That threshold is longer than the
longest thing a connect waits for (20s worker spawn, 30s readiness, 15s agent join,
in sequence), so a slow connect is never mistaken for a hung one.

## A model your key cannot reach is not an outage

Found by the multi-session run above, and worth recording because the classifier
got it wrong in a way that would have cost real diagnosis time.

Groq answers a model that does not exist, or that the key cannot reach, with a
generic **404**:

```
The model `llama-3.3-70b-versatile` does not exist or you do not have access to it.
code: model_not_found
```

That was classified as `provider_down` with `retryable: true` and the message *"I
couldn't reach my thinking service. Try that again?"* — wrong three ways. The
provider is healthy and answering. Retrying fails identically forever. And the
advice sends the user to debug their network instead of their settings.

`model_unavailable` is now its own kind: non-retryable, not a limit, and the
message points at Settings. It is matched on the **body**, before any status-code
rule, because the status is genuinely ambiguous — 404 covers both "this model is
gone" and "your key may not have access", and reporting either as an outage
misleads. A 404 with nothing about a model in it is still treated as our own bug.

`test_resilience.py` pins this against the verbatim error captured from the live
response, because a classifier that has been right in tests and wrong in the log is
worse than one that was never written.

## The real cause of "it joined once, then never again"

Not the worker dying. A **telemetry `print()` was ending the session.**

The desktop app pipes the worker's stdout, and a write to a pipe whose read end has
gone away raises `OSError: [Errno 22] Invalid argument` on Windows. The first thing
a job does is emit a latency record:

```text
File "agent/agent.py", line 259, in entrypoint
    latency.mark("entrypoint", room=ctx.room.name)
  File "agent/runtime_events.py", line 42, in emit_record
    print(f"LUMINE_EVENT {encoded}", flush=True)
OSError: [Errno 22] Invalid argument
```

So the session died on its opening line. The agent appeared to join, produced
nothing, and the only recovery was restarting the whole desktop app — which is
exactly the reported symptom. Four fixes, in order of importance:

1. **A telemetry write can never raise.** `write_event_line` catches `OSError` and
   `ValueError` and reports the broken pipe *once*. Losing an event is acceptable;
   losing the session is not.
2. **`logging.raiseExceptions = False`.** LiveKit's own logger was failing on the
   same broken stream, and logging prints a full traceback *per record* — which is
   how a one-line failure ended up buried under thousands of identical ones.
3. **`LIVEKIT_LOG_LEVEL=info` on the child**, unless already set. `agent.py dev` is
   a development command and LiveKit's dev mode turns the root logger to DEBUG,
   which is a lot of records for a desktop app to consume over a fixed-size OS
   buffer. Set through the environment so it composes with `LUMINE_AGENT_ARGS`.
4. **The reader thread only echoes `LUMINE_EVENT` records.** It was `println!`-ing
   every line, taking a lock per line on the one thread responsible for keeping the
   pipe drained.

`agent/broken_stdout_check.py` reproduces the whole thing: it points the child's
stdout at a closed pipe and asserts the job still starts. It deliberately does
*not* assert the session completes — every session signal is a `LUMINE_EVENT`
record, which is the thing under test.

Note the trap that cost an hour here: a worker left registered from a previous test
keeps absorbing dispatches, so the *next* run looks broken when nothing is. Both
check scripts now kill the process tree, because LiveKit's per-job child outlives a
plain terminate of the parent.

### A regression the unit tests could not see

Fixing the above introduced one. A module-level `def write_event_line(...)` was
inserted in the middle of `RuntimeEventPublisher`, which **ended the class body** —
`emit`, `publish_data`, `publish_async` and the rest silently became nested
functions. `publisher.emit` vanished, and every session died with an
`AttributeError` on `LatencyTracker(publisher.emit)`.

Every unit test for the writer passed throughout, because none of them touched the
methods that got swallowed. Only dispatching a real session found it.

`test_stdout_resilience.PublisherSurfaceTests` now asserts the publisher's public
surface, so a truncated class body fails loudly. The guard was verified by
re-injecting the exact regression and confirming it reports:

```text
AssertionError: RuntimeEventPublisher.emit is missing --
  the class body was probably truncated by a top-level definition
```

That is the general lesson: asserting that a function *works* is not the same as
asserting that a class still *has* it.

## The fifth smoke test: the agent never joined the room

Reported as three separate symptoms — LiveKit would not accept the credentials,
the agent never appeared, and the voice test did nothing. One cause.

### Three values, one slot

`credentials.rs` stored one secret per provider id, and `credential_injection.rs`
wrote it into every variable that provider declared:

```rust
let secret = store.secret_for_worker(&provider_id)?;   // one value
for name in names {
    command.env(name, secret.trim());                  // into all of them
}
```

A user pasting their LiveKit API key got all three variables set to it. The
worker could not register with LiveKit, `wait_for_agent_worker` timed out after
20 s, and no session ever started. The reported "LiveKit rejects the credentials"
was accurate and misleading at once: the values really were rejected, because one
of them was an API key in the field meant for a hostname.

### Why three separate screens stayed quiet

This is the part worth keeping. Every layer that could have caught it was built
on "does a value exist for this provider?", which one pasted value satisfies:

- **Providers** said "Stored", because the keyring reported one entry.
- **Test** said "Not testable", because `_PROBES` had no LiveKit entry at all.
- **The setup gate** said ready, because `is_satisfied()` was
  `local || stored || in_env`.

Not one of them was lying. Each was answering a question that could not have the
answer. The fix was not to correct the wording; it was to make the credential
expressible — three slots, three fields, and a gate that requires all three.

### Verified both ways

The regression is pinned from three sides, because the interesting failure is
silent:

- `credentials.rs` — three slots hold three different values, and a multi-slot
  provider **never** falls back to the legacy bare entry.
- `credential_injection.rs` — `LIVEKIT_URL` != `LIVEKIT_API_KEY` in the child's
  environment, and a partly-filled provider leaves the untyped variable unset
  rather than copying a sibling into it.
- `provider_probe.py` — against a local server, a minted JWT reaches
  `ListRooms`; 401 is a rejected credential and 503 is not.

And against the real project on this checkout:

```text
livekit: {"ok": true,  "verdict": "valid", "status": 200, "latencyMs": 2274}
```

The same three variables deliberately collapsed to one value — reproducing the
old injection exactly — is now reported rather than suffered:

```text
{"ok": false, "verdict": "rejected",
 "detail": "LIVEKIT_URL is not a server address. It should start with https:// or wss://."}
```

That second line is the actual deliverable. The bug was never that the
credential was wrong; it was that a wrong credential produced silence, and
silence is indistinguishable from a broken microphone.

## Phase B: one picker, and settings that route

The settings screen had grown four different choosers. A native `<select>` (whose
popup is drawn by the OS, so a warm low-contrast screen got a hard white
rectangle the moment anyone opened it), a `<datalist>`, a segmented button row, and
a wall of cards for typefaces. Four settings, four applications.

They are all `components/ui/dropdown.tsx` now — base-ui's listbox with Lumine's
surfaces, so the keyboard model and typeahead are free and the popup belongs to the
app. It grew three capabilities to get there:

| Addition | Why it was needed |
| --- | --- |
| `editable` | A trailing row swaps the trigger for a text field. Providers publish far more voices than any curated list, and a cloned voice has no name that could be listed, so a dropdown that only offered known ids could not represent a legal value. |
| `group` | `provider/model` in one control, split on the **first** slash — model ids contain slashes, so a `split("/")` pair would shred `openai/gpt-oss-20b`. |
| `face` | A CSS `font-family` per row, for the typeface picker. Applied as a style and never baked into the label string, so base-ui's typeahead still matches plain text. |

A value with no matching row is appended as "Not in the published list" rather than
rendered empty. A picker that shows a placeholder for a value that is genuinely set is
lying about the thing it exists to report.

`StageSelect` collapsed from two native selects to one. The two selects had also been
inconsistent with each other, which is how a model that did not belong to the chosen
provider could be selected and then refused at save time.

## Phase C: the settings rail, rebuilt

```
Appearance · Voice · Models · Providers · Diagnostics
```

`Voice` is the profile, the voice, and the active card. `Models` carries a sub-tab
per stage — `stt / llm / tts / vad` — because a tab that mixes four providers' worth
of settings is not a tab. Configuration checks moved to Diagnostics, where a blocking
result already means something.

Three implementation decisions, each of which fixed a real defect rather than
tidying a preference:

- **`SettingsRoute` is one object.** `{ section, tab }` as two pieces of state let a
  section be shown with a tab it does not have. `normaliseRoute()` makes that
  unrepresentable, and it is the reason the `AnimatePresence` key is
  `` `${section}:${tab}` ``.
- **One `Hint` for the whole tab strip.** Five `?` affordances, one per tab, is five
  tooltips to read and nothing to read them from. The content pane is a flex column
  with its own scroller, so the strip stays put while the pane scrolls.
- **The save bar is a sticky footer, not a layout sibling.** `position: sticky;
  bottom: 0` with a `::before` bleed, and `.settings-page` gained `min-height: 100%`
  so `margin-top: auto` can hold it to the bottom. It was previously in normal flow,
  which meant it scrolled away on a long stage and the screen had no visible save.

Appearance was rebuilt on the same primitives: colour mode is a dropdown, the two
typefaces are one list drawn in their own faces, and the bubble-style picker is a
dropdown beside a **live preview** drawn with the real `Bubble` components. A
bubble-style choice that shows you a generic rectangle is a choice you are making
about a rectangle. The twelve colour inputs and the palette importer sit behind
separate disclosures, and "Custom colours" counts how many differ from the mode's
own palette — a count rather than a dot, because "3 set" is answerable and
"something is in here" is not.

## Phase D: `advanced`, declared in the catalog

Every stage lists what its model accepts. The list is longer than the stage is
interesting: speed, voice and thinking level are what somebody came to change;
temperature, a token cap and a top-p are not — they are correct at the provider's
default and wrong in ways that are hard to trace back to a slider.

So the catalog marks each option advanced or not, and the judgement is made there
rather than in the UI, because only the catalog knows whether a `temperature` is
ordinary on one model and meaningless on another.

| Advanced | Never advanced |
| --- | --- |
| `temperature`, `top_p`, `max_completion_tokens`, `parallel_tool_calls`, `volume`, every Silero VAD timing | voice, speed, emotion, language, `thinking_level`, `reasoning_effort` |

Two tests pin the classification in both directions. A test that only checked the
first column would let everything migrate into it.

## Phase E: a call, not a dock

The old bottom control was a microphone button and a mute button side by side — two
controls for one thing in the same state, distinguished only by size. Pressing the
big one during a call ended it. A call has a beginning and an end, and the interface
shows that now:

| State | Controls |
| --- | --- |
| Idle | One circular handset, centred. `is-blocked` is a neutral fill with no accent halo, so "you cannot call" never looks like "calling". |
| Connecting / ending | The same handset with a spinner. |
| Live | Mute, camera, screenshare, then end call in red **last**. A row of four identical circles leaves the destructive one to be found by hovering. |

The timer is counted from the session's own `startedAt`, not from when the component
mounted, so a reconnect does not reset a call to zero. It is the only text in the
bar, because a call's length is the one number somebody actually wants from it.

`Icon` gained a `weight` prop. The start and end handsets are `fill`; the toolbar
handsets are not. A handset rather than a square, because the control starts a
conversation rather than toggling a state — and the end-call mark is the same
handset dropped, which is what makes the pair readable without a label.

## Phase F: what each model can actually do

`ModelDefinition.input_modalities`, defaulted from `CAPABILITY_MODALITIES` and
published as `inputModalities`. All four Gemini Live models declare
`(text, audio, image, video)`; `transport` declares none.

The claim is narrower than it looks, and the Diagnostics matrix says so on screen:
a dot means **Lumine can hand that model that kind of input on the path it is reached
by** — not that the model would accept it in principle. The language stage sends a
chat history of strings, so a camera turned on beside a pipeline stage delivers
frames to nobody. Only the Live models have somewhere to put one, and that is what
`canReceiveVideo` in `Home.tsx` reads.

The Diagnostics page's vague "Catalog" block is now a sorted matrix with a
retired-model toggle and a "N of M can see" summary. Every `<p>` under a heading
became a `Hint`, because a diagnostics screen that answers in paragraphs cannot be
scanned.

## Phase G: local models, and the value a plugin demands

The `ollama` provider: `requires_key=False`, `local=True`, `key_env=()`, and four
seed models offered as "things to offer before anyone has opened Discovery".

`max_completion_tokens` is **deliberately absent** from its options. OpenAI's newer
parameter names are rejected keywords, and a rejected keyword is a 400 on the first
spoken turn — which is the exact failure mode this whole document keeps running
into.

The `api_key` problem is the interesting one. Ollama's OpenAI-compatible server has
no authentication, but the OpenAI client refuses to construct without a key, so Lumine
sends the literal string `"ollama"`. Three things had to be true for that not to
reach a person:

1. The catalog publishes an explicit `default` for an option the profile does not
   set, and `build_options` applies it.
2. The catalog publishes `hidden`, and `ModelOptions` filters hidden **before** the
   advanced split. A mandatory value nobody can usefully change is not a setting.
3. `("ollama", "llm")` is an ordinary catalog entry, so nothing about it is special
   in Python, in Rust, or in the UI.

Wiring it needed no factory branch. `_PLUGIN_ALIASES = {"ollama": "openai"}` in
`pipeline_factory.py`, and `require_module` names the **package**
(`livekit-plugins-openai`) rather than the provider, so a missing plugin is not
reported as "install livekit-plugins-ollama", which does not exist.

`local` stays in `RESERVED_PROVIDER_IDS`, and the comment says why: it is reserved
for inference whose weights ship **with the app**. `ollama` promises a server you
started. Shipping the first under the second's name would be a promise the app
cannot keep.

Discovery is a button, not an automatic list — `agent/local_models.py`, run as a
one-shot by the Tauri command `discover_local_models`. A network round trip on a tab
somebody may have come to for something else, against a server that may not be
running, is not a list. Three details in it are worth recording:

- **It always exits 0.** The desktop helper discards stdout on a non-zero exit, so
  the one message worth reading would be thrown away. The verdict lives in `ok`.
- **Reachable-but-empty is not unreachable.** Those are different problems with
  different fixes, and a single boolean cannot say which one happened.
- **Root stripping is longest-suffix-first.** `/v1` matched the tail of `/api/v1` and
  left `http://host/api`, which then 404s on `/api/tags`. A test caught it.

## Phase H: the avatar moves on its own

`components/avatar/idleMontage.ts` already had a weighted fourteen-animation
montage, and **nothing outside the lab called it**. `Presence.tsx` called
`engine.connect()`, which played one `blink` and stopped. So the home screen was a
still image, and the lab — which had its own private montage switch — looked
correct. The lab was the only place the feature was visible, which is the worst
possible place for it to be visible.

`useAvatarMontage.ts` is a queue driver, not a `play()` call. Each tick awaits the
current animation and then waits a per-state gap, so timing is self-adjusting rather
than assuming an animation lasts its nominal duration. Two yields:

- **A held reaction.** A priority-0 gesture cancelled mid-tween by a priority-2
  reaction leaves motion half-applied, so `paused` is true while a reaction is set.
- **Cursor gaze.** Not by switching the montage off, which would freeze the face for
  anyone who left gaze on — it *filters* the five gaze-touching animations and keeps
  body movement running.

`Presence.tsx` now holds `engine` in state as well as a ref, because the hook must
restart when the avatar connects and cannot see a ref change. The Avatar Lab was
rebuilt on the same driver, so the two can no longer disagree about what "the montage"
is. Its twenty-six emotions remain a wall of chips on purpose: a menu hides
twenty-five of them, and comparing expressions is the activity the page exists for.

## What the dead code was hiding

Three files nothing imported, and the reasons were not all the same.

| File | Verdict |
| --- | --- |
| `features/expression-studio/lab.tsx` | Deleted. A second avatar lab. Two avatar labs means two places to change an animation. |
| `hooks/useLumineSession.ts` | Deleted. A duplicate of `useLumineVoice` whose own comment called itself a duplicate. |
| `lib/agentRuntime.ts` | **Wired, not deleted** — see below. |
| `lib/errors.ts` | **Wired, not deleted** — see below. |

`lib/agentRuntime.ts` declared the events as `agent.started` and wrapped each
payload in `{ type, payload }`. Rust emits `agent_started` with the `AgentStatus`
object *as* the payload. Every name was wrong and every shape was wrong, and nothing
noticed for one reason: no component imported it.

Which is the argument for fixing it rather than deleting it. Deleting would have
removed the only description of the contract and left the real gap untouched — the
worker emits these and the app listened to **none** of them, so a worker that died
between two status polls was invisible. That is the same class of silence as the
LiveKit bug, and the `agent/` side of the repository already has a note about it.

It now also draws a distinction worth keeping. `agent_*` is the **desktop layer's**
view: one child process, and whether it is registered. `agent_runtime` is the
**worker's own** stream, forwarded verbatim: the session connected, a tool ran, a
turn failed. Collapsing them is how "the worker is running" gets read as "Lumine is
listening", which is the confusion that made the original bug so hard to see from the
UI. Diagnostics reports `running` and `connected` separately for the same reason —
a process can be alive while its LiveKit registration is missing.

`lib/errors.ts` turns a raw failure string into a title that names the problem and
one sentence that says what to do about it. Every failure in the app now goes through
it, because text is the one thing a person cannot act on: a bare `401 Unauthorized`
tells them nothing they did not already suspect, and the most common cause by a wide
margin is a key that was never right.

## The shipped defaults are now the tested ones

`agent/.env.example` shipped `GROQ_MAX_COMPLETION_TOKENS=300` while the code's
default was 900. On a *reasoning* model the thinking is drawn from the same budget,
so 300 buys a monologue and no audio — a silent turn that presents exactly like a
muted microphone, which is the failure mode this document has now hit three times.

It also shipped `LIVEKIT_API_KEY=your-livekit-api-key` **uncommented**, and the same
placeholder in every credential variable. Every credential check in the product
treats "the variable holds a non-empty string" as "you have this key", because
telling a placeholder from a real key would cost a billable request. So the shipped
file declared credentials nobody had, and the setup gate opened on them. This is the
same shape as the injection bug — a value that reads as something it is not — and it
is guarded the same way: **absence has to be real, so the shipped values are empty
rather than illustrative.** An empty line is an absence *and* leaves somewhere to
paste.

`agent/tests/test_env_example.py` compares the shipped file against the code that
reads it. The credential list is derived from the catalog rather than written out, so
a provider gaining a second variable is covered automatically; and every credential
variable must still be **named** in the file, so the fix cannot be "delete the line".

One more default had two answers. Google draws its voice catalogue from two lists,
and each marked a default, so the merged provider had two — and they disagreed: the
per-model list said `Sulafat`, the merged list said `Zephyr`. The same provider
therefore defaulted to a different voice depending on whether the caller happened to
name a model. `_dedupe_voices` now demotes the later default, the `default` is
declared once, and two tests hold both the rule and the agreement with
`DEFAULT_GEMINI_VOICE`.

The default pipeline is `gemini_live`, so a fresh install needs LiveKit's three
values and Google's one. `test_the_default_pipeline_needs_no_optional_credential`
exists because if that ever moves back to the cascade, the first-run requirement
silently doubles — and nothing else would notice until a user hit it.

## Four reports, and only two of them were bugs

Four things were reported at once. Two were defects in the code and two were
defects in how the code was presented, and the difference matters because the
fixes are not interchangeable.

### The settings overlay drew its own navigation and no content

The rail, the header and the tab strip rendered; the pane under them did not. The
cause was one character in `SettingsDialog.tsx`:

```ts
transition.animate = reduceMotion
  ? { opacity: 1, y: 0 }
  : { opacity: 0, y: 0 };   // the pane rested at invisible
```

`animate` is the frame the element settles on, and it was `0` in the motion branch
only. So the bug was **conditional on `prefers-reduced-motion` being off** - which is
the default path, on every machine that has not asked for reduced motion. That is why
it presented as total rather than intermittent, and why it looked like "the settings
page has no content" rather than "an animation occasionally failed".

The reduced-motion branch was already correct, which is what made it misleading: the
file contains a correct answer to this exact question, one line away from the wrong
one, and nothing about the bug points at the pair.

### "It saved all three keys, and reopening showed three empty boxes"

This one is worth being precise about, because the reported behaviour sounds like a
write path and is not one. `setCredential(provider, secret, slot)` ->
`set_credential` -> `OsCredentialStore.set(provider, slot, secret)` was, and remains,
correctly slot-addressed. Nothing ever wrote three values when one was submitted.

What was broken was everything *around* the write:

- `SetupGate` closed the whole provider group after **one** slot's save, so the panel
  vanished and the other two fields went with it. Saving a server URL looked like
  completing the credential.
- The button read "Replace" off the first slot alone, so a provider holding exactly
  one of three values was labelled as though it were finished.
- Nothing on the page could say which slot was filled. A credential is write-only -
  there is deliberately no command that reads one back - so an empty input beside a
  stored value is genuinely ambiguous from the outside, and "empty" was the only
  thing the UI could draw.

Three fields, no per-field state, a panel that closes on the first save. The user was
reading the UI correctly.

**So the fix was to publish per-slot state, not to change the write path.** Rust
already knew the answer; it was being flattened:

```rust
pub struct SlotStatus {          // new
    pub env: String, pub label: String, pub kind: String, pub help: String,
    pub stored: bool, pub stored_last4: Option<String>, pub in_env: bool,
}
// RequiredCredential.stored is now "any slot stored" - a badge signal, deliberately
// weaker than is_satisfied(), which stays per-slot.
```

`RequiredCredential.stored` used to be read from `key_env.first()`, which is a
different question and gave a confident wrong answer for a three-value credential.
It now means *any* slot is held, and is documented as a badge only: nothing that
decides whether a call can be attempted may read it. `missing.length == 0` decides
that.

Two more facts keep this honest:

- An empty input next to a stored value is not a bug to be papered over. The field
  says `Stored ....3f2a` in words, because that is the only copy of the fact that
  exists anywhere.
- `stored` and `in_env` are different answers. A value in `agent/.env` is not a
  credential the app stored, and the UI names the file rather than dressing it as
  success.

### The Tools page took a very long time and showed nothing

Measured: `python agent/tool_catalog.py` takes **6.36 seconds** cold. The page showed
five grey placeholder cards for that entire window, which is the one thing a page
whose job is "here is what Lumine can do" must not look like.

The catalog is now cached in Rust for the life of the process
(`OnceLock<Mutex<Option<Result<Value, String>>>>`), behind `clear_tool_catalog` as the
deliberate escape hatch. Caching it in Rust rather than in TypeScript is a real
decision: the catalog depends only on `TOOL_CARDS` (on disk) and
`LUMINE_DISABLED_TOOLS` / `LUMINE_ENABLE_APP_LAUNCH`, which are read from *this
process's* environment when the child is spawned, and Tauri never rewrites its own
environment afterwards. So the value cannot go stale within a run. Failures are cached
too, deliberately - a helper that cannot start will not start on the fourth attempt
either, and retrying it six seconds apart is how a broken install becomes a slow one.

The loading state now says what it is waiting for rather than drawing six seconds of
nothing.

## The Tools page is a tab strip, and the tabs are declared in Python

It was a scrolling column of grouped cards, which is a fine layout for two groups and
a bad one for "can she open Spotify" - you had to scroll past the entire web section
to find a heading saying the answer was further down. Settings already solved this,
so the Tools page now uses the **identical control** rather than a second
implementation that would drift within a release.

That control is `components/ui/tabstrip.tsx`, and extracting it exposed a real bug:
`SettingsDialog` had a document-level `ArrowLeft`/`ArrowRight` handler for its strip
*and* a second one on the strip itself. Both fired, so one keypress moved two tabs.
Both landings looked like "the tab changed", so nothing noticed. The strip now owns
its own keys and the overlay owns only the rail's.

The rail is a vertical list and was taking horizontal keys anyway - `ArrowRight`
moved it *up*, because the handler fell through to `index - 1` for anything that was
not `ArrowDown`. It is now `ArrowUp`/`ArrowDown`/`Home`/`End` and nothing else, and
each level only claims focus from within itself, so stepping between sections no
longer dumps you into the tab strip and strands you there.

The categories themselves are declared in `agent/tools/tools_registry.py` and
published, order included:

| Order | Category | Holds |
| --- | --- | --- |
| 1 | `Web` | `get_weather`, `get_news`, `search_web` |
| 2 | `Internal` | `recall_persona` |
| 3 | `Desktop` | `open_app` |

The order is a decision, not alphabetical accident. Web is most of what Lumine can
do and least interesting to distinguish; `Desktop` is the only category that acts on
the machine, so it comes last and a reader is offered the harmless categories before
the one that starts programs.

`--check` rejects a card whose category is not in `TOOL_CATEGORIES`, because the page
derives its tabs from that list: an undeclared category does not error, it *hides the
tool behind a tab that was never drawn*. A declared-but-empty category is not an
error - declaring a group before its first tool arrives is legitimate, and the
frontend drops empty groups for exactly that reason.

Empty `App` and `System` tabs were considered and rejected. An empty section teaches
the user that the app has features it does not have, and inventing a tab for a
capability nobody has asked for is the same lie in a more convincing costume.

## Three presentation fixes that are worth more than they look

**The call bar was 92px tall.** It holds four controls at the bottom edge of a
window, and every pixel of it comes off the avatar above. It was sized for a touch
target a desktop mouse never needs. Now 72px, with a 56px call button.

**The end-call icon was `PhoneDisconnect` at `fill`.** A slash through a filled
handset merges with it at 20px - earpiece, mouthpiece and bar become one silhouette -
and reads as a "blocked" sign rather than a hang-up. Both handsets are now the same
glyph at `bold`, with the end-call one rotated 135 degrees by CSS. 135 and not 180
because a handset is drawn diagonally: a quarter turn leaves it diagonal the other
way and *reads* as dropped, where half a turn lands it vertical and looks cut in half.
Because the rotation is CSS on one glyph, the pair cannot be two icons that merely
look related.

`fill` was the wrong weight for the call button too. A filled handset has no interior
detail, so at 24px and below both ends blur and the shape stops being a handset.

**The Avatar Lab icon was an eye.** It points at *looking at*, and the Avatar Lab is
where Lumine's expression is built. It is now a face.

## A note on reading files before editing them

Mid-round, `read` returned a stale view of `SettingsDialog.tsx` showing
`opacity: 0` in **both** branches, when only the motion branch was wrong - which is
the opposite of the bug, and editing from it would have introduced the very error
being fixed. `[System.IO.File]::ReadAllText` showed the file as it was.

The general rule that fell out of it: this repository's prose describes intent and the
executable source is authoritative, and a cached read of a file you have already read
this session is weaker evidence than a fresh one. Confirm before every edit to a file
read recently.

## A thinking level the standard Live model refuses

Switching to a realtime profile produced a session that joined, said nothing, and
closed. The log named the cause exactly:

```text
APIError('1007 None. Thinking level is not supported for this model.')
```

`gemini-3.8-live` is not configured by thinking level at all. Google's own Live
thinking table says so in one cell — "`thinking_level` not supported" — and its
migration note for 3.8 says to *omit* `thinking_config`. We were sending one
anyway, as `minimal`.

### The catalog was wrong, and wrong in a way that looked deliberate

The catalog held one constant for the whole family:

```python
# The Live API takes the same parameter but a wider set, and documents `minimal`
# as its default for lowest latency.
_GOOGLE_LIVE_LEVELS = ("minimal", "low", "medium", "high")
```

applied to all four Live models. The comment cites the LiveKit plugin page, and
the plugin page does say 3.1 uses `thinkingLevel` with `minimal` available. But
that is a statement about the *parameter's shape*, and it was read as a statement
about *which models accept it*. The two are different, and Google's per-model
table is the authority:

| Model | Levels |
| --- | --- |
| `gemini-3.8-live` | none — the parameter is rejected |
| `gemini-3.8-live-extended-thinking` | `low`, `medium`, `high` (`minimal` unsupported) |
| `gemini-3.1-flash-live-preview` | `minimal` … `high` |

So the single set was wrong for **two of the three** current models: it sent a
parameter the first refuses, and a value the second refuses. The comment's
"lowest latency" reasoning is what made it feel safe — `minimal` sounds like the
conservative choice. It is the least conservative one available.

### The catalog was only half the bug

Fixing the sets alone would have left the trap armed. `gemini_settings()` seeded
`thinking_level` with a hardcoded `DEFAULT_GEMINI_THINKING_LEVEL = "minimal"` into
**every** realtime profile, and `config_store` copied it into the resolved stage
options whether or not a level had ever been chosen. So a profile with no
thinking setting carried one anyway, and switching to the extended-thinking model
would have hit the identical failure on a different value.

The pipeline LLM path had already solved this, and said so. `test_no_level_is_
invented_for_the_pipeline_llm` is named for the regression it guards: *"a saved
profile with no level must send no level. The factory used to default to
`minimal`."* The realtime path never got the same treatment, which is why a
principle that was already established in this codebase had to be applied to it
late. `DEFAULT_GEMINI_THINKING_LEVEL` is deleted; `gemini_settings()` reports a
level only when the environment names one, exactly as it already handled
`GEMINI_TEMPERATURE`.

**Not sending a level is also the lower-latency option**, which is what made the
invention hard to see. It is the provider's own default, it is valid for every
current model, and it costs nothing to omit. The old default bought a latency
claim and paid for it with a session that could not start.

### The guard existed, and nothing called it

`pipeline_factory._realtime_thinking_level` was written for exactly this problem.
Its docstring: *"Nothing is invented: a wrong level is a 400 that produces a
session with no audio."* It was never called from anywhere — the realtime builder
went through `_declared_options` → `build_options` instead — and its own behaviour
was to return the model's lowest accepted level when none was requested, which is
the invention the docstring disclaims.

A helper whose only purpose is to enforce an invariant, tested directly, and
unreferenced, is not coverage. `RealtimeThinkingLevelTests` asserted
`_realtime_thinking_level(model, None) == "minimal"` and passed, four lines away
from the assertion that would have caught the bug. Those tests now drive
`_build_realtime` and read the kwargs the plugin actually receives, so a future
regression has to survive the real construction path to be caught.

Removing it left `ModelDefinition.default_thinking_level()` with no production
caller, and its docstring still argued the case for the bug — *"the lowest
supported level is the right default for a voice agent: it is the fastest"*. That
is the trap re-armed for whoever reaches for it next, so it is gone too. A model
either takes the levels it declares or takes none; there is no default to compute,
because omitting the level is both valid everywhere and faster than any level we
could name. `test_no_model_ships_a_default_level` guards the removal.

**The catalog's declared sets are what make this work**, and no factory change was
needed to stop the crash: `_bind_option_schemas` drops the `thinking_level` option
entirely for a model with no levels, so `build_options` omits `thinking_config`
and the plugin holds `NOT_GIVEN`. Verified against the real plugin, not a mock —
`is_given(thinking_config)` is `False`, and the plugin's setup path passes `None`.

The lesson generalises past this bug: *a rule enforced by a helper nobody calls is
not enforced*, and a constant that compiles is not the same as a constant that is
true of the service it names. The second one is only caught by a live call, which
is why the failing session is the test that mattered.

### One thing deliberately left alone

`gemini-2.5-flash-native-audio-preview-12-2025` still declares
`_GOOGLE_LIVE_LEVELS`. LiveKit's migration note says 2.5 is configured by
`thinkingBudget` rather than `thinkingLevel`, which suggests the same class of
problem, but Google's table for 3.8 does not cover it and no Live thinking page
states what 2.5 does with a level. Changing it would be guessing at a fact in the
same way the family-wide constant was, in the opposite direction. It is deprecated,
and it is worth checking against the 2.5 model page before anyone selects it.

## References

LiveKit Agents documentation, consulted for the rules above:

* Pipeline types and half-cascade — <https://docs.livekit.io/agents/models/pipelines>
* Realtime models and separate TTS — <https://docs.livekit.io/agents/models/realtime>
* Gemini Live, 3.1 compatibility, and the separate-TTS caveat —
  <https://docs.livekit.io/agents/models/realtime/plugins/gemini>
* Turn handling and the interruption `ValueError` —
  <https://docs.livekit.io/agents/logic/turns>
* Live thinking, per-model levels, and the 3.8 migration note —
  <https://ai.google.dev/gemini-api/docs/live-api/thinking>
* Cartesia TTS, model lifecycle, and voices —
  <https://docs.livekit.io/agents/models/tts/cartesia>
* Google STT, including the VAD requirement —
  <https://docs.livekit.io/agents/models/stt/gemini>
* Silero VAD options — <https://docs.livekit.io/agents/logic/turns/vad>
