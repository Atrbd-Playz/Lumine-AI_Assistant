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
# Version 8 added per-option ``control``/``minimum``/``maximum``/``step``, so a
# bounded setting draws a slider bounded to the provider's own range instead of
# a free-text field. It also removed Cartesia's ``temperature``, which the plugin
# has no such parameter for -- a profile could set it, validate, and then fail
# with a ``TypeError`` at TTS construction.
# Version 9 added ``authKind``/``tokenPath`` to a probe, and the LiveKit probe
# that uses them. LiveKit has no bearer key: access is a short-lived JWT minted
# from the API key and signed with the API secret, so a URL, a key and a secret
# are one credential. Nothing could test that triple before -- the Providers page
# showed "not testable" and a one-value keyring entry was copied into all three
# variables -- so a wrong LiveKit credential could not be reported, only
# suffered from.
# Version 10 added a per-option ``advanced`` flag, so a stage's screen can lead
# with the settings a person actually chooses and put the rest behind a
# disclosure. Judged per option rather than per provider: Google's TTS and
# Cartesia's both accept a speed that matters, while a token cap matters to
# neither.
# Version 11 declared each model's input modalities, so the settings screen can
# report what a model can actually be given -- and, more usefully, refuse to offer
# a camera to a stack whose frames would arrive nowhere.
# Version 12 added the per-option ``hidden`` flag and ``default``, so a setting
# can exist without being drawn, and a required argument can arrive with the
# value the provider expects rather than with nothing.
# Version 13 added per-option ``models`` and ``integer``. ``models`` says which
# model ids the option does anything on: Cartesia honours ``speed``, ``emotion``
# and ``volume`` only on the sonic-3 family, and without the field the settings
# screen offered sliders that moved and a voice that did not change. ``integer``
# states the wire type of a bounded or enumerated value, which is what the worker
# needs to send ``sample_rate`` as ``24000`` rather than as ``"24000"``.
CATALOG_VERSION = 13

ModelStatus = Literal["available", "deprecated", "retired"]

# Reserved provider id, for inference whose weights ship *with the app*.
#
# It is still not in PROVIDERS, and that is now a narrower gap than it was. Local
# inference did arrive, under the id `ollama` rather than `local`, because the
# two names promise different things: `ollama` means "a server on this machine
# that you started", and `local` would mean "weights bundled with Lumine, working
# with no process of your own". Shipping the first under the second's name would
# have made a promise the app cannot keep -- someone would install it, expect a
# bundled model, and find an empty chooser and a server they had to run.
#
# So `local` stays reserved for the day a model is actually bundled (local
# Whisper, local Kokoro), and `ollama` is what exists.
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
    #: How the credential reaches the request.
    #:
    #: `"header"` is the ordinary case: one secret, placed in one header, as the
    #: four probes above all do. `"livekit_token"` is for LiveKit, which has no API
    #: key at all -- it mints a short-lived JWT from the key *and* the secret and
    #: signs it with the secret, so three values are needed together and none of
    #: them is a bearer token on its own. A probe that copied one value into one
    #: header could not test LiveKit at all, which is why it read "not testable"
    #: while the credential was quietly unable to connect.
    auth_kind: str = "header"
    #: Path appended to the server URL, for the `livekit_token` kind.
    token_path: str = ""

    def headers_for(self, secret: str) -> dict[str, str]:
        headers = {name: value for name, value in self.extra_headers}
        headers[self.auth_header] = f"{self.auth_prefix}{secret}"
        return headers

    def request_body(self) -> str:
        """The body to send, as JSON. Empty for the GET probes that are used."""
        return ""


@dataclass(frozen=True)
class KeySlot:
    """One field of a provider's credential, described for a settings screen.

    A provider with several environment variables needs a field each, and each
    field needs a name a person can act on. ``LIVEKIT_URL`` is documentation;
    "Server URL" is an instruction. Keeping the label here rather than in the
    settings screen is the whole point of the catalog: the desktop app renders
    what it is told and holds no provider knowledge of its own.

    ``kind`` decides how the field is drawn. A ``url`` slot holds a hostname, not
    a secret, so masking it would make a typo invisible -- and a LiveKit API key
    pasted into the server URL is exactly the mistake this class exists to make
    obvious. ``text`` is for a public identifier. ``secret`` is the default.
    """

    env: str
    label: str
    kind: str = "secret"
    help: str = ""

    def __post_init__(self) -> None:
        # `url` is a hostname, not a secret, and drawing it masked is how a key
        # pasted into the wrong field stays invisible. This check is a default
        # rather than a convention because the mistake it prevents is exactly the
        # one that produced the original bug.
        if self.kind == "url" and not self.env.endswith(("_URL", "_URI", "_ENDPOINT")):
            raise ValueError(
                f"key slot {self.env!r} is drawn as a server address, so its "
                f"variable name should end in _URL, _URI or _ENDPOINT -- otherwise "
                f"a real secret could be shown in the clear"
            )


