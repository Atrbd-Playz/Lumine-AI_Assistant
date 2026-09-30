"""Lumine's emotions, expressed in the voice and not only in the face.

The avatar already reacts to what the model says. This is the same signal, one
stage earlier, handed to the synthesizer before it speaks the reply.

Two facts about this module are worth stating up front, because both were easy
to get wrong and both change what the feature can promise.

**It only reaches `legacy_cascade`.** The default profile is `gemini_live`, a
native-audio model that synthesizes its own voice and has no separate TTS to
talk to. Everything here is inert there. The settings screen says so rather
than implying the voice is being shaped when it is not.

**It does not turn on LiveKit's `expressive` mode.** That mode teaches the LLM
to write inline delivery tags which the TTS renders, and Cartesia's `sonic-3`
declares exactly that dialect (`_CARTESIA_TAGS` is `emotion`, `speed`,
`volume`, `break`, `spell`). The framework still refuses to activate it for
Cartesia: `AgentActivity._resolve_expressive_options` returns `None` for a
natively streaming plugin that is not handed to it as a `StreamAdapter`,
because such a plugin owns its own input task and would *speak the tags
aloud*. That is a deliberate guard, not an oversight, so this module works
around neither it nor the reason behind it. Wrapping Cartesia in a
`StreamAdapter` would be the way in, and it would also move synthesis from
token streaming to sentence batching — a latency decision nobody has taken
yet.

What is left is the blunter control Cartesia exposes as configuration: a
per-turn `emotion`. It is applied to the text *as it streams*, before that
text is yielded to the pipeline, so it is not a reply behind.
"""

from __future__ import annotations

import logging
import weakref
from typing import Any

try:  # package import (tests, `python -m agent`)
    from .emotion_contract import extract_emotion_sequence
except ImportError:  # script import (`python agent/agent.py`)
    from runtime.emotion_contract import extract_emotion_sequence

logger = logging.getLogger("lumine.emotion_voice")

# The model family that accepts voice controls at all. The plugin's own
# `_is_sonic_3` is `model.startswith("sonic-3")`; below it, `update_options`
# only logs a warning that the controls were ignored, so applying them anyway
# would look like expressiveness and deliver silence.
SUPPORTED_MODEL_PREFIX = "sonic-3"

# Cartesia's own voice-control vocabulary — the same list the plugin spells
# `TTSVoiceEmotion` — keyed by Lumine's canonical emotions.
#
# Deliberately incomplete. Eight of Lumine's emotions have no honest counterpart
# in a vocabulary built for a different purpose, and they are absent rather than
# answered with a plausible neighbour: `wink` is a face gesture with no vocal
# meaning, and speaking `embarrassed` as `Insecure` is a claim about how she
# sounds that nobody checked. An unmapped emotion leaves the voice exactly as
# the profile configured it, which is a better outcome than a confident wrong
# answer.
#
# Several emotions do share one target. That is not a shortcut: the two
# vocabularies differ in size, and `Joking/Comedic` is how Cartesia says what
# Lumine's `amused`, `playful` and `mischievous` all mean.
EMOTION_TO_TTS: dict[str, str] = {
    "neutral": "Neutral",
    "idle": "Neutral",
    "happy": "Happy",
    "loving": "Affectionate",
    "delighted": "Elated",
    "amused": "Joking/Comedic",
    "excited": "Excited",
    "playful": "Joking/Comedic",
    "mischievous": "Joking/Comedic",
    "jealous": "Envious",
    "sleepy": "Tired",
    "sad": "Sad",
    "surprised": "Surprised",
    "confused": "Confused",
    "thinking": "Contemplative",
    "angry": "Angry",
    "curious": "Curious",
    "proud": "Proud",
    "worried": "Anxious",
    "relieved": "Content",
    "determined": "Determined",
    "calm": "Calm",
    "concerned": "Sympathetic",
}

# The scan stops here. A reply that has shown no emotion in this many characters
# is not going to, and continuing to score every chunk of a long answer costs
# time on a decision that has already been missed.
MAX_SCAN_CHARS = 600


# What each synthesizer was *built* with, kept here rather than read off it.
#
# `update_options` mutates the instance in place, and the session holds one
# instance for its whole life — so the first reply that demonstrated something
# overwrites the value the profile configured, and every later reply inherits
# whichever emotion happened to land two turns ago. The constructor's argument
# is the only honest description of "what the profile asked for", and it is
# gone from the object before anyone thinks to look for it, so it is written
# down at build time instead. Weak keys: the synthesizer outlives a reply, not
# the program.
_BASELINES: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
_UNREMEMBERED = object()


def remember_baseline(tts: Any, value: Any) -> None:
    """Record what this synthesizer was configured with before it is changed."""
    try:
        _BASELINES[tts] = value
    except TypeError:  # pragma: no cover - an object that refuses weak refs
        return


