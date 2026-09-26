# Lumine AI Control Center — architecture and contracts

This document pins down the contracts that the settings redesign depends on, and
records the decisions taken so far. It is the reference for the remaining
phases; the executable source stays authoritative where prose and code disagree.

Status: **Phases 0–4 complete.** The catalog, validation rules, shared types,
configuration precedence, the capability-driven factory, the Tauri command
surface, the settings surface, profile collections, and credential injection all
exist and are tested.

`agent.py` now honours a saved configuration when one is valid and falls back to
`agent/.env` otherwise. A fresh install with nothing saved behaves exactly as
before, so the voice path is unchanged until a user saves something.

## Decisions taken

Recorded so later phases do not relitigate them:

| Question | Decision |
| --- | --- |
| Credential backend | `keyring` crate (verified), Stronghold held in reserve. |
| Settings shape | Overlay shell, no router dependency. |
| Interruption mode | Moves into `AI → Voice & Models`; it is part of the active profile, not appearance. |
| Persona | Read-only until the settings surface exists. |
| Settings file owner | Rust writes it atomically; the JS store plugin is not the authoritative copy. |

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
| `set_credential` | redacted status | Write-only. |
| `delete_credential` | — | Deleting an absent credential is a success. |
| `get_credential_status` | redacted status | `present` / `last4` only. |

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
* No provider connection test. Credentials can be stored and are handed to the
  worker, but nothing has yet made a live authenticated request to prove a key
  works. That is Phase 6.
* Credential changes need a worker restart. The worker is a persistent LiveKit
  process; re-reading the keyring per job would mean a round trip on every voice
  session.
* The `providers` map in the document is declared but still carries no meaning.
  Injection currently keys off "a secret exists in the keyring" rather than an
  `enabled` flag, so there is no per-profile provider opt-out yet.
* No local/offline provider. The id is reserved; the backend is not.
* Only Google has a realtime builder. Other realtime providers are rejected with
  a message naming the file to edit, rather than producing a broken session.
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
agent/validation.py                    pure validation rules -> diagnostics
agent/config_store.py                  precedence: saved document > agent/.env > defaults
agent/validate_config.py               configuration CLI: --describe and validate
agent/session_preferences.py           per-job metadata -> JobPreferences (mode + profile ref)
agent/pipeline_config.py               cartesia_tts_settings() (model no longer hardcoded)
agent/tests/test_providers.py          catalog integrity + golden-config consistency
agent/tests/test_validation.py         every rule above, both directions
agent/tests/test_config_store.py       precedence, env profile, job metadata, deprecation guard
agent/tests/test_pipeline_capabilities.py  independent stage selection, half-cascade gating
agent/tests/test_config_wiring.py      saved-config governance and environment fallback
agent/tests/test_import_shapes.py      worker import path + no relative imports in functions
agent/tests/fixtures/                  golden config, validated by the suite
lumine-ui/src/features/settings/       TS mirror of the contracts, IPC client, state hook
lumine-ui/src/pages/settings/          settings shell, nav, and pages
lumine-ui/src/features/settings/profileOps.ts  pure profile operations (kind conversion, defaults)
lumine-ui/scripts/                    check:profile-ops, check:conversion, the TS resolver hook
lumine-ui/src-tauri/src/settings_store.rs   versioned file, atomic write, .bak
lumine-ui/src-tauri/src/credentials.rs       OS keyring, write-only surface
lumine-ui/src-tauri/src/python_env.rs        shared interpreter/agent-dir resolution
```

## Verification

| Check | Result |
| --- | --- |
| `python -m unittest discover -s agent\tests` | 279 passed |
| `cargo test --lib` (src-tauri) | 35 passed |
| `cargo build` / `cargo check --all-targets` | zero warnings |
| `npm run build` (`tsc && vite build`) | clean |
| `npm run check:profile-ops` | passed |
| `npm run check:conversion` | passed |
| `python agent/provider_catalog.py --check` | ok (version 5) |
| `python agent/validate_config.py --describe` | reports the effective source and path |

### Checking the TypeScript side

The frontend has no test runner and the project rule is not to add one for
convenience. Two checks run against the real source instead, because Node can
strip type annotations itself. `scripts/ts-extension-resolver.mjs` teaches Node
the extensionless imports Vite resolves, so nothing is copied or duplicated:

| Script | What it proves |
| --- | --- |
| `check:profile-ops` | `convertProfileKind` produces a complete, valid profile in both directions, the interruption mode survives a round trip, a chosen name is not mistaken for an environment label, and the catalog's voice lists are duplicate-free. |
| `check:conversion` | The document the settings screen builds is accepted by `agent/validate_config.py`, and loading it through `LUMINE_CONFIG_PATH` — the same variable `agent_manager.rs` sets — reports `source: "ui"` with the selection intact. |

`check:conversion` exists because the seed values are chosen in TypeScript and
judged in Python, and nothing else in the build puts those two halves in the same
room. It has already earned its place: it is what showed that `--describe` reads
the *active* configuration and ignores its positional argument, so a check
pointing at a document by path was silently validating the environment profile
instead.

`scripts/` is inside the `tsconfig.json` include list, so `npm run build` typechecks
these files. That is how three real type errors in them were found.

## References

LiveKit Agents documentation, consulted for the rules above:

* Pipeline types and half-cascade — <https://docs.livekit.io/agents/models/pipelines>
* Realtime models and separate TTS — <https://docs.livekit.io/agents/models/realtime>
* Gemini Live, 3.1 compatibility, and the separate-TTS caveat —
  <https://docs.livekit.io/agents/models/realtime/plugins/gemini>
* Turn handling and the interruption `ValueError` —
  <https://docs.livekit.io/agents/logic/turns>
* Cartesia TTS, model lifecycle, and voices —
  <https://docs.livekit.io/agents/models/tts/cartesia>
* Google STT, including the VAD requirement —
  <https://docs.livekit.io/agents/models/stt/gemini>
* Silero VAD options — <https://docs.livekit.io/agents/logic/turns/vad>