#: How a credential field may be drawn. The settings screen has exactly these
#: three, and a fourth is a shape it cannot render.
KEY_SLOT_KINDS: frozenset[str] = frozenset({"secret", "text", "url"})


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
        minimum: The lowest value a numeric setting accepts. Declaring it does two
            jobs -- the settings screen draws a slider bounded to exactly the
            provider's own range, and `is_valid` rejects a number the provider
            would refuse. A bound discovered only in a provider's runtime is a
            `TypeError` or a 400 at the moment somebody presses Start.
        maximum: The highest value a numeric setting accepts.
        step: The granularity a slider should snap to, in the setting's own units.
        control: Overrides the widget the settings screen would infer from
            `values` and the numeric bounds. Needed for `voice`, whose choices
            come from the model's voice catalog rather than from `values`.
        keyword: The plugin's own parameter name, when it differs from `name`.
            Lumine says `voice` and `speed` everywhere so a profile reads the
            same whichever provider is selected, but Google's TTS calls those
            `voice_name` and `speaking_rate`. Without this, a Google TTS profile
            passes a key the plugin does not accept and fails at construction.
        advanced: Whether this belongs behind a disclosure rather than on the
            first screen of the stage that owns it.

            The test is not "is it numeric" -- speed is numeric and is one of the
            two controls a voice app exists to expose. The test is whether the
            provider's own default is a good answer. Temperature, token caps,
            top-p and volume are knobs whose sensible value depends on the model
            and the prompt, and a wrong setting of any of them makes replies
            worse in ways that are hard to attribute. Thinking level, speed,
            emotion, voice and language have defaults a person can predict, so
            they stay visible.
        default: A value to send when the profile sets none.

            The provider's own default is not always expressible as an absent
            argument. Ollama's OpenAI-compatible server has no authentication, but
            the OpenAI client refuses to construct without a key -- so Lumine
            sends the literal string ``"ollama"``, which the server ignores and
            which nobody should ever be asked to type. Declaring the default here
            is what keeps that out of the settings screen and out of a profile.
        hidden: Sent, but not drawn.

            For a default the user cannot usefully change, and which would be
            confusing if they could: an ``api_key`` field on a provider that has
            no key reads as a broken setup wizard.
    """

    name: str
    values: tuple[str, ...] = ()
    nest: str = ""
    companions: tuple[str, ...] = ()
    notes: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    control: str = ""
    keyword: str = ""
    advanced: bool = False
    default: str | float | bool | None = None
    hidden: bool = False
    models: tuple[str, ...] = ()
    integer: bool = False

    @property
    def plugin_keyword(self) -> str:
        """The keyword to hand the plugin. Usually the same as `name`."""
        return self.keyword or self.name

    def applies_to(self, model_id: str) -> bool:
        """Whether this option does anything at all on `model_id`.

        Declared rather than inferred, because whether a setting is honoured is
        a fact about the plugin, not about the setting's name. Cartesia is the
        case that made this necessary: its request builder sends `speed`,
        `emotion` and `volume` only on the sonic-3 path, so a profile on
        `sonic-2` can save all three, pass validation, and have the API ignore
        every one of them. The user then turns a slider and hears nothing change,
        which is worse than not offering the slider -- the control has started
        lying.

        An empty tuple means "every model that declares it", which is the case
        for every option except those a plugin gates on the model id.
        """
        return not self.models or model_id in self.models

    def coerce(self, raw: object) -> object | None:
        """The value to hand the plugin, or `None` to send nothing at all.

        Settings reach here from three places that type them differently. The
        settings screen writes JSON, so every number and every flag it saves
        arrives as a *string*. The environment writes floats. A catalog default
        is whatever the declaration said. The plugin tolerates none of that
        spread: Cartesia raises `ValueError: speed must be a float for sonic-3`
        the moment a profile-built `TTS` is constructed, and a stage that throws
        during session setup is indistinguishable, to the person waiting for a
        reply, from a microphone that never worked.

        So the coercion lives here rather than in the settings screen: this is
        the one place every path passes through, including the environment-derived
        profile that never sees the UI at all.

        Three rules, in the order they can apply:

        - an enumerated value is matched case-insensitively but returned in the
          *declared* spelling. Cartesia's emotions are `Happy`, not `happy`, and
          the old lowercasing sent the API a word it does not have.
        - a bounded value is parsed, clamped to the bound, and rounded to an int
          when the option is declared `integer`. Clamping rather than dropping:
          "replaced with the closest one it does" is what the docstring on
          `build_options` has always promised, and a rate that snaps back to
          0.6 is a better answer to an impossible 0.1 than silence.
        - a switch parses the words a checkbox can be saved as. Any other string
          is not a boolean, and guessing would turn a typo into a feature that
          is on.

        Anything left over is forwarded untouched: free-form options are strings
        by nature, and `voice` ids must not be reinterpreted.
        """
        if self.values:
            declared = {candidate.lower(): candidate for candidate in self.values}
            found = declared.get(str(raw).strip().lower())
            value = found if found is not None else self.first_allowed()
            if value is None:
                return None
            if not self.integer:
                return value
            # An enumerated option can still be an integer on the wire --
            # Cartesia's sample rates are a fixed set of *numbers*, and handing
            # the API `"24000"` is the same mistake as handing it a float.
            try:
                return int(float(value))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        if self.minimum is not None or self.maximum is not None:
            try:
                number = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
            if self.minimum is not None:
                number = max(self.minimum, number)
            if self.maximum is not None:
                number = min(self.maximum, number)
            return int(round(number)) if self.integer else number

        if self.control_kind == "switch":
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off", ""}:
                return False
            return None

        return raw

    @property
    def control_kind(self) -> str:
        """The widget that fits this option: slider, select, combobox, switch,
        number, voice, or text.

        Derived rather than declared, so an option added without a `control` still
        renders as something sensible instead of as a bare text field.
        """
        if self.control:
            return self.control
        if self.name == "voice":
            return "voice"
        if self.values:
            # A handful of values reads as a list of buttons. A few dozen does
            # not -- Cartesia's sonic-3 has 53 named emotions, and showing them
            # as 53 rows is a worse experience than typing the first letters.
            return "select" if len(self.values) <= 8 else "combobox"
        if self.minimum is not None and self.maximum is not None:
            return "slider"
        if self.name.startswith(("is_", "enable_")) or self.name.endswith(("_flag",)):
            return "switch"
        return "text"

    def is_valid(self, value: object) -> bool:
        if self.values:
            return str(value).strip().lower() in {v.lower() for v in self.values}
        if isinstance(value, bool):
            return True
        if self.minimum is not None or self.maximum is not None:
            # A bounded option only accepts a number inside the bound. The
            # catalog is where a provider's range is known, so it is where an
            # out-of-range value is caught -- a profile that saves, validates and
            # then fails at TTS construction is the bug these bounds prevent.
            if isinstance(value, str):
                try:
                    value = float(value)
                except ValueError:
                    return False
            if not isinstance(value, (int, float)):
                return False
            if self.minimum is not None and value < self.minimum:
                return False
            if self.maximum is not None and value > self.maximum:
                return False
            return True
        return isinstance(value, (str, int, float, bool))

    def first_allowed(self) -> str | None:
        """The lowest-cost value this option accepts, or ``None`` if free-form.

        Returned in the spelling the catalog declared. The scan is over lowercase
        names because the preference order is a fact about cost, not about
        capitalisation, but the answer has to come back as the provider spells
        it -- `coerce`'s whole contract is that an enumerated value leaves here
        the way it was written down.
        """
        for candidate in ("minimal", "low", "medium", "high"):
            for declared in self.values:
                if declared.lower() == candidate:
                    return declared
        return self.values[0] if self.values else None


#: The kinds of input a model stage can be given. Closed because the settings
#: screen draws a fixed matrix of them; a fourth kind would be a fourth column
#: nobody has a row for.
MODALITIES: frozenset[str] = frozenset({"text", "audio", "image", "video"})

#: What a stage of each capability accepts when its model does not say otherwise.
#:
#: Transcription hears audio, a language model reads text, synthesis is handed
#: text, and a voice-activity detector hears audio. The realtime family is the
#: exception and always declares its own, because a native-audio model that can
#: also see is the difference between a voice assistant and something else.
CAPABILITY_MODALITIES: dict[Capability, tuple[str, ...]] = {
    "stt": ("audio",),
    "llm": ("text",),
    "tts": ("text",),
    "vad": ("audio",),
    "transport": (),
    "realtime": ("text", "audio"),
}


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

    # --- input modalities ---
    # What Lumine can feed this model on the path it is reached by, which is not
    # always what the model could accept in principle. That distinction is the
    # whole point of the field: Google's Gemini 3 Flash takes images and video
    # through the Generative API, but the LiveKit LLM stage sends it a chat
    # history of strings, so a camera turned on beside a pipeline stage delivers
    # frames to nobody.
    #
    # Left empty, the capability decides -- see ``CAPABILITY_MODALITIES``. The
    # realtime family declares its own, because a native-audio model that cannot
    # see is a different product from one that can, and that is exactly the fact
    # a person needs before deciding whether to turn a camera on.
    input_modalities: tuple[str, ...] = ()

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
    # This is per-model on purpose, and the Live API is not an exception to that.
    # Google rejects a level a given model does not accept with a 400, and the
    # sets are not nested: `gemini-3.8-flash` takes low/medium/high but not
    # minimal, `gemini-3.8-live` takes no level at all, and
    # `gemini-3.8-live-extended-thinking` takes low/medium/high but not minimal.
    # A single "Live API" set is wrong for two of the three.
    thinking_levels: tuple[str, ...] = ()
    # The settings this model accepts, described. Drives both the factory (which
    # values to forward, and in what shape) and validation (which values are
    # legal), so neither has to know which provider it is looking at.
    options: tuple[OptionDefinition, ...] = ()

    def __post_init__(self) -> None:
        if not self.input_modalities:
            # Frozen, so the default is written the only way a frozen dataclass
            # allows. Declaring a default per capability keeps thirty model
            # definitions from repeating the same four words.
            object.__setattr__(self, "input_modalities", CAPABILITY_MODALITIES.get(self.capability, ()))
        unknown = [name for name in self.input_modalities if name not in MODALITIES]
        if unknown:
            raise ValueError(
                f"{self.id}: unknown input modalit{'y' if len(unknown) == 1 else 'ies'} "
                f"{unknown}; expected values from {sorted(MODALITIES)}"
            )

    @property
    def sees(self) -> bool:
        """Whether a camera frame would reach this model."""
        return "image" in self.input_modalities

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

        Two gates run before anything is forwarded, and both exist because of
        failures that were silent rather than loud:

        - an option that `applies_to` rejects is not sent even though the model
          declares it. This is the Cartesia case -- `speed` is real on sonic-3
          and ignored everywhere else -- and the alternative was a slider that
          moved and a voice that did not change.
        - every value goes through `coerce`, because the settings screen hands
          over strings and the plugin hands those strings straight to an API
          that wants numbers and the declared spelling of its enums.
        """
        options: dict[str, Any] = {}
        nested: dict[str, dict[str, Any]] = {}

        for definition in self.options:
            if not definition.applies_to(self.id):
                continue

            raw = settings.get(definition.name)
            if raw is None:
                # Absent means the provider applies its own default, which is
                # valid by construction. Nothing is invented here -- except where
                # the catalog spelled one out, which is the case for a required
                # argument the provider validates but has no default for.
                if definition.default is None:
                    continue
                raw = definition.default

            value = definition.coerce(raw)
            if value is None:
                # Not interpretable as this option's type. The provider's own
                # default is the honest answer: sending the uninterpreted value
                # is what raised at construction time.
                continue

            if definition.nest:
                group = nested.setdefault(definition.nest, {})
                group[definition.plugin_keyword] = value
                for companion in definition.companions:
                    extra = settings.get(companion)
                    if extra is not None:
                        group[companion] = extra
            else:
                options[definition.plugin_keyword] = value

        options.update(nested)
        return options


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
    #: Field descriptions for a multi-variable credential. Empty means "one field
    #: named after the provider", which is right for every single-key provider.
    key_slots: tuple[KeySlot, ...] = ()

    def slots(self) -> tuple[KeySlot, ...]:
        """The fields to draw for this provider's credential.

        Derived from ``key_env`` when not spelled out, so a provider that gains a
        second variable gets a second field automatically instead of silently
        collapsing back to one -- which is what made a three-part credential
        storable but unusable.
        """
        if self.key_slots:
            return self.key_slots
        return tuple(
            KeySlot(env=env, label=self.label if len(self.key_env) == 1 else env)
            for env in self.key_env
        )

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
    # This is the voice Lumine has always used, and it is the catalog's default so
    # that a newly seeded profile sounds like Lumine. The plugin's own default
    # (Katie) is kept below for anyone who prefers it -- but marking that one
    # default here meant a fresh profile did not sound like the assistant it is
    # named after, and disagreed with `pipeline_config.DEFAULT_CARTESIA_VOICE`.
    VoiceDefinition(
        id="002622d8-19d0-4567-a16a-f99c7397c062",
        label="Lumine",
        default=True,
        notes="Lumine's own voice, and what the legacy cascade has used since it "
        "was added.",
    ),
    VoiceDefinition(
        id="f786b574-daa5-4673-aa0c-cbe3e8534c02",
        label="Calm",
        notes="The Cartesia plugin's own default voice.",
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
    # No default here, and that is deliberate. This list is merged with the Live
    # one into a single provider catalogue, and the provider may name exactly one
    # default -- `_dedupe_voices` demotes the second. Marking one in both lists is
    # how "which voice does Google default to?" came to have two answers that
    # disagreed: the per-model list said `Sulafat` and the merged list said
    # whichever came first here. The default is declared once, below.
    VoiceDefinition(id="Zephyr", label="Zephyr"),
    VoiceDefinition(id="Kore", label="Kore", notes="Plugin default voice."),
    VoiceDefinition(id="Aoede", label="Aoede"),
)