def baseline_emotion(tts: Any) -> Any:
    """The remembered configuration, or ``_UNREMEMBERED`` if there is none.

    Not remembered is not the same as ``None``: ``None`` is a configured "leave
    the voice alone", while unremembered means nothing was ever written down and
    guessing would be worse than leaving it as it is.
    """
    try:
        return _BASELINES.get(tts, _UNREMEMBERED)
    except TypeError:  # pragma: no cover - an object that refuses weak refs
        return _UNREMEMBERED


def supports_voice_controls(tts: Any) -> bool:
    """Whether this synthesizer can take a per-turn emotion at all.

    Checked against the object rather than the profile, because the profile
    says what was asked for and this is what will happen: a Cartesia stage
    configured for `sonic-2` still cannot do it.
    """
    if tts is None or not hasattr(tts, "update_options"):
        return False
    if str(getattr(tts, "provider", "")) != "Cartesia":
        return False
    return str(getattr(tts, "model", "")).startswith(SUPPORTED_MODEL_PREFIX)


def first_voice_emotion(text: str) -> str | None:
    """The first emotion the text actually demonstrates, or `None`.

    `extract_emotion_sequence` always answers with something — an empty reading
    falls back to `neutral` at intensity `0.0` rather than returning nothing —
    so "no evidence" and "evidence of neutral" have to be told apart. They are
    already distinguishable by intensity: a real hit scores at least one hint
    and lands at `0.43`, while the fallback is exactly `0.0`. Reaching for
    that rather than for a `primary == "neutral"` test keeps genuine neutral
    reachable and keeps silence distinguishable from a shrug.
    """
    if not text:
        return None
    for event in extract_emotion_sequence(text, source="llm"):
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            continue
        try:
            intensity = float(payload.get("intensity") or 0.0)
        except (TypeError, ValueError):
            continue
        if intensity <= 0.0:
            continue
        return str(payload.get("primary") or "")
    return None


def apply_emotion(tts: Any, primary: str | None) -> bool:
    """Give the synthesizer one emotion, if this synthesizer has one.

    Never raises. A voice tweak is the least important thing happening during a
    turn, and letting an exception out of it would trade an unremarkable reply
    for a dead session.
    """
    if tts is None or not primary or not supports_voice_controls(tts):
        return False
    target = EMOTION_TO_TTS.get(str(primary).strip().lower())
    if not target:
        return False
    try:
        tts.update_options(emotion=[target])
    except Exception:  # noqa: BLE001 - see the docstring
        logger.debug("[Emotion] could not apply %s to the voice", primary, exc_info=True)
        return False
    logger.debug("[Emotion] voice set to %s for %s", target, primary)
    return True


class TurnVoiceControls:
    """One reply's worth of "when do we know how she is saying this".

    Observed rather than awaited: the caller keeps streaming text straight
    through and this only *decides*. The control is therefore applied to the
    synthesizer before the chunk that revealed it is yielded, so the text that
    carried the emotion is never handed over with the option still unset. The
    only words that can be spoken under the profile's default are those from
    chunks that arrived before any evidence existed, and there is no way to
    know those without holding the reply back — which would cost latency to
    save a clause.

    Decides once. A reply has one tone; changing the voice halfway through a
    sentence would make the second half arrive under a control the first half
    was not sent with.

    The first observation also returns the synthesizer to what the profile
    configured, which is a separate job and the reason this cannot simply
    *set* an emotion when it finds one. The synthesizer is shared for the whole
    session and `update_options` writes through it, so without that first step
    the reply that demonstrated nothing would be spoken under the emotion of
    whichever reply happened to come before it — and "no evidence" is the
    common case, not the rare one.
    """

    def __init__(self, tts: Any) -> None:
        self._tts = tts if supports_voice_controls(tts) else None
        self._seen = ""
        self._opened = False
        # Ends the scan either way: no synthesizer to talk to, or one already set.
        self._done = self._tts is None

    @property
    def active(self) -> bool:
        """Whether observing could still change anything."""
        return not self._done

    def _restore(self) -> None:
        """Put the voice back to its configured setting.

        Best effort and silent. A synthesizer nothing was written down for is
        left exactly as it is — unremembered is not the same as empty, and
        clearing a real setting on a guess would be a worse mistake than
        inheriting one.
        """
        baseline = baseline_emotion(self._tts)
        if baseline is _UNREMEMBERED:
            return
        try:
            self._tts.update_options(emotion=baseline)
        except Exception:  # noqa: BLE001 - a voice tweak must not kill a turn
            logger.debug("[Emotion] could not restore the configured voice", exc_info=True)

    def observe(self, delta: str) -> bool:
        """Take one more piece of the reply. Returns True if the voice changed."""
        if self._done or not delta:
            return False
        if not self._opened:
            # First word of this reply, and the caller has not yielded it yet,
            # which is precisely the moment the previous reply's colouring has
            # to go. Doing it here rather than in ``__init__`` keeps
            # construction side-effect free and ties it to the only event that
            # makes it necessary: text actually on its way out.
            self._opened = True
            self._restore()
        self._seen += delta
        emotion = first_voice_emotion(self._seen)
        if emotion is None:
            if len(self._seen) >= MAX_SCAN_CHARS:
                self._done = True
            return False
        self._done = True
        return apply_emotion(self._tts, emotion)
