"""Saying one exact line, on either kind of voice session.

There are two architectures in this product and they cannot both be served by
``session.say``:

* the **cascade** profile -- STT, an LLM, and a TTS stage. ``say()`` synthesises
  the text and plays it, so Lumine can say *exactly* the sentence given to her.
* the **realtime** profile (``gemini_live``, the default) -- one native-audio
  model. There is no TTS stage, and LiveKit is explicit about the consequence:

  > Realtime models don't offer a method to directly generate speech from a text
  > script, such as with the say method. You can produce a response with
  > generate_reply(instructions='...') and include specific instructions but the
  > output isn't guaranteed to precisely follow any provided script.
  > -- https://docs.livekit.io/agents/models/realtime/

  ``say()`` raises ``RuntimeError`` there. The supported way to get *both*
  realtime comprehension and exact scripted speech is the half-cascade -- realtime
  model with ``modalities=["text"]`` plus a TTS stage -- which is a profile
  decision rather than something to smuggle in at a call site.

## Why this module exists

Every caller that needed a fixed sentence used to call ``say()`` inside a bare
``try/except`` and swallow the failure. That is a *silent* failure, and for a
voice product it is the worst kind:

* the approval question was never spoken, so the user saw a card appear, had no
  idea an answer was expected, and sat through the full timeout in silence --
  and silence is a **denial**;
* the "sorry, I'm having trouble" apology after a failed turn was never spoken
  either, which is the one moment the user most needs to be told something.

So the rule is now stated once, here: **a line that must be heard is escalated
until it is audible, and the caller is told which channel carried it.** The
fallback is not verbatim -- LiveKit does not offer verbatim on a realtime model --
which is precisely why the approval *card* keeps carrying the exact sentence.

## Why escalating is safe for a consent question

The gate's rule is that the model must never be able to paraphrase a consent
question, because it never gets to write one. The fallback does hand the words to
the model, so the guarantee has to be stated honestly:

* the text goes in as ``generate_reply(instructions=...)``, which the SDK applies
  as a **system-level prompt** -- it does not join the chat context, so the model
  cannot later read its own approval request back as something the user said;
* the card is published with the identical sentence and is the authoritative
  channel, so a reworded utterance never becomes the record of what was agreed;
* both channels resolve the same request id, so answering either one runs the
  action exactly once.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("lumine")

#: ``say()`` played the text. Verbatim, and the only fully trustworthy channel.
SAID = "said"

#: The realtime model was asked to say it. Audible; wording is not guaranteed.
GENERATED = "generated"

#: No audio channel at all. The caller owes the user something else.
SILENT = "silent"

#: How the fallback asks. Kept short and imperative because it is prepended to
#: whatever has to be said, and a long preamble on a consent question is noise.
_VERBATIM_PREFIX = "Say this out loud, word for word, and say nothing else: "


def _swallow(finished: asyncio.Future) -> None:
    """Retrieve a finished task's exception, cancellation included.

    A done-callback is the only way to stop the loop logging "Task exception was
    never retrieved", and it is also a place where a naive ``.exception()`` call
    raises: for a *cancelled* task that call re-raises ``CancelledError`` inside
    the callback, where nothing is waiting for it. Shutdown cancels the
    announcement task routinely -- the session ends while audio is still
    scheduled -- so the guard is the normal case, not an edge one.
    """
    if not finished.cancelled():
        finished.exception()


def _speech_handle() -> Any:
    """The legacy ``SpeechHandle`` class, if this SDK still ships one."""
    try:
        from livekit.agents.voice import speech_handle

        return speech_handle.SpeechHandle
    except Exception:  # noqa: BLE001 - absence is the normal case on a pinned SDK
        return None


async def _try_say(session: Any, text: str, *, allow_interruptions: bool) -> bool:
    """``say()``, including the older handle-taking signature."""
    say = getattr(session, "say", None)
    if not callable(say):
        return False
    try:
        # `add_to_chat_ctx=False` is load-bearing for a consent question: if it
        # joined the transcript, the model would read the gate's own request back
        # as part of the conversation and could treat it as the user's answer.
        say(text, allow_interruptions=allow_interruptions, add_to_chat_ctx=False)
        return True
    except TypeError:
        # An older `say` that takes a handle rather than text.
        handle_cls = _speech_handle()
        if handle_cls is None:
            return False
        try:
            say(handle_cls(text, allow_interruptions=allow_interruptions))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Voice] legacy say(handle) failed: %s: %s", type(exc).__name__, exc)
            return False
    except Exception as exc:  # noqa: BLE001 - the documented realtime case lands here
        logger.debug("[Voice] say() unavailable (%s: %s)", type(exc).__name__, exc)
        return False


async def _try_generate(session: Any, text: str) -> bool:
    """Ask the realtime model to say it. Audible, not verbatim.

    The call itself is made **synchronously** and its result awaited later, in a
    task. That ordering is the point: a channel that raises on the call has not
    carried the line, and reporting otherwise would tell the app "she spoke" for
    a session that said nothing -- which is the one distinction this whole module
    exists to keep honest. Only once the call is accepted does the audio become
    somebody else's problem, and by then waiting for it would be wrong anyway: the
    gate arms its consent deadline the moment the question is asked, and blocking
    until the announcement finished would spend part of the user's window on the
    announcement.
    """
    generate = getattr(session, "generate_reply", None)
    if not callable(generate):
        return False
    try:
        result = generate(instructions=_VERBATIM_PREFIX + text)
    except asyncio.CancelledError:  # pragma: no cover - shutdown
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Voice] generate_reply refused the announcement: %s: %s", type(exc).__name__, exc)
        return False

    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):

        async def drain() -> None:
            try:
                await result
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except Exception as exc:  # noqa: BLE001
                # The line was accepted and then failed mid-generation. Nothing
                # can be done about it from here, and raising would take the gate
                # down over a courtesy utterance.
                logger.debug("[Voice] the announcement failed mid-generation: %s: %s", type(exc).__name__, exc)

        try:
            task = asyncio.ensure_future(drain())
            # Nobody awaits this, so its exception is retrieved here rather than
            # being reported by the loop at shutdown -- which is the difference
            # between a courtesy utterance and a traceback in someone's log.
            # `cancelled()` is checked first on purpose: calling `.exception()` on
            # a cancelled task *raises* CancelledError, inside the callback, where
            # nothing catches it. Shutdown cancels this task routinely.
            task.add_done_callback(_swallow)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Voice] could not schedule the announcement: %s", exc)
    return True


async def speak_line(
    session: Any,
    text: str,
    *,
    allow_interruptions: bool = True,
) -> str:
    """Say `text` out loud, escalating until one channel works.

    Returns :data:`SAID`, :data:`GENERATED` or :data:`SILENT` so the caller can
    report *how* the user was reached, and can tell the difference between "she
    spoke and you did not hear it" and "she said nothing at all".

    Never raises. A courtesy line that fails must not end a session, and the
    only honest thing to do about a failure is to try the next channel.
    """
    line = str(text or "").strip()
    if not line or session is None:
        return SILENT
    try:
        if await _try_say(session, line, allow_interruptions=allow_interruptions):
            return SAID
        if await _try_generate(session, line):
            return GENERATED
    except asyncio.CancelledError:  # pragma: no cover - shutdown
        raise
    except Exception as exc:  # noqa: BLE001 - never let a courtesy line break a turn
        logger.debug("[Voice] could not speak a line: %s: %s", type(exc).__name__, exc)
    return SILENT


def spoken_via_label(channel: str) -> str:
    """A short word for diagnostics and for the request record.

    Published rather than kept in a log line because "she said nothing" and "she
    spoke and you did not hear it" need different answers, and the only evidence
    is which channel carried the question.
    """
    return {SAID: "tts", GENERATED: "model_voice", SILENT: "none"}.get(channel, "none")
