"""Saying one exact line, on a session that may have no way to say it.

This is the module that decides whether an approval question is *audible*, and it
exists because of a specific, reproducible failure rather than a hypothetical one.

On the default profile -- `gemini_live`, one native-audio model -- there is no TTS
stage, so ``AgentSession.say()`` raises. LiveKit says so plainly:

    Realtime models don't offer a method to directly generate speech from a text
    script, such as with the say method. You can produce a response with
    generate_reply(instructions='...') ... but the output isn't guaranteed to
    precisely follow any provided script.
    -- https://docs.livekit.io/agents/models/realtime/

Every caller that needed a fixed sentence used to call ``say()`` inside a
``try/except`` and swallow the result. On that profile the swallow ate
*everything*: the approval question was never spoken, the user saw a card appear
with no indication that an answer was expected, and the gate counted their
silence as a denial. For a voice product that is the worst possible outcome, and
it looked like a product bug rather than a missing audio path.

So the contract under test is: **escalate until one channel carries the line, and
report which one.** Silent is a valid answer -- but it is never the first one.
"""

from __future__ import annotations

import asyncio
import unittest

from agent.runtime import voice_line
from agent.runtime.voice_line import GENERATED, SAID, SILENT, speak_line, spoken_via_label


class RecordingSession:
    """A session whose speech channels can be made to fail on demand."""

    def __init__(self, *, say_raises: Exception | None = None, generates: bool = True) -> None:
        self._say_raises = say_raises
        self._generates = generates
        self.said: list[tuple[str, dict]] = []
        self.instructions: list[str] = []

    def say(self, text: str, **kwargs) -> None:
        if self._say_raises is not None:
            raise self._say_raises
        self.said.append((text, kwargs))

    def generate_reply(self, *, instructions: str = "", **_kwargs):
        self.instructions.append(instructions)
        if not self._generates:
            raise RuntimeError("no generation either")
        return None


class BareSession:
    """A session with neither channel. The worst case has to be survivable."""

    pass


def run(coro):
    return asyncio.run(coro)


class EscalationTests(unittest.TestCase):
    def test_a_session_with_a_tts_speaks_the_line_verbatim(self):
        session = RecordingSession()
        self.assertEqual(run(speak_line(session, "Read it to you?")), SAID)
        self.assertEqual(session.said[0][0], "Read it to you?")

    def test_say_is_told_not_to_interrupt_and_not_to_log_the_question(self):
        # Both arguments are load-bearing. An approval question that can be cut
        # off is one that is sometimes never heard, and silence is a denial. A
        # question that joined the chat context would be read back by the model as
        # part of the conversation -- so the model could infer that the user had
        # approved something they were never asked.
        session = RecordingSession()
        run(speak_line(session, "Delete it for everyone?", allow_interruptions=False))
        _text, kwargs = session.said[0]
        self.assertIs(kwargs["allow_interruptions"], False)
        self.assertIs(kwargs["add_to_chat_ctx"], False)

    def test_a_realtime_profile_falls_back_to_the_model_speaking(self):
        # The case that was silently broken. `say()` raises with no TTS in the
        # pipeline, and the line still has to be audible.
        session = RecordingSession(say_raises=RuntimeError("no TTS model or RealtimeSession"))
        self.assertEqual(run(speak_line(session, "Read it to you?")), GENERATED)
        self.assertEqual(session.said, [], "say() was expected to fail")
        self.assertEqual(len(session.instructions), 1)

    def test_the_fallback_asks_for_the_exact_words(self):
        # Not guaranteed verbatim by the provider, so the request has to be as
        # unambiguous as it can be. The card stays the authoritative channel; this
        # is the attempt to make the voice agree with it.
        session = RecordingSession(say_raises=RuntimeError("no TTS"))
        run(speak_line(session, "Delete it for everyone?"))
        (instruction,) = session.instructions
        self.assertIn("word for word", instruction)
        self.assertIn("Delete it for everyone?", instruction)

    def test_the_fallback_does_not_await_the_audio_before_returning(self):
        # The gate arms its timeout the moment the question is asked. A caller
        # that blocked until the announcement finished would spend part of the
        # user's consent window on the announcement itself.
        class Slow(RecordingSession):
            def generate_reply(self, *, instructions: str = "", **_kwargs):
                self.instructions.append(instructions)
                return asyncio.sleep(30)  # a coroutine the caller must not await

        self.assertEqual(run(speak_line(Slow(say_raises=RuntimeError("x")), "hello")), GENERATED)

    def test_say_is_preferred_over_the_model(self):
        # When both channels work, the verbatim one wins: a synthesiser is not
        # paraphrasing a consent question.
        session = RecordingSession()
        run(speak_line(session, "Read it to you?"))
        self.assertEqual(session.instructions, [])

    def test_the_model_fallback_can_be_refused_by_the_caller(self):
        # The send confirmation is spoken by a tool that is about to return a
        # result, and the model answers that result on its own. Escalating there
        # would put a second sentence about one event in front of the first, so
        # that caller refuses the fallback and is told plainly that nothing
        # carried the line -- which is `SILENT`, not a lie about having spoken.
        session = RecordingSession(say_raises=RuntimeError("no TTS model"))
        channel = run(
            speak_line(
                session,
                "The message has been sent to Alice.",
                allow_model=False,
            )
        )
        self.assertEqual(channel, SILENT)
        self.assertEqual(session.instructions, [], "the model was asked to speak anyway")

    def test_a_session_with_nothing_says_it_is_silent_rather_than_raising(self):
        self.assertEqual(run(speak_line(BareSession(), "Read it to you?")), SILENT)

    def test_nothing_to_say_is_not_an_attempt(self):
        for empty in ("", "   ", None):
            with self.subTest(text=empty):
                session = RecordingSession()
                self.assertEqual(run(speak_line(session, empty)), SILENT)
                self.assertEqual(session.said, [])
                self.assertEqual(session.instructions, [])

    def test_a_channel_that_raises_something_unexpected_still_ends_silently_not_loudly(self):
        # A courtesy line must never be the thing that ends a session. Note the
        # exception is an ordinary one: a `KeyboardInterrupt` is a shutdown signal
        # and is deliberately *not* swallowed, because swallowing it would make
        # the worker unkillable at the worst possible moment.
        class Hostile:
            def say(self, *_args, **_kwargs):
                raise MemoryError("no")

            def generate_reply(self, **_kwargs):
                raise MemoryError("still no")

        self.assertEqual(run(speak_line(Hostile(), "hello")), SILENT)


class ChannelLabelTests(unittest.TestCase):
    def test_each_channel_has_its_own_word(self):
        # "She said nothing" and "she spoke and you did not hear it" need
        # different answers, and the only evidence is which channel carried it.
        self.assertEqual(spoken_via_label(SAID), "tts")
        self.assertEqual(spoken_via_label(GENERATED), "model_voice")
        self.assertEqual(spoken_via_label(SILENT), "none")

    def test_an_unknown_channel_reads_as_none(self):
        self.assertEqual(spoken_via_label("something else"), "none")
        self.assertEqual(spoken_via_label(None), "none")


if __name__ == "__main__":
    unittest.main()