_GEMINI_LIVE_VOICES = (
    # Gemini Live and Gemini TTS draw on the same named voice set, so every voice
    # the catalog knows is offered here. The list is curated: an unknown name is a
    # warning rather than an error, and the settings screen also accepts one typed
    # by hand, so a voice missing from this list is never a dead end.
    #
    # `Sulafat` is Google's default across the catalogue because it is the voice
    # the worker ships: `DEFAULT_GEMINI_VOICE` in `pipeline_config.py` and the
    # `.env.example` both name it, so a profile with no voice set has to agree
    # with the environment-derived one or the same provider speaks two ways
    # depending on where the setting came from.
    VoiceDefinition(id="Sulafat", label="Sulafat", default=True),
    VoiceDefinition(id="Puck", label="Puck"),
    VoiceDefinition(id="Zephyr", label="Zephyr"),
    VoiceDefinition(id="Kore", label="Kore"),
    VoiceDefinition(id="Aoede", label="Aoede"),
)


#: The Cartesia models that actually forward ``speed``, ``emotion`` and
#: ``volume``.
#:
#: This is a fact about the plugin's request builder, not about those settings.
#: Its sonic-3 path writes them into ``generation_config``; every other model
#: falls through to a legacy body that is only built when the API version is the
#: 2024 one, which it never is by default. A profile on ``sonic-2`` can therefore
#: save all three, validate cleanly, and have the API ignore every one of them --
#: which turns the settings screen's sliders into decoration. Declaring the gate
#: here lets ``build_options`` leave them out and let the settings screen say
#: why, instead of offering a control that does nothing.
#:
#: ``test_providers.py`` asserts the set matches the plugin's own
#: ``_is_sonic_3`` predicate.
_CARTESIA_SONIC_3_MODELS: tuple[str, ...] = ("sonic-3", "sonic-3.5", "sonic-3.6")


