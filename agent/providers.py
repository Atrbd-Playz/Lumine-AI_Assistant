"""Curated, capability-based catalog of the providers and models Lumine can use.

This module is the single source of truth for "what can Lumine do with which
model". It is deliberately:

* **static and offline** - there is no cross-provider "list my models" API that
  every LiveKit plugin implements, so the catalog is curated from the LiveKit
  Agents documentation instead of probed at runtime. A future provider-test
  command can confirm entries against the live API; until then this stays pure
  and unit-testable.
* **secret-free** - it contains no keys, URLs with credentials, or anything else
  sensitive. Only *environment variable names* appear, which are already
  documented in ``agent/.env.example``.
* **capability-based** - a provider is not "a Groq" or "a Cartesia", it is a set
  of capabilities (stt / llm / tts / realtime / vad / transport). Components read
  this catalog instead of branching on provider names.

Keeping this in the Python worker (rather than duplicating it in TypeScript)
means the runtime and the UI cannot disagree about which model can do what.
``agent/provider_catalog.py`` prints a redacted JSON view for the desktop layer.

Model identifiers were transcribed from the LiveKit Agents docs for the pinned
``livekit-agents>=1.8.2,<1.9`` plugins. Re-verify them when the plugin pins move;
``test_providers.py`` guards the structural invariants, not provider availability.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Literal, Mapping

# The capability vocabulary is open-ended on purpose: adding a new kind of model
# capability must not require editing every provider definition.
Capability = Literal["stt", "llm", "tts", "realtime", "vad", "transport"]

CAPABILITIES: tuple[Capability, ...] = ("stt", "llm", "tts", "realtime", "vad", "transport")

# Bumped when the shape of the public catalog changes, so the desktop layer can
# refuse to render a catalog it does not understand. Version 2 added
# ``preferredFor``; the settings screen reads it while seeding a new stage.
# Version 3 added the Gemini 3.8 Live family, and marked the 3.1 and 2.5 Live
# models deprecated in favour of it. Version 4 added ``thinkingLevels`` and
# ``options``, the per-model description of what a stage accepts and which values
# are legal, so the settings screen can offer a model its own settings.
# Version 6 added ``probe``, how each provider's credential can be tested.
CATALOG_VERSION = 7

ModelStatus = Literal["available", "deprecated", "retired"]

# Reserved provider id for future local/offline inference (Ollama, llama.cpp,
# local Whisper, local Kokoro). It is intentionally not in PROVIDERS yet: the
# abstraction must not force a cloud-only assumption, but shipping a selectable
# provider with no backend would be a lie in the UI.
RESERVED_PROVIDER_IDS: tuple[str, ...] = ("local",)


@dataclass(frozen=True)
class VoiceDefinition:
    """One selectable voice for a TTS or realtime model."""

    id: str
    label: str
    languages: tuple[str, ...] = ("en",)
    default: bool = False
    notes: str = ""


@dataclass(frozen=True)
class ProbeDefinition:
    """How to make one cheap authenticated request to prove a credential works.

    Declared here so a provider's connectivity check is a catalog entry rather
    than a branch in the desktop layer, and so the status codes that mean "this
    key is not usable" are stated by the provider rather than guessed.

    The endpoints below were verified against live keys, not read out of docs.
    Two findings shaped this:

    * **A key in a URL is a leak.** It lands in access logs and proxy logs, so
      the secret always travels in a header.
    * **4xx does not mean "bad key".** Google answers an invalid key with **400**
      and a message, while a wrong path answers 405 and an outage answers 5xx.
      Treating any 4xx as a bad credential would tell a user their key was wrong
      when the truth was that our request was. `invalid_status` lists only the
      codes that really do mean the credential is at fault; everything else is
      reported as inconclusive.
    """

    url: str
    method: str = "GET"
    #: Header the secret is placed in. Cartesia wants a version header too, which
    #: is why extra headers exist.
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    extra_headers: tuple[tuple[str, str], ...] = ()
    #: Statuses that mean the credential itself is not usable.
    invalid_status: tuple[int, ...] = (401, 403)
    #: Whether the request costs anything. A TTS probe synthesises a word, so it
    #: is worth saying so out loud.
    costs: str = ""

    def headers_for(self, secret: str) -> dict[str, str]:
        headers = {name: value for name, value in self.extra_headers}
        headers[self.auth_header] = f"{self.auth_prefix}{secret}"
        return headers

    def request_body(self) -> str:
        """The body to send, as JSON. Empty for the GET probes that are used."""
        return ""


@dataclass(frozen=True)
class OptionDefinition:
    """One tunable setting a model accepts, described rather than special-cased.

    This exists so that "what does this model need, and what may I set on it" is
    declared once, in the catalog, instead of being re-derived in a provider
    branch in the factory and again in a validation rule. A provider added later
    declares its options here and needs no change anywhere else.

    Attributes:
        name: The key as it appears in a saved profile stage.
        values: The accepted values, for an enumerated setting. Empty means the
            value is free-form (a number, or a string the provider validates).
        nest: Some providers take a setting inside a nested object rather than as
            a keyword argument -- Google's thinking level arrives as
            ``thinking_config={"thinking_level": ...}``. Naming the group here
            keeps that shape out of the factory.
        companions: Settings that only mean anything alongside this one, and are
            gathered into the same nested object.
        default_for_voice: Set when the option should follow the model's default
            voice rather than carrying a value of its own.
    """

    name: str
    values: tuple[str, ...] = ()
    nest: str = ""
    companions: tuple[str, ...] = ()
    notes: str = ""

    def is_valid(self, value: object) -> bool:
        if not self.values:
            return isinstance(value, (str, int, float, bool))
        return str(value).strip().lower() in {v.lower() for v in self.values}

    def first_allowed(self) -> str | None:
        """The lowest-cost value this option accepts, or ``None`` if free-form."""
        for candidate in ("minimal", "low", "medium", "high"):
            if candidate in {v.lower() for v in self.values}:
                return candidate
        return self.values[0] if self.values else None


@dataclass(frozen=True)
class ModelDefinition:
    """One selectable model, tagged with the capability it satisfies.

    The realtime-only flags default to ``False`` so that non-realtime models stay
    terse to declare. They mirror real, documented LiveKit behaviours rather than
    invented ones; see ``docs/ai-control-center.md`` for the citations.
    """

    id: str
    label: str
    capability: Capability
    status: ModelStatus = "available"
    default: bool = False
    replaces: str | None = None
    notes: str = ""

    # --- STT ---
    # The Google STT backend is non-streaming, so LiveKit needs a VAD to decide
    # when a speech segment is complete. Without one, the session raises.
    requires_vad: bool = False

    # --- realtime ---
    # A native-audio model generates speech itself. LiveKit's separate-TTS
    # ("half-cascade") path needs a text-only response modality, which the
    # Gemini native-audio models do not support.
    native_audio: bool = False
    text_only_modality: bool = False
    # Whether "finish reply" can be honoured through a provider-side setting.
    # For a realtime model that owns server-side turn detection, LiveKit rejects
    # ``turn_handling.interruption.enabled=False`` outright, so this must be a
    # real provider capability rather than a session-level toggle.
    finish_response: bool = False
    proactivity: bool = False
    affective_dialog: bool = False
    async_function_calling: bool = False
    # Voices the model can use directly, when the model speaks for itself.
    voices: tuple[str, ...] = ()
    # Thinking levels this model accepts, when it takes one at all.
    #
    # Empty means "do not send a thinking level": the model then applies its own
    # default, which is always valid. It is not the same as "supports nothing",
    # it means the parameter is not how this model is configured -- Gemini 2.5
    # uses a token budget rather than a level, and the Google plugin only
    # translates a level for the Gemini 3 family.
    #
    # This is per-model on purpose. Google rejects a level a given model does not
    # accept with a 400, and the sets are not nested: `gemini-3.8-flash` takes
    # low/medium/high but not minimal, while the Live API takes minimal too.
    thinking_levels: tuple[str, ...] = ()
    # The settings this model accepts, described. Drives both the factory (which
    # values to forward, and in what shape) and validation (which values are
    # legal), so neither has to know which provider it is looking at.
    options: tuple[OptionDefinition, ...] = ()

    def is_usable(self) -> bool:
        """Retired models stay in the catalog so old profiles explain themselves."""
        return self.status != "retired"

    def supports_thinking_level(self, level: str) -> bool:
        return level.strip().lower() in {value.lower() for value in self.thinking_levels}

    def option(self, name: str) -> OptionDefinition | None:
        for candidate in self.options:
            if candidate.name == name:
                return candidate
        return None

    def accepts(self, name: str) -> bool:
        return self.option(name) is not None

    def build_options(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """Turn saved stage settings into keyword arguments for the provider.

        Declared options are forwarded, unknown keys are dropped, and a setting
        the provider nests inside another object is gathered there. This is the
        one place that knows about nesting, which is why the factory does not
        have a branch per provider.

        A value the model does not accept is replaced with the closest one it
        does, or left out. Silently sending a rejected value produces a 400 at
        request time, which in a voice session is indistinguishable from a muted
        microphone; validation reports it separately.
        """
        options: dict[str, Any] = {}
        nested: dict[str, dict[str, Any]] = {}

        for definition in self.options:
            raw = settings.get(definition.name)
            if raw is None:
                # Absent means the provider applies its own default, which is
                # valid by construction. Nothing is invented here.
                continue

            value = raw
            if definition.values:
                text = str(raw).strip().lower()
                if text not in {v.lower() for v in definition.values}:
                    value = definition.first_allowed()
                    if value is None:
                        continue
                else:
                    value = text

            if definition.nest:
                group = nested.setdefault(definition.nest, {})
                group[definition.name] = value
                for companion in definition.companions:
                    extra = settings.get(companion)
                    if extra is not None:
                        group[companion] = extra
            else:
                options[definition.name] = value

        options.update(nested)
        return options

    def default_thinking_level(self) -> str | None:
        """The lowest level this model accepts, or ``None`` if it takes none.

        The lowest supported level is the right default for a voice agent: it is
        the fastest, and it is a level the model is known to accept, which a
        hardcoded constant is not.
        """
        order = ("minimal", "low", "medium", "high")
        available = {value.lower(): value for value in self.thinking_levels}
        for candidate in order:
            if candidate in available:
                return available[candidate]
        return None


@dataclass(frozen=True)
class ProviderDefinition:
    """A provider: identity, credential requirement, and what it can do."""

    id: str
    label: str
    requires_key: bool = True
    key_env: tuple[str, ...] = ()
    setup_url: str = ""
    local: bool = False
    notes: str = ""
    #: Capabilities this provider is the application's own default for.
    #:
    #: The settings screen seeds a new stage from these, so switching stack type
    #: lands on the stack Lumine already runs rather than on whichever provider
    #: happens to sort first. Alphabetical order is not a product decision: it
    #: would put Google in front of Groq for speech recognition and quietly move
    #: the conversation onto a different vendor.
    preferred_for: tuple[Capability, ...] = ()
    #: The connectivity check for this provider, or `None` when it has no cheap
    #: authenticated request. Being absent is honest: the page then says the key is
    #: stored, not that it works.
    probe: ProbeDefinition | None = None
    models: tuple[ModelDefinition, ...] = field(default_factory=tuple)
    voices: tuple[VoiceDefinition, ...] = field(default_factory=tuple)

    def capabilities(self) -> tuple[Capability, ...]:
        found = {model.capability for model in self.models}
        return tuple(cap for cap in CAPABILITIES if cap in found)

    def supports(self, capability: Capability) -> bool:
        return any(model.capability == capability for model in self.models)

    def models_for(self, capability: Capability) -> tuple[ModelDefinition, ...]:
        return tuple(model for model in self.models if model.capability == capability)

    def default_for(self, capability: Capability) -> ModelDefinition | None:
        for model in self.models_for(capability):
            if model.default:
                return model
        return None

    def get_model(self, model_id: str) -> ModelDefinition | None:
        for model in self.models:
            if model.id == model_id:
                return model
        return None

    def get_voice(self, voice_id: str) -> VoiceDefinition | None:
        for voice in self.voices:
            if voice.id == voice_id:
                return voice
        return None


# ---------------------------------------------------------------------------
# Voice catalogs
# ---------------------------------------------------------------------------

_CARTESIA_VOICES = (
    VoiceDefinition(
        id="f786b574-daa5-4673-aa0c-cbe3e8534c02",
        label="Calm",
        default=True,
        notes="Plugin default voice.",
    ),
    # This is the voice Lumine has always used. It stays in the catalog so an
    # existing profile keeps working and keeps sounding like Lumine.
    VoiceDefinition(
        id="002622d8-19d0-4567-a16a-f99c7397c062",
        label="Lumine (current)",
        notes="The voice the legacy cascade has used since it was added.",
    ),
    VoiceDefinition(
        id="a167e0f3-df7e-4d52-a9c3-f949145efdab",
        label="Blake",
        languages=("en-US",),
        notes="Energetic American adult male.",
    ),
    VoiceDefinition(
        id="9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
        label="Jacqueline",
        languages=("en-US",),
        notes="Confident, young American adult female.",
    ),
)

_GEMINI_TTS_VOICES = (
    VoiceDefinition(id="Zephyr", label="Zephyr", default=True),
    VoiceDefinition(id="Kore", label="Kore", notes="Plugin default voice."),
    VoiceDefinition(id="Aoede", label="Aoede"),
)

_GEMINI_LIVE_VOICES = (
    # Gemini Live and Gemini TTS draw on the same named voice set, so every voice
    # the catalog knows is offered here. The list is curated: an unknown name is a
    # warning rather than an error, and the settings screen also accepts one typed
    # by hand, so a voice missing from this list is never a dead end.
    VoiceDefinition(id="Sulafat", label="Sulafat", default=True),
    VoiceDefinition(id="Puck", label="Puck"),
    VoiceDefinition(id="Zephyr", label="Zephyr"),
    VoiceDefinition(id="Kore", label="Kore"),
    VoiceDefinition(id="Aoede", label="Aoede"),
)


def _dedupe_voices(*groups: tuple[VoiceDefinition, ...]) -> tuple[VoiceDefinition, ...]:
    """Merge voice groups, keeping the first definition of each id.

    Gemini Live and Gemini TTS draw on the same named voices, so concatenating
    the two lists would surface duplicates in the settings picker.
    """
    seen: set[str] = set()
    merged: list[VoiceDefinition] = []
    for group in groups:
        for voice in group:
            if voice.id in seen:
                continue
            seen.add(voice.id)
            merged.append(voice)
    return tuple(merged)


#: Every voice the catalog knows for Google, across TTS and Live.
_GOOGLE_VOICES = _dedupe_voices(_GEMINI_TTS_VOICES, _GEMINI_LIVE_VOICES)

# Thinking levels, from Google's own per-model table at
# https://ai.google.dev/gemini-api/docs/thinking. The sets are not nested:
# `gemini-3.8-flash` rejects `minimal` with a 400 while `gemini-3.6-flash`
# accepts it, so a single default for the family is wrong.
_GOOGLE_LEVELS_3_8 = ("low", "medium", "high")
_GOOGLE_LEVELS_FULL = ("minimal", "low", "medium", "high")

# The Live API takes the same parameter but a wider set, and documents `minimal`
# as its default for lowest latency. See the LiveKit Gemini Live plugin page.
_GOOGLE_LIVE_LEVELS = ("minimal", "low", "medium", "high")

# Declared option sets, keyed by (provider, capability). Grouped here rather than
# inline at each model so the shape of a provider's configuration is readable in
# one place, and so a new model picks up the right set by naming its provider and
# capability. A provider added later adds a key here and nothing else.
#
# `nest` says the provider takes a setting inside another object rather than as a
# keyword argument -- Google's thinking level arrives as
# ``thinking_config={"thinking_level": ...}``. Naming the group here is what lets
# the factory build every provider's options the same way, with no branch per
# provider.

_OPTION_SETS: dict[tuple[str, Capability], tuple[OptionDefinition, ...]] = {
    ("google", "realtime"): (
        OptionDefinition(
            name="thinking_level",
            values=(),  # filled in per model from `thinking_levels`
            nest="thinking_config",
            companions=("include_thoughts",),
            notes="How much the model reasons before answering. Lower is faster.",
        ),
        OptionDefinition(name="temperature"),
        OptionDefinition(
            name="max_output_tokens",
            notes="A hard cutoff that includes reasoning tokens.",
        ),
        OptionDefinition(name="language", notes="Spoken language, as a locale code."),
    ),
    ("google", "llm"): (
        OptionDefinition(
            name="thinking_level",
            values=(),  # filled in per model from `thinking_levels`
            nest="thinking_config",
            companions=("include_thoughts",),
            notes="How much the model reasons before answering. Lower is faster.",
        ),
        OptionDefinition(
            name="temperature",
            notes="Higher is more varied, lower is more predictable.",
        ),
        OptionDefinition(
            name="max_output_tokens",
            notes="A hard cutoff that includes reasoning tokens.",
        ),
    ),
    ("google", "stt"): (
        OptionDefinition(name="language", notes="Spoken language, as a locale code."),
    ),
    ("google", "tts"): (
        OptionDefinition(name="language", notes="Spoken language, as a locale code."),
        OptionDefinition(name="speed"),
        OptionDefinition(name="voice", notes="Which voice speaks the reply."),
    ),
    ("cartesia", "tts"): (
        OptionDefinition(name="speed"),
        OptionDefinition(name="language", notes="Spoken language, as a locale code."),
        OptionDefinition(name="temperature"),
        OptionDefinition(name="voice", notes="Which voice speaks the reply."),
    ),
    ("groq", "llm"): (
        OptionDefinition(name="temperature"),
        OptionDefinition(name="max_completion_tokens", notes="A hard cap on the reply."),
        OptionDefinition(name="top_p"),
        OptionDefinition(name="parallel_tool_calls"),
        OptionDefinition(
            name="reasoning_effort",
            values=("low", "medium", "high"),
            notes=(
                "How much the model thinks before answering. GPT-OSS is a reasoning "
                "model and its thinking counts against the token cap, so 'low' is "
                "what a voice turn wants."
            ),
        ),
    ),
    ("groq", "stt"): (
        OptionDefinition(name="language", notes="Spoken language, as a locale code."),
    ),
    ("silero", "vad"): (
        OptionDefinition(name="min_speech_duration"),
        OptionDefinition(name="min_silence_duration"),
        OptionDefinition(name="prefix_padding"),
        OptionDefinition(name="activation_threshold"),
    ),
}

#: Settings the factory consumes itself rather than forwarding to a plugin.
#:
#: Turn detection and connection policy are session wiring: LiveKit needs them
#: alongside the model, but they are not constructor arguments of it. Declaring
#: them as model options would pass them to a plugin that does not accept them,
#: and leaving them undeclared would report them as ignored on every save.
SESSION_OPTIONS: frozenset[str] = frozenset(
    {
        "silence_duration_ms",
        "connect_max_retry",
        "connect_timeout",
        "max_tool_steps",
    }
)

# Connectivity checks. Verified against live keys rather than taken from docs,
# because the interesting part is the failure shape and documentation rarely
# states it. See `ProbeDefinition` for why 4xx is not treated as "bad key".
#
# `invalid_status` is the part worth reading. Google's list-of-models answers an
# invalid key with 400 and a message, and answers a wrong path with 405 -- so a
# blanket "any 4xx means bad key" would have blamed the user's key for our own
# mistake. Everything not listed is reported as inconclusive, not as a failure.
_PROBES: dict[str, ProbeDefinition] = {
    "google": ProbeDefinition(
        url="https://generativelanguage.googleapis.com/v1beta/models",
        auth_header="x-goog-api-key",
        auth_prefix="",
        # Verified: a valid key gives 200; an invalid one gives 400 with
        # "API key not valid", not 401.
        invalid_status=(400, 401, 403),
    ),
    "groq": ProbeDefinition(
        url="https://api.groq.com/openai/v1/models",
        # Verified: 200 with a valid key, 401 "Invalid API Key" with a bad one.
        invalid_status=(401, 403),
    ),
    "cartesia": ProbeDefinition(
        # There is no /v2 prefix on the path: the version travels in a header,
        # and asking for /v2/... returns 404 before authentication even happens.
        # Taken from the installed plugin's own constants rather than guessed.
        url="https://api.cartesia.ai/voices",
        extra_headers=(("Cartesia-Version", "2025-04-16"),),
        # Verified: 200 with a valid key, 401 "must be logged in" with a bad one.
        invalid_status=(401, 403),
    ),
    "openai": ProbeDefinition(
        url="https://api.openai.com/v1/models",
        invalid_status=(401, 403),
    ),
}

_OPENAI_TTS_VOICES = (
    VoiceDefinition(id="ash", label="Ash", default=True),
    VoiceDefinition(id="ballad", label="Ballad"),
    VoiceDefinition(id="coral", label="Coral"),
    VoiceDefinition(id="sage", label="Sage"),
)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, ProviderDefinition] = {
    "livekit": ProviderDefinition(
        id="livekit",
        label="LiveKit",
        # Lumine cannot speak to a user at all without this, so it is modeled as
        # a provider with a `transport` capability rather than left implicit.
        requires_key=True,
        key_env=("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"),
        setup_url="https://cloud.livekit.io/",
        notes="Rooms, tokens, and the transport that carries the voice session.",
        models=(
            ModelDefinition(
                id="livekit-cloud",
                label="LiveKit Cloud",
                capability="transport",
                default=True,
                notes="The only transport Lumine currently supports.",
            ),
        ),
    ),
    "google": ProviderDefinition(
        id="google",
        label="Google",
        requires_key=True,
        key_env=("GOOGLE_API_KEY",),
        setup_url="https://aistudio.google.com/apikey",
        notes="Gemini covers STT, LLM, TTS, and realtime from one credential.",
        voices=_GOOGLE_VOICES,
        models=(
            # --- realtime ---
            ModelDefinition(
                id="gemini-3.8-live",
                label="Gemini 3.8 Live",
                capability="realtime",
                default=True,
                native_audio=True,
                text_only_modality=False,
                finish_response=True,
                proactivity=False,
                affective_dialog=False,
                async_function_calling=False,
                voices=tuple(voice.id for voice in _GEMINI_LIVE_VOICES),
                thinking_levels=_GOOGLE_LIVE_LEVELS,
                notes=(
                    "Google's default Live API model, and the one Google recommends "
                    "over 3.1. Native audio, so it speaks for itself. LiveKit's "
                    "separate-TTS path needs a non-native-audio model, which no "
                    "current Gemini Live model is."
                ),
            ),
            ModelDefinition(
                id="gemini-3.8-live-extended-thinking",
                label="Gemini 3.8 Live (thinking)",
                capability="realtime",
                native_audio=True,
                text_only_modality=False,
                finish_response=True,
                proactivity=False,
                affective_dialog=False,
                async_function_calling=False,
                voices=tuple(voice.id for voice in _GEMINI_LIVE_VOICES),
                thinking_levels=_GOOGLE_LIVE_LEVELS,
                notes=(
                    "The high-reasoning Live model, for when the background "
                    "reasoning matters more than the delay before the first word. "
                    "Same native-audio limits as Gemini 3.8 Live."
                ),
            ),
            ModelDefinition(
                id="gemini-3.1-flash-live-preview",
                label="Gemini 3.1 Flash Live",
                capability="realtime",
                status="deprecated",
                replaces="gemini-3.8-live",
                native_audio=True,
                text_only_modality=False,
                finish_response=True,
                proactivity=False,
                affective_dialog=False,
                async_function_calling=False,
                voices=tuple(voice.id for voice in _GEMINI_LIVE_VOICES),
                thinking_levels=_GOOGLE_LIVE_LEVELS,
                notes=(
                    "Google now labels this legacy and recommends Gemini 3.8 Live. "
                    "Kept in the catalog so an existing profile keeps working and "
                    "can be told what to move to. No proactivity, affective dialog, "
                    "or asynchronous function calling."
                ),
            ),
            ModelDefinition(
                id="gemini-2.5-flash-native-audio-preview-12-2025",
                label="Gemini 2.5 Flash Live",
                capability="realtime",
                status="deprecated",
                replaces="gemini-3.8-live",
                native_audio=True,
                text_only_modality=False,
                finish_response=True,
                proactivity=False,
                affective_dialog=False,
                async_function_calling=False,
                voices=tuple(voice.id for voice in _GEMINI_LIVE_VOICES),
                thinking_levels=_GOOGLE_LIVE_LEVELS,
                notes=(
                    "The previous-generation Live model. Google limits 2.5 access to "
                    "accounts that have used it before."
                ),
            ),
            # --- llm ---
            ModelDefinition(
                id="gemini-3.8-flash",
                label="Gemini 3.8 Flash",
                capability="llm",
                default=True,
                requires_vad=True,
                thinking_levels=_GOOGLE_LEVELS_3_8,
                notes="Google's most capable Flash model, for long-horizon agent work.",
            ),
            ModelDefinition(id="gemini-3.1-flash-lite", label="Gemini 3.1 Flash Lite", capability="llm"),
            ModelDefinition(
                id="gemini-3.1-pro-preview",
                label="Gemini 3.1 Pro",
                capability="llm",
                thinking_levels=_GOOGLE_LEVELS_3_8,
            ),
            ModelDefinition(
                id="gemini-3.5-flash",
                label="Gemini 3.5 Flash",
                capability="llm",
                requires_vad=True,
                thinking_levels=_GOOGLE_LEVELS_FULL,
            ),
            ModelDefinition(
                id="gemini-3.5-flash-lite",
                label="Gemini 3.5 Flash Lite",
                capability="llm",
                requires_vad=True,
                thinking_levels=_GOOGLE_LEVELS_FULL,
            ),
            ModelDefinition(
                id="gemini-3.6-flash",
                label="Gemini 3.6 Flash",
                capability="llm",
                requires_vad=True,
                thinking_levels=_GOOGLE_LEVELS_FULL,
            ),
            ModelDefinition(
                id="gemini-3.7-flash",
                label="Gemini 3.7 Flash",
                capability="llm",
                requires_vad=True,
                thinking_levels=_GOOGLE_LEVELS_3_8,
            ),
            ModelDefinition(
                id="gemini-3-flash-preview",
                label="Gemini 3 Flash",
                capability="llm",
                status="deprecated",
                replaces="gemini-3.8-flash",
                thinking_levels=_GOOGLE_LEVELS_FULL,
                notes="Superseded by the 3.8 family.",
            ),
            # --- stt ---
            ModelDefinition(
                id="gemini-2.5-flash-lite",
                label="Gemini 2.5 Flash Lite",
                capability="stt",
                default=True,
                requires_vad=True,
                notes="Transcription is non-streaming, so a VAD is required to segment speech.",
            ),
            ModelDefinition(
                id="gemini-2.5-pro",
                label="Gemini 2.5 Pro",
                capability="stt",
                requires_vad=True,
                notes="Transcription is non-streaming, so a VAD is required to segment speech.",
            ),
            ModelDefinition(
                id="gemini-2.0-flash-001",
                label="Gemini 2.0 Flash",
                capability="stt",
                requires_vad=True,
                notes="Transcription is non-streaming, so a VAD is required to segment speech.",
            ),
            # --- tts ---
            ModelDefinition(
                id="gemini-3.8-flash-tts",
                label="Gemini 3.8 Flash TTS",
                capability="tts",
                default=True,
                notes="Supports LiveKit expressive mode.",
            ),
            ModelDefinition(
                id="gemini-3.8-flash-lite-tts",
                label="Gemini 3.8 Flash Lite TTS",
                capability="tts",
                notes="Supports LiveKit expressive mode.",
            ),
            ModelDefinition(
                id="gemini-3.1-flash-tts-preview",
                label="Gemini 3.1 Flash TTS (preview)",
                capability="tts",
                notes="Plugin default Gemini TTS model.",
            ),
        ),
    ),
    "groq": ProviderDefinition(
        id="groq",
        label="Groq",
        requires_key=True,
        key_env=("GROQ_API_KEY",),
        setup_url="https://console.groq.com/keys",
        notes="Very fast STT and LLM inference. No TTS or realtime.",
        # The speech recognition and language model Lumine has always run, and
        # therefore what a new profile should be seeded with.
        preferred_for=("stt", "llm"),
        models=(
            ModelDefinition(
                id="whisper-large-v3-turbo",
                label="Whisper Large v3 Turbo",
                capability="stt",
                default=True,
                notes="Streaming transcription. Groq's STT default.",
            ),
            ModelDefinition(
                id="whisper-large-v3",
                label="Whisper Large v3",
                capability="stt",
                notes="The larger Whisper variant.",
            ),
            ModelDefinition(
                id="openai/gpt-oss-20b",
                label="GPT-OSS 20B",
                capability="llm",
                default=True,
                notes=(
                    "Fastest and cheapest text model on Groq, and one of the two that "
                    "remain on the free tier. Supports prompt caching, so the system "
                    "prompt is exempt from the token allowance once it is warm."
                ),
            ),
            ModelDefinition(
                id="openai/gpt-oss-120b",
                label="GPT-OSS 120B",
                capability="llm",
                notes=(
                    "The same free-tier allowance as the 20B with better answers, at "
                    "roughly half the speed. Worth it for a harder question, not for "
                    "ordinary chat."
                ),
            ),
            ModelDefinition(
                id="llama-3.3-70b-versatile",
                label="Llama 3.3 70B Versatile",
                capability="llm",
                status="deprecated",
                replaces="openai/gpt-oss-20b",
                notes=(
                    "Groq withdrew this from the free and developer tiers in August "
                    "2026; it is now Enterprise only, so a free key is refused. Kept "
                    "so an existing profile explains itself."
                ),
            ),
            ModelDefinition(
                id="llama-3.1-8b-instant",
                label="Llama 3.1 8B Instant",
                capability="llm",
                status="deprecated",
                replaces="openai/gpt-oss-20b",
                notes="Also Enterprise only since August 2026.",
            ),
        ),
    ),
    "cartesia": ProviderDefinition(
        id="cartesia",
        label="Cartesia",
        requires_key=True,
        key_env=("CARTESIA_API_KEY",),
        setup_url="https://play.cartesia.ai/keys",
        notes="Fast, expressive streaming TTS with aligned transcriptions.",
        # The voice Lumine speaks with. Keep this in step with the legacy
        # pipeline in pipeline_config.py, or a new profile will not sound like
        # Lumine.
        preferred_for=("tts",),
        voices=_CARTESIA_VOICES,
        models=(
            ModelDefinition(id="sonic-3", label="Sonic 3", capability="tts", default=True),
            ModelDefinition(id="sonic-3.5", label="Sonic 3.5", capability="tts"),
            ModelDefinition(id="sonic-3.6", label="Sonic 3.6", capability="tts"),
            ModelDefinition(
                id="sonic-2",
                label="Sonic 2",
                capability="tts",
                status="deprecated",
                replaces="sonic-3.5",
                notes=(
                    "Deprecated and retires 2026-10-20. This is what Lumine's legacy "
                    "cascade still hardcodes, so it needs migrating before then."
                ),
            ),
            ModelDefinition(
                id="sonic-turbo",
                label="Sonic Turbo",
                capability="tts",
                status="deprecated",
                replaces="sonic-3.5",
            ),
        ),
    ),
    "openai": ProviderDefinition(
        id="openai",
        label="OpenAI",
        requires_key=True,
        key_env=("OPENAI_API_KEY",),
        setup_url="https://platform.openai.com/api-keys",
        notes="Installed but not used by any built-in Lumine pipeline yet.",
        voices=_OPENAI_TTS_VOICES,
        models=(
            ModelDefinition(id="gpt-4.1", label="GPT-4.1", capability="llm", default=True),
            ModelDefinition(id="gpt-4.1-mini", label="GPT-4.1 mini", capability="llm"),
            ModelDefinition(id="gpt-4o", label="GPT-4o", capability="llm"),
            ModelDefinition(id="gpt-4o-mini", label="GPT-4o mini", capability="llm"),
            ModelDefinition(id="gpt-5", label="GPT-5", capability="llm"),
            ModelDefinition(id="gpt-5-mini", label="GPT-5 mini", capability="llm"),
            ModelDefinition(
                id="gpt-4o-mini-transcribe",
                label="GPT-4o mini Transcribe",
                capability="stt",
                default=True,
            ),
            ModelDefinition(
                id="gpt-realtime-whisper",
                label="GPT Realtime Whisper",
                capability="stt",
                requires_vad=True,
                notes="Streaming STT without server-side turn detection, so a VAD is required.",
            ),
            ModelDefinition(
                id="gpt-4o-mini-tts",
                label="GPT-4o mini TTS",
                capability="tts",
                default=True,
            ),
        ),
    ),
    "silero": ProviderDefinition(
        id="silero",
        label="Silero",
        # Silero runs on-device; there is nothing to authenticate.
        requires_key=False,
        key_env=(),
        local=True,
        setup_url="https://github.com/snakers4/silero-vad",
        notes="Local CPU voice activity detection. No credential, no network call.",
        preferred_for=("vad",),
        models=(
            ModelDefinition(
                id="silero",
                label="Silero VAD",
                capability="vad",
                default=True,
                notes="Ships with the LiveKit plugin; downloaded on first use.",
            ),
        ),
    ),
}


def _bind_option_schemas() -> None:
    """Give every model its option set, and every provider its connectivity check.

    Done as one pass rather than by repeating ``options=`` on every model, so
    adding a model cannot forget to declare what it accepts. Two things are
    resolved here rather than at each declaration:

    * **A thinking level's values come from the model.** ``thinking_levels`` is
      the single source, and a model with none does not get a thinking option at
      all -- which is how a Gemini 2.5 model, configured by token budget instead,
      is distinguished from one that takes a level.
    * **A model's own ``options=`` wins.** An escape hatch for a model that takes
      something the shared set does not list.
    """
    for provider in PROVIDERS.values():
        models = []
        for model in provider.models:
            declared = _OPTION_SETS.get((provider.id, model.capability), ())
            resolved: list[OptionDefinition] = []
            for definition in declared:
                if definition.name == "thinking_level":
                    if not model.thinking_levels:
                        continue
                    definition = replace(definition, values=model.thinking_levels)
                resolved.append(definition)
            models.append(replace(model, options=model.options or tuple(resolved)))
        updated = replace(provider, models=tuple(models))
        probe = provider.probe if provider.probe is not None else _PROBES.get(provider.id)
        PROVIDERS[provider.id] = replace(updated, probe=probe)


_bind_option_schemas()


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def get_provider(provider_id: str) -> ProviderDefinition | None:
    return PROVIDERS.get(str(provider_id or "").strip())


def get_model(
    provider_id: str,
    model_id: str,
    capability: Capability | None = None,
) -> ModelDefinition | None:
    provider = get_provider(provider_id)
    if provider is None:
        return None
    model = provider.get_model(str(model_id or "").strip())
    if model is None:
        return None
    if capability is not None and model.capability != capability:
        return None
    return model


def models_for(provider_id: str, capability: Capability) -> tuple[ModelDefinition, ...]:
    provider = get_provider(provider_id)
    return provider.models_for(capability) if provider is not None else ()


def voices_for(provider_id: str) -> tuple[VoiceDefinition, ...]:
    provider = get_provider(provider_id)
    return provider.voices if provider is not None else ()


def providers_with(capability: Capability) -> tuple[ProviderDefinition, ...]:
    return tuple(p for p in PROVIDERS.values() if p.supports(capability))


def required_credentials() -> tuple[str, ...]:
    """Every provider id that needs a credential before a session can start."""
    return tuple(sorted(pid for pid, p in PROVIDERS.items() if p.requires_key))


# ---------------------------------------------------------------------------
# Public (redacted) view for the desktop layer
# ---------------------------------------------------------------------------


def _model_public(model: ModelDefinition) -> dict[str, Any]:
    return {
        "id": model.id,
        "label": model.label,
        "capability": model.capability,
        "status": model.status,
        "default": model.default,
        "replaces": model.replaces,
        "notes": model.notes,
        "requiresVad": model.requires_vad,
        # Empty means the model is not configured by thinking level, and the
        # worker sends none. The settings screen offers only what is listed here.
        "thinkingLevels": list(model.thinking_levels),
        "options": [
            {
                "name": definition.name,
                "values": list(definition.values),
                "nest": definition.nest,
                "notes": definition.notes,
            }
            for definition in model.options
        ],
        "realtime": {
            "nativeAudio": model.native_audio,
            "textOnlyModality": model.text_only_modality,
            "finishResponse": model.finish_response,
            "proactivity": model.proactivity,
            "affectiveDialog": model.affective_dialog,
            "asyncFunctionCalling": model.async_function_calling,
        }
        if model.capability == "realtime"
        else None,
        "voices": list(model.voices),
    }


def _voice_public(voice: VoiceDefinition) -> dict[str, Any]:
    return {
        "id": voice.id,
        "label": voice.label,
        "languages": list(voice.languages),
        "default": voice.default,
        "notes": voice.notes,
    }


def _probe_public(probe: ProbeDefinition | None) -> dict[str, Any] | None:
    """The connectivity check, with no secret in it.

    The header *name* and the fixed headers are public: they say which API is
    being called, not anything about the caller. The secret is only ever placed
    into the header at request time, in the process that holds it.
    """
    if probe is None:
        return None
    return {
        "method": probe.method,
        "url": probe.url,
        "authHeader": probe.auth_header,
        "authPrefix": probe.auth_prefix,
        "headers": {name: value for name, value in probe.extra_headers},
        "invalidStatus": list(probe.invalid_status),
        "costs": probe.costs,
    }


def _provider_public(provider: ProviderDefinition) -> dict[str, Any]:
    return {
        "id": provider.id,
        "label": provider.label,
        "requiresKey": provider.requires_key,
        # Environment variable *names* are public documentation, never secrets.
        "keyEnv": list(provider.key_env),
        "setupUrl": provider.setup_url,
        "local": provider.local,
        "notes": provider.notes,
        "capabilities": list(provider.capabilities()),
        "preferredFor": list(provider.preferred_for),
        "probe": _probe_public(provider.probe),
        "models": [_model_public(m) for m in provider.models],
        "voices": [_voice_public(v) for v in provider.voices],
    }


def to_public_catalog() -> dict[str, Any]:
    """A JSON-serializable snapshot safe to hand to the frontend.

    Contains no credentials and no runtime state. The desktop layer renders its
    provider and model pickers from this instead of hardcoding provider names.
    """
    return {
        "version": CATALOG_VERSION,
        "capabilities": list(CAPABILITIES),
        "reservedProviderIds": list(RESERVED_PROVIDER_IDS),
        "providers": [_provider_public(PROVIDERS[pid]) for pid in sorted(PROVIDERS)],
    }


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------


def catalog_issues(
    providers: Iterable[ProviderDefinition] | None = None,
) -> list[str]:
    """Structural problems with the catalog. An empty list means it is coherent.

    This is intentionally a pure function so ``test_providers.py`` can assert the
    catalog is internally consistent without touching a provider.
    """
    issues: list[str] = []
    seen_providers: set[str] = set()

    for provider in providers if providers is not None else PROVIDERS.values():
        if provider.id in seen_providers:
            issues.append(f"duplicate provider id: {provider.id}")
        seen_providers.add(provider.id)

        if not provider.label:
            issues.append(f"{provider.id}: missing label")

        # A provider that needs a key must name the variables the worker reads.
        if provider.requires_key and not provider.key_env:
            issues.append(f"{provider.id}: requires_key but declares no key_env")
        if not provider.requires_key and provider.key_env:
            issues.append(f"{provider.id}: declares key_env but does not require a key")
        if not provider.setup_url:
            issues.append(f"{provider.id}: missing setup_url")

        probe = provider.probe
        if probe is not None:
            if not provider.requires_key:
                issues.append(f"{provider.id}: has a probe but needs no credential")
            if not probe.url.startswith("https://"):
                # A probe sends the user's credential to this address. Plain HTTP
                # would put it on the wire in the clear.
                issues.append(f"{provider.id}: probe url must be https")
            if not probe.auth_header:
                issues.append(f"{provider.id}: probe names no auth header")
            if not probe.invalid_status:
                issues.append(
                    f"{provider.id}: probe lists no invalid status, so a wrong key "
                    "could not be told from an outage"
                )
            if 200 in probe.invalid_status:
                issues.append(f"{provider.id}: probe lists 200 as an invalid status")

        if not provider.models:
            issues.append(f"{provider.id}: has no models")

        seen_models: set[str] = set()
        for model in provider.models:
            label = f"{provider.id}/{model.id}"
            if model.id in seen_models:
                issues.append(f"{label}: duplicate model id within provider")
            seen_models.add(model.id)

            if model.capability not in CAPABILITIES:
                issues.append(f"{label}: unknown capability {model.capability!r}")
            if not model.label:
                issues.append(f"{label}: missing label")
            if model.status == "deprecated" and not model.replaces:
                issues.append(f"{label}: deprecated but names no replacement")
            if model.replaces and model.replaces not in seen_models and model.replaces != model.id:
                # A replacement that is not in the catalog would leave the user
                # with a dead-end "switch to" suggestion.
                issues.append(f"{label}: replacement {model.replaces!r} is not in this provider")
            if model.default and model.status != "available":
                issues.append(f"{label}: default model must be available, not {model.status}")
            if model.capability == "realtime":
                if model.text_only_modality and model.native_audio:
                    issues.append(
                        f"{label}: cannot be both native-audio and text-only; "
                        "LiveKit's separate-TTS path needs a non-native-audio model"
                    )
                if not model.voices and not model.text_only_modality:
                    issues.append(f"{label}: realtime model has no voices and cannot use a separate TTS")
            if model.capability == "tts" and provider.voices and not provider.voices:
                issues.append(f"{label}: tts model on a provider with no voices")

        for capability in CAPABILITIES:
            bucket = provider.models_for(capability)  # type: ignore[arg-type]
            if not bucket:
                continue
            defaults = [m for m in bucket if m.default]
            if len(defaults) > 1:
                issues.append(f"{provider.id}/{capability}: more than one default model")
            if not defaults and all(m.status != "available" for m in bucket):
                issues.append(f"{provider.id}/{capability}: no available model to use as default")

        # Merged voice groups (TTS + Live) can overlap, and a duplicate would show
        # up twice in the settings picker.
        seen_voices: set[str] = set()
        for voice in provider.voices:
            if voice.id in seen_voices:
                issues.append(f"{provider.id}: duplicate voice id {voice.id!r} in provider voice list")
            seen_voices.add(voice.id)

        known_voice_ids = {voice.id for voice in provider.voices}
        for model in provider.models:
            for voice_id in model.voices:
                if provider.voices and voice_id not in known_voice_ids:
                    issues.append(f"{provider.id}/{model.id}: voice {voice_id!r} is not in the provider voice list")

    for reserved in RESERVED_PROVIDER_IDS:
        if reserved in seen_providers:
            issues.append(f"reserved provider id {reserved!r} must not be defined yet")

    # A stage with no seeded default would leave the settings screen guessing,
    # and a guess is how a conversation ends up on an unintended vendor.
    claimed: dict[Capability, list[str]] = {cap: [] for cap in CAPABILITIES}
    for provider in providers if providers is not None else PROVIDERS.values():
        for capability in provider.preferred_for:
            if capability not in claimed:
                issues.append(f"{provider.id}: claims preferred_for unknown capability {capability!r}")
                continue
            if not provider.supports(capability):
                issues.append(f"{provider.id}: claims preferred_for {capability!r} but has no such model")
            claimed[capability].append(provider.id)
    for capability, provider_ids in claimed.items():
        if len(provider_ids) > 1:
            issues.append(
                f"{capability}: more than one preferred provider ({', '.join(sorted(provider_ids))})"
            )

    return issues