#: Cartesia sonic-3's named emotions, transcribed from the plugin's own
#: ``TTSVoiceEmotion`` Literal. Copied rather than imported so this module stays
#: free of a livekit dependency -- it is the catalog the settings screen reads,
#: and it has to be importable and testable on its own.
#: ``test_providers.py`` asserts the copy still matches the plugin, so the two
#: cannot drift apart silently.
_CARTESIA_SONIC_3_EMOTIONS: tuple[str, ...] = (
    "Happy",
    "Excited",
    "Enthusiastic",
    "Elated",
    "Euphoric",
    "Triumphant",
    "Amazed",
    "Surprised",
    "Flirtatious",
    "Joking/Comedic",
    "Curious",
    "Content",
    "Peaceful",
    "Serene",
    "Calm",
    "Grateful",
    "Affectionate",
    "Trust",
    "Sympathetic",
    "Anticipation",
    "Mysterious",
    "Angry",
    "Mad",
    "Outraged",
    "Frustrated",
    "Agitated",
    "Threatened",
    "Disgusted",
    "Contempt",
    "Envious",
    "Sarcastic",
    "Ironic",
    "Sad",
    "Dejected",
    "Melancholic",
    "Disappointed",
    "Hurt",
    "Guilty",
    "Bored",
    "Tired",
    "Rejected",
    "Nostalgic",
    "Wistful",
    "Apologetic",
    "Hesitant",
    "Insecure",
    "Confused",
    "Resigned",
    "Anxious",
    "Panicked",
    "Alarmed",
    "Scared",
    "Neutral",
    "Proud",
    "Confident",
    "Distant",
    "Skeptical",
    "Contemplative",
    "Determined",
)


def _dedupe_voices(*groups: tuple[VoiceDefinition, ...]) -> tuple[VoiceDefinition, ...]:
    """Merge voice groups, keeping the first definition of each id.

    Gemini Live and Gemini TTS draw on the same named voices, so concatenating
    the two lists would surface duplicates in the settings picker.

    ## Exactly one voice may be the default

    Merging two lists that each named a default produced a provider with two, and
    every consumer asking "which voice does this default to?" then answered with
    whichever it happened to reach first. That is not a defensible answer to a
    question a user can also ask of the same provider by naming a model, so the
    rule is enforced here rather than trusted to the callers: the first default in
    merge order wins and every later one is demoted.

    Being a guard rather than a fix, it also means a future list can mark its own
    default without anyone having to remember to demote it.
    """
    seen: set[str] = set()
    merged: list[VoiceDefinition] = []
    default_claimed = False
    for group in groups:
        for voice in group:
            if voice.id in seen:
                continue
            seen.add(voice.id)
            if voice.default:
                if default_claimed:
                    voice = replace(voice, default=False)
                else:
                    default_claimed = True
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

# The Live API takes the same parameter, but the set is per-model even here, and
# one family's set is wrong for the next. Google's own Live thinking table, at
# https://ai.google.dev/gemini-api/docs/live-api/thinking, is the authority:
#
#   gemini-3.8-live                    `thinking_level` NOT supported
#   gemini-3.8-live-extended-thinking  low / medium / high, `minimal` NOT supported
#
# Both facts were previously flattened into one family-wide set, which sent
# `minimal` to a model that rejects the parameter outright. Google answers that
# with a 1007 at session setup, so the greeting never arrives and the session
# closes with no audio -- indistinguishable, to a user, from a broken microphone.
_GOOGLE_LIVE_LEVELS = ("minimal", "low", "medium", "high")
_GOOGLE_LIVE_THINKING_LEVELS = ("low", "medium", "high")

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
        OptionDefinition(
            name="temperature",
            minimum=0.0,
            maximum=2.0,
            step=0.05,
            notes="Higher is more varied, lower is more predictable.",
            advanced=True,
        ),
        OptionDefinition(
            name="max_output_tokens",
            notes="A hard cutoff that includes reasoning tokens.",
            advanced=True,
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
            minimum=0.0,
            maximum=2.0,
            step=0.05,
            notes="Higher is more varied, lower is more predictable.",
            advanced=True,
        ),
        OptionDefinition(
            name="max_output_tokens",
            notes="A hard cutoff that includes reasoning tokens.",
            advanced=True,
        ),
    ),
    ("google", "stt"): (
        # Google's plugin spells this `languages` and takes a list, coercing a
        # bare string, so Lumine's singular name maps across unchanged.
        OptionDefinition(
            name="language",
            keyword="languages",
            notes="Spoken language, as a locale code.",
        ),
    ),
    ("google", "tts"): (
        OptionDefinition(name="language", notes="Spoken language, as a locale code."),
        # Google's plugin names these differently. Lumine's names are kept so a
        # profile reads the same whichever provider is selected; `keyword`
        # translates on the way into the plugin.
        OptionDefinition(
            name="speed",
            keyword="speaking_rate",
            minimum=0.5,
            maximum=2.0,
            step=0.05,
            notes="Rate of speech. Google accepts 0.25-4.0; Lumine offers a "
            "narrower band because anything outside it is not worth hearing.",
        ),
        OptionDefinition(
            name="voice",
            keyword="voice_name",
            notes="Which voice speaks the reply.",
        ),
    ),
    ("cartesia", "tts"): (
        OptionDefinition(
            name="speed",
            minimum=0.6,
            maximum=2.0,
            step=0.05,
            models=_CARTESIA_SONIC_3_MODELS,
            notes="Rate of speech. Must be a number for sonic-3 -- the named "
            "speeds are a legacy alias the plugin no longer forwards. "
            "Sent on the sonic-3 family only; older models drop it.",
        ),
        OptionDefinition(
            name="emotion",
            values=_CARTESIA_SONIC_3_EMOTIONS,
            models=_CARTESIA_SONIC_3_MODELS,
            # A middle setting rather than a mood. Cartesia's own neutral reading
            # is a voice with nothing in it, and a companion that answers a quiet
            # question in a flat register reads as absent -- which is the same
            # complaint as having no expressiveness at all, one turn later. It is
            # a default and not a floor: per-turn emotion overrides it while a
            # reply demonstrates one and returns to it when the reply does not.
            default="Content",
            notes="How the delivery is coloured. Content is a warm middle "
            "setting rather than a mood, so a quiet reply does not sound flat; "
            "a reply that demonstrates an emotion uses that instead, and comes "
            "back to this afterwards. Clear the field for Cartesia's own "
            "neutral reading. sonic-3 family only.",
        ),
        OptionDefinition(
            name="volume",
            minimum=0.5,
            maximum=2.0,
            step=0.05,
            models=_CARTESIA_SONIC_3_MODELS,
            notes="Loudness relative to 1.0. Best left alone: the room's own "
            "output gain is the right control for overall level. "
            "sonic-3 family only.",
            advanced=True,
        ),
        OptionDefinition(name="language", notes="Spoken language, as a locale code."),
        OptionDefinition(name="voice", notes="Which voice speaks the reply."),
        OptionDefinition(
            name="sample_rate",
            # Enumerated rather than a slider: Cartesia accepts a fixed set of
            # rates, and a slider from 8000 to 48000 would let someone pick
            # 12345, which the API refuses -- at synthesis time, in a session,
            # for a reason the screen never mentioned.
            values=("8000", "16000", "22050", "24000", "44100", "48000"),
            integer=True,
            control="select",
            default="24000",
            notes="Sample rate of the audio the synthesis returns. 24000 is the "
            "plugin's default and what LiveKit expects; higher rates cost more "
            "bandwidth for a difference a phone speaker cannot show.",
            advanced=True,
        ),
        OptionDefinition(
            name="word_timestamps",
            control="switch",
            default="true",
            notes="Ask Cartesia for word-level timing. Useful to anything that "
            "aligns captions; it does not change how the reply sounds.",
            advanced=True,
        ),
        OptionDefinition(
            name="text_pacing",
            control="switch",
            default="false",
            notes="Stream the reply through a sentence pacer, so a long answer "
            "reaches the microphone in pieces instead of one block.",
            advanced=True,
        ),
        OptionDefinition(
            name="pronunciation_dict_id",
            control="text",
            notes="ID of a pronunciation dictionary in your Cartesia account, "
            "applied on top of the chosen voice.",
            advanced=True,
        ),
    ),
    ("groq", "llm"): (
        OptionDefinition(
            name="temperature",
            minimum=0.0,
            maximum=2.0,
            step=0.05,
            notes="Higher is more varied, lower is more predictable.",
            advanced=True,
        ),
        OptionDefinition(
            name="max_completion_tokens",
            notes="A hard cap on the reply.",
            advanced=True,
        ),
        OptionDefinition(
            name="top_p",
            minimum=0.0,
            maximum=1.0,
            step=0.01,
            notes="Narrower than temperature for the same effect. Leave at 1.0 "
            "unless you have a reason.",
            advanced=True,
        ),
        OptionDefinition(name="parallel_tool_calls", advanced=True),
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
        # Turn detection is four overlapping timeouts. They matter -- get them
        # wrong and Lumine either cuts you off mid-word or never notices you
        # finished -- but they are tuning, not a choice somebody makes on purpose,
        # so all four sit behind the disclosure.
        OptionDefinition(name="min_speech_duration", advanced=True),
        OptionDefinition(name="min_silence_duration", advanced=True),
        OptionDefinition(name="prefix_padding", advanced=True),
        OptionDefinition(name="activation_threshold", advanced=True),
    ),
    ("ollama", "llm"): (
        # A locally served model, reached over the OpenAI-compatible API.
        #
        # `base_url` is the only setting here that is really a question, and it is
        # *not* advanced: a wrong address is the whole failure mode of a local
        # provider, and the one value somebody needs to be able to change without
        # opening a disclosure. Ollama's default is the port everyone else's
        # server is usually configured to imitate, so it is the right guess and
        # still a guess.
        OptionDefinition(
            name="base_url",
            default="http://localhost:11434/v1",
            notes="Where the model server is listening. Ollama's default is "
            "http://localhost:11434/v1; LM Studio, llama.cpp and vLLM speak the "
            "same protocol on a port you choose.",
        ),
        # Sent, never drawn. The OpenAI client refuses to construct without a
        # key; this server has no authentication and ignores the value. Drawing
        # the field would present a non-problem as a setup step.
        OptionDefinition(
            name="api_key",
            default="ollama",
            hidden=True,
            notes="Not a credential. The OpenAI client requires the argument and "
            "this server ignores it.",
        ),
        OptionDefinition(
            name="temperature",
            minimum=0.0,
            maximum=2.0,
            step=0.05,
            notes="Higher is more varied, lower is more predictable.",
            advanced=True,
        ),
        OptionDefinition(
            name="top_p",
            minimum=0.0,
            maximum=1.0,
            step=0.01,
            notes="Narrower than temperature for the same effect. Leave at 1.0 "
            "unless you have a reason.",
            advanced=True,
        ),
        OptionDefinition(name="parallel_tool_calls", advanced=True),
        # Deliberately absent: `max_completion_tokens`. OpenAI's newer parameter
        # is not understood by every local server, and a rejected keyword is a
        # 400 on the first spoken turn -- indistinguishable from a muted
        # microphone. A local model's own context length is the right cap.
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
    # LiveKit is the odd one out: there is no API key to send. Access is a JWT
    # minted from the key and signed with the secret, so the three values only
    # mean anything together and the only honest test is to make a request with
    # all three. `ListRooms` is the cheapest authenticated call there is -- it
    # returns 200 and an empty list on a new project, and 401 for a token it will
    # not accept, which separates "the URL is wrong" (a connection error) from
    # "the key or secret is wrong" (401) from "it works" (200).
    "livekit": ProbeDefinition(
        url="",
        auth_kind="livekit_token",
        token_path="/twirp/livekit.RoomService/ListRooms",
        method="POST",
        # A malformed or unsigned token is rejected with 401; an expired one with
        # something in the same family. Anything else is not about the credential.
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
        # Three fields, and the first one is not a secret. The ordering is the
        # order a person copies them out of the console in.
        key_slots=(
            KeySlot(
                env="LIVEKIT_URL",
                label="Server URL",
                kind="url",
                help="From your LiveKit project. A wss:// or https:// address, "
                "not your API key.",
            ),
            KeySlot(
                env="LIVEKIT_API_KEY",
                label="API key",
                help="From the same page, under API Keys.",
            ),
            KeySlot(
                env="LIVEKIT_API_SECRET",
                label="API secret",
                help="Reveal it with the eye icon next to the key. It is shown "
                "once, at creation.",
            ),
        ),
        models=(
            ModelDefinition(
                id="livekit-cloud",
                label="LiveKit Cloud",
                capability="transport",
                default=True,
                notes="The only transport Lumine currently supports.",
            ),
        ),
        probe=_PROBES["livekit"],
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
            # All four Live models take image and video on top of text and audio.
            # That is the capability that makes a camera worth having at all, and
            # it is the only stage in the catalog where a frame goes anywhere.
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
                input_modalities=("text", "audio", "image", "video"),
                voices=tuple(voice.id for voice in _GEMINI_LIVE_VOICES),
                # Deliberately empty. This model reasons with a fixed latency
                # profile and is not configured by level at all; Google rejects
                # `thinking_config` for it outright. Empty means "send no level",
                # which is the only thing that works here.
                thinking_levels=(),
                notes=(
                    "Google's default Live API model, and the one Google recommends "
                    "over 3.1. Native audio, so it speaks for itself. LiveKit's "
                    "separate-TTS path needs a non-native-audio model, which no "
                    "current Gemini Live model is. Reasoning depth is not "
                    "configurable here -- use Gemini 3.8 Live (thinking) for that."
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
                input_modalities=("text", "audio", "image", "video"),
                voices=tuple(voice.id for voice in _GEMINI_LIVE_VOICES),
                # No `minimal` here. This is the one Live model that takes a
                # level, and Google documents its floor as `low`; sending the
                # family's `minimal` default is the same 1007 as above.
                thinking_levels=_GOOGLE_LIVE_THINKING_LEVELS,
                notes=(
                    "The high-reasoning Live model, for when the background "
                    "reasoning matters more than the delay before the first word. "
                    "Same native-audio limits as Gemini 3.8 Live. Reasoning runs "
                    "low to high; there is no minimal."
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
                input_modalities=("text", "audio", "image", "video"),
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
                input_modalities=("text", "audio", "image", "video"),
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
    "ollama": ProviderDefinition(
        id="ollama",
        label="Local model",
        # Ollama has no API key, no account and no signup. Asking for one is the
        # single most effective way to stop somebody using a model that costs them
        # nothing, and it is also the fastest way to a person concluding this
        # section of the app is broken.
        requires_key=False,
        key_env=(),
        local=True,
        setup_url="https://ollama.com/download",
        notes=(
            "A model server on this machine, reached over the OpenAI-compatible "
            "API. No credential, no network call, no data leaves the computer."
        ),
        # Not `preferred_for`: this is opt-in, not the app's default. A local
        # 8B model is a worse conversation partner than Groq's hosted one, and
        # quietly switching a saved profile onto it because it sorts first would
        # be Lumine making that trade for the user.
        models=(
            ModelDefinition(
                id="llama3.2:3b",
                label="Llama 3.2 3B",
                capability="llm",
                notes="Small enough to answer on a laptop CPU. Pull it with "
                "`ollama pull llama3.2:3b`.",
            ),
            ModelDefinition(
                id="llama3.1:8b",
                label="Llama 3.1 8B",
                capability="llm",
                notes="The usual first choice. Wants a machine with 16GB of memory.",
            ),
            ModelDefinition(
                id="qwen3:8b",
                label="Qwen 3 8B",
                capability="llm",
            ),
            ModelDefinition(
                id="gemma3:12b",
                label="Gemma 3 12B",
                capability="llm",
                notes="Wants 24GB, or a quantisation you have to look up.",
            ),
            # Not listed: the hundreds of other models. The point of this provider
            # is that the chooser asks the server what it has -- `agent/local_models.py`
            # -- so the catalog is a set of things to offer before anyone has
            # opened Discovery, not a list pretending to be complete.
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
        # What Lumine can hand this model. The settings screen draws a capability
        # matrix from it, and the camera control reads it to decide whether to
        # offer itself -- a button that turns on a track nothing consumes is the
        # one control in the app that would be lying.
        "inputModalities": list(model.input_modalities),
        # Empty means the model is not configured by thinking level, and the
        # worker sends none. The settings screen offers only what is listed here.
        "thinkingLevels": list(model.thinking_levels),
        "options": [
            {
                "name": definition.name,
                "values": list(definition.values),
                "nest": definition.nest,
                "notes": definition.notes,
                # The range and the widget travel with the option so the settings
                # screen can draw a bounded slider instead of a free-text box
                # that only tells you it was wrong after you pressed Save.
                "control": definition.control_kind,
                "minimum": definition.minimum,
                "maximum": definition.maximum,
                "step": definition.step,
                # Whether a bounded or enumerated value leaves the worker as an
                # integer. Only the worker cares -- the settings screen writes
                # strings either way -- but the catalog is also read by tests
                # and diagnostics, and an option the wire type of which is not
                # stated is an option nobody can check.
                "integer": definition.integer,
                # Models this option does anything on. Empty means all of them.
                #
                # This is the Cartesia gate: `speed`, `emotion` and `volume` are
                # real on the sonic-3 family and silently ignored everywhere
                # else, so the settings screen dims them with a reason rather
                # than drawing a slider whose turn produces no change. The UI
                # has no idea what Cartesia does; only the catalog does.
                "models": list(definition.models),
                # Whether the settings screen hides this behind a disclosure. The
                # judgement is made here, in the catalog, because only the catalog
                # knows what a setting means for the model that accepts it -- a
                # `temperature` that is ordinary on one model can be meaningless on
                # another, and the frontend cannot work that out from the name.
                "advanced": definition.advanced,
                # A value Lumine sends when the profile sets none, and a flag
                # saying the settings screen should not draw the field at all.
                # Together they are how a required argument that is not a choice
                # -- Ollama's ignored `api_key` -- reaches the plugin without
                # appearing as a box somebody has to fill in wrongly.
                "default": definition.default,
                "hidden": definition.hidden,
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
        "authKind": probe.auth_kind,
        # Empty for the ordinary header probes, which have no path to append.
        "tokenPath": probe.token_path,
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
        # How to draw a field per variable. `slots()` fills this in from `keyEnv`
        # for a single-key provider, so the frontend never has to know that most
        # providers need one field and one happens to need three.
        "keySlots": [
            {
                "env": slot.env,
                "label": slot.label,
                "kind": slot.kind,
                "help": slot.help,
            }
            for slot in provider.slots()
        ],
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

        # A slot is the UI's field for one variable. If the two disagree, a field
        # is drawn for a variable the worker never reads, or a variable is read
        # with no field to enter it into -- and in the second case the credential
        # simply cannot be completed.
        for slot in provider.key_slots:
            if slot.env not in provider.key_env:
                issues.append(
                    f"{provider.id}: slot {slot.env!r} is not one of its key_env "
                    f"variables"
                )
            if not slot.label:
                issues.append(f"{provider.id}: slot {slot.env} has no label")
            if slot.kind not in KEY_SLOT_KINDS:
                issues.append(
                    f"{provider.id}: slot {slot.env} has unknown kind {slot.kind!r}"
                )
        if provider.key_slots and len(provider.key_slots) != len(provider.key_env):
            issues.append(
                f"{provider.id}: declares {len(provider.key_env)} credential "
                f"variable(s) but {len(provider.key_slots)} slot(s)"
            )
        if not provider.requires_key and provider.key_slots:
            issues.append(f"{provider.id}: declares slots but needs no credential")

        probe = provider.probe
        if probe is not None:
            if not provider.requires_key:
                issues.append(f"{provider.id}: has a probe but needs no credential")
            if probe.auth_kind == "livekit_token":
                # A minted-token probe has no fixed address: the host is the
                # credential being tested, so "the url must be https" is not a
                # property of the catalog. What must hold is that the probe
                # declares the path it appends, and that the provider supplies
                # the several values minting needs -- a URL alone cannot sign
                # anything, which is the mistake this auth kind exists to avoid.
                if not probe.token_path.startswith("/"):
                    issues.append(
                        f"{provider.id}: token probe path must be a rooted path"
                    )
                if len(provider.key_env) < 2:
                    issues.append(
                        f"{provider.id}: token probe needs a server address and a "
                        f"key pair, but declares {len(provider.key_env)} variable(s)"
                    )
            else:
                if not probe.url.startswith("https://"):
                    # A probe sends the user's credential to this address. Plain
                    # HTTP would put it on the wire in the clear.
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
