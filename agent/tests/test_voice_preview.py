"""The voice preview: what it builds, what it returns, and what it refuses.

The preview exists so a user can hear a talking-speed change. That makes it the one
settings control whose correctness is not visible on screen, which shapes these
tests: the WAV has to be playable, the stage has to come from the same builder the
worker uses, and a failure has to arrive as a sentence rather than as silence.

Nothing here contacts a provider. The synthesizer is a stand-in that yields
crafted frames, so the suite stays offline and fast.
"""

import asyncio
import json
import struct
import unittest
from types import SimpleNamespace
from unittest import mock

from agent import voice_preview
from agent.settings.config_store import ResolvedStage
from agent.pipeline.pipeline_factory import ConfigurationRejected  # noqa: F401 - import shape check


def _frame(payload: bytes, sample_rate: int = 24_000, channels: int = 1) -> SimpleNamespace:
    """A stand-in for `rtc.AudioFrame`, which the real one cannot be built cheaply."""
    return SimpleNamespace(data=payload, sample_rate=sample_rate, num_channels=channels)


class _FakeSynthesizer:
    """Yields `frames` once, then records that it was closed."""

    def __init__(self, frames):
        self._frames = frames
        self.closed = False

    def synthesize(self, text):
        self.text = text

        async def stream():
            for frame in self._frames:
                yield SimpleNamespace(frame=frame, is_final=False)

        return stream()

    async def aclose(self):
        self.closed = True


class WavHeaderTests(unittest.TestCase):
    """The header has to be right, or the browser decodes noise."""

    def test_the_header_declares_the_format_it_was_given(self):
        header = voice_preview._wav_header(24_000, 1, 480)
        self.assertTrue(header.startswith(b"RIFF"))
        self.assertIn(b"WAVE", header)
        # RIFF size counts everything after the first eight bytes.
        self.assertEqual(struct.unpack_from("<I", header, 4)[0], 36 + 480)
        # fmt chunk: PCM (1), 1 channel, 24 kHz.
        self.assertEqual(struct.unpack_from("<H", header, 20)[0], 1)
        self.assertEqual(struct.unpack_from("<H", header, 22)[0], 1)
        self.assertEqual(struct.unpack_from("<I", header, 24)[0], 24_000)
        self.assertEqual(struct.unpack_from("<I", header, 40)[0], 480)

    def test_a_stereo_header_does_not_claim_mono(self):
        # The first frame defines the format, so a provider that answers in stereo
        # must not be written out with a mono header: it plays at double speed.
        header = voice_preview._wav_header(48_000, 2, 960)
        self.assertEqual(struct.unpack_from("<H", header, 22)[0], 2)
        self.assertEqual(struct.unpack_from("<I", header, 24)[0], 48_000)
        # Byte rate is sample rate * channels * 2 bytes; 48000*2*2 = 192000.
        self.assertEqual(struct.unpack_from("<I", header, 28)[0], 192_000)


class StageTests(unittest.TestCase):
    """The fragment the screen sends is a request, and it has to be complete."""

    def test_a_provider_and_model_are_required(self):
        for payload in ({"model": "sonic-3"}, {"provider": "cartesia"}, {}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    voice_preview._build_stage(payload)

    def test_a_blank_provider_is_no_provider(self):
        # A cleared select submits "   ", and that is not a provider id.
        with self.assertRaises(ValueError):
            voice_preview._build_stage({"provider": "   ", "model": "sonic-3"})

    def test_options_must_be_an_object(self):
        # A string here would be passed to the plugin as `model_speed="1.2"`, and
        # the resulting error would name the wrong thing entirely.
        with self.assertRaises(ValueError):
            voice_preview._build_stage({"provider": "cartesia", "model": "sonic-3", "options": "1.2"})

    def test_a_blank_voice_becomes_no_voice(self):
        stage = voice_preview._build_stage({"provider": "cartesia", "model": "sonic-3", "voice": "  "})
        self.assertIsNone(stage.voice)

    def test_the_stage_carries_the_draft_settings_through(self):
        # The point of a preview is to hear an unsaved change, so the options have
        # to survive intact rather than being filtered down to a known set.
        stage = voice_preview._build_stage(
            {"provider": "cartesia", "model": "sonic-3", "options": {"speed": "1.4", "emotion": "Warm"}}
        )
        self.assertIsInstance(stage, ResolvedStage)
        self.assertEqual(stage.options["speed"], "1.4")
        self.assertEqual(stage.options["emotion"], "Warm")


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_preview_returns_playable_audio(self):
        # 6000 int16 samples per frame is a quarter of a second at 24 kHz.
        frame = b"\x00\x01" * 6000
        synthesizer = _FakeSynthesizer([_frame(frame), _frame(frame)])
        with mock.patch.object(voice_preview, "_build_tts", mock.AsyncMock(return_value=synthesizer)):
            result = await voice_preview.preview({"provider": "cartesia", "model": "sonic-3"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["mimeType"], "audio/wav")
        self.assertEqual(result["sampleRate"], 24_000)
        self.assertEqual(result["channels"], 1)
        # Half a second of 24 kHz mono 16-bit audio.
        self.assertAlmostEqual(result["durationSeconds"], 0.5, places=2)
        self.assertTrue(result["audioBase64"])

    async def test_the_synthesizer_is_always_closed(self):
        # A leaked websocket pool makes the interpreter hang on exit, which the
        # desktop layer sees as a preview that never returned.
        synthesizer = _FakeSynthesizer([_frame(b"\x00" * 480)])
        with mock.patch.object(voice_preview, "_build_tts", mock.AsyncMock(return_value=synthesizer)):
            await voice_preview.preview({"provider": "cartesia", "model": "sonic-3"})
        self.assertTrue(synthesizer.closed)

    async def test_it_is_closed_even_when_synthesis_fails(self):
        class Exploding:
            closed = False

            def synthesize(self, text):
                async def stream():
                    raise RuntimeError("provider refused")
                    yield  # pragma: no cover - unreachable, keeps this an async generator

                return stream()

            async def aclose(self):
                type(self).closed = True

        exploding = Exploding()
        with mock.patch.object(voice_preview, "_build_tts", mock.AsyncMock(return_value=exploding)):
            with self.assertRaises(RuntimeError):
                await voice_preview.preview({"provider": "cartesia", "model": "sonic-3"})
        self.assertTrue(Exploding.closed)

    async def test_an_empty_answer_is_an_error_not_a_silent_clip(self):
        # Zero bytes of audio would be a data URI the webview accepts and never
        # plays, which looks to the user like a button that does nothing.
        with mock.patch.object(
            voice_preview, "_build_tts", mock.AsyncMock(return_value=_FakeSynthesizer([]))
        ):
            with self.assertRaises(RuntimeError) as caught:
                await voice_preview.preview({"provider": "cartesia", "model": "sonic-3"})
        self.assertIn("no audio", str(caught.exception))

    async def test_empty_frames_are_skipped_rather_than_joined(self):
        # A frame with no payload is a keep-alive, not silence. Joining `None` would
        # raise and lose the whole clip over one empty packet.
        # Two frames of 6000 int16 samples: a quarter of a second each.
        synthesizer = _FakeSynthesizer(
            [_frame(b""), _frame(b"\x00" * 12_000), _frame(b"\x00" * 12_000)]
        )
        with mock.patch.object(voice_preview, "_build_tts", mock.AsyncMock(return_value=synthesizer)):
            result = await voice_preview.preview({"provider": "cartesia", "model": "sonic-3"})
        self.assertAlmostEqual(result["durationSeconds"], 0.5, places=2)

    async def test_the_clip_stops_at_the_ceiling(self):
        # A pathological rate must not make this hang while the provider keeps
        # streaming; the preview exists to judge a few seconds of voice.
        frame = _frame(b"\x00" * (24_000 * 2 * 2))  # two seconds of audio per frame
        synthesizer = _FakeSynthesizer([frame] * 20)
        with mock.patch.object(voice_preview, "_build_tts", mock.AsyncMock(return_value=synthesizer)):
            result = await voice_preview.preview({"provider": "cartesia", "model": "sonic-3"})
        self.assertLessEqual(result["durationSeconds"], voice_preview.MAX_SECONDS + 0.1)

    async def test_the_first_frame_decides_the_format(self):
        # A later frame that disagrees would mean the provider switched mid-clip,
        # and the audio would play at the wrong speed.
        synthesizer = _FakeSynthesizer(
            [_frame(b"\x00" * 480, 24_000, 1), _frame(b"\x00" * 480, 44_100, 2)]
        )
        with mock.patch.object(voice_preview, "_build_tts", mock.AsyncMock(return_value=synthesizer)):
            result = await voice_preview.preview({"provider": "cartesia", "model": "sonic-3"})
        self.assertEqual(result["sampleRate"], 24_000)
        self.assertEqual(result["channels"], 1)

    async def test_a_phrase_of_the_callers_own_is_spoken_verbatim(self):
        # "How do you pronounce Lumine" is a question the fixed sample cannot answer.
        synthesizer = _FakeSynthesizer([_frame(b"\x00" * 480)])
        with mock.patch.object(voice_preview, "_build_tts", mock.AsyncMock(return_value=synthesizer)):
            await voice_preview.preview(
                {"provider": "cartesia", "model": "sonic-3", "phrase": "Kumquat."}
            )
        self.assertEqual(synthesizer.text, "Kumquat.")

    async def test_a_blank_phrase_falls_back_to_the_built_in_line(self):
        synthesizer = _FakeSynthesizer([_frame(b"\x00" * 480)])
        with mock.patch.object(voice_preview, "_build_tts", mock.AsyncMock(return_value=synthesizer)):
            await voice_preview.preview({"provider": "cartesia", "model": "sonic-3", "phrase": "   "})
        self.assertEqual(synthesizer.text, voice_preview.DEFAULT_PHRASE)

    async def test_a_payload_too_large_to_send_is_refused_outright(self):
        # Failing loudly beats truncating audio into something that sounds like a
        # dropout, and beats a silent IPC failure in the webview.
        with mock.patch.object(voice_preview, "MAX_BASE64_CHARS", 64):
            synthesizer = _FakeSynthesizer([_frame(b"\x00" * 4096)])
            with mock.patch.object(voice_preview, "_build_tts", mock.AsyncMock(return_value=synthesizer)):
                with self.assertRaises(RuntimeError) as caught:
                    await voice_preview.preview({"provider": "cartesia", "model": "sonic-3"})
        self.assertIn("too long", str(caught.exception))

    async def test_the_stage_is_built_by_the_worker_factory(self):
        # A preview built any other way would be auditioning a voice the session
        # does not have, which is worse than no preview at all.
        with mock.patch.object(
            voice_preview, "_build_tts", mock.AsyncMock(return_value=_FakeSynthesizer([_frame(b"\x00" * 480)]))
        ) as builder:
            await voice_preview.preview(
                {"provider": "cartesia", "model": "sonic-3", "voice": "abc", "options": {"speed": "1.1"}}
            )
        stage = builder.await_args.args[0]
        self.assertEqual((stage.provider, stage.model, stage.voice), ("cartesia", "sonic-3", "abc"))
        self.assertEqual(stage.options, {"speed": "1.1"})


class EnvelopeTests(unittest.TestCase):
    """The one line the desktop layer reads."""

    def test_a_success_reports_ok_and_no_error(self):
        import io

        buffer = io.StringIO()
        with mock.patch.object(voice_preview.sys, "stdout", buffer):
            self.assertEqual(voice_preview._emit({"ok": True, "audioBase64": "AA"}), 0)
        payload = json.loads(buffer.getvalue().strip())
        self.assertTrue(payload["ok"])
        self.assertNotIn("error", payload)

    def test_a_failure_exits_non_zero_so_a_caller_can_notice(self):
        import io

        buffer = io.StringIO()
        with mock.patch.object(voice_preview.sys, "stdout", buffer):
            self.assertEqual(voice_preview._emit({"ok": False, "error": "no key"}), 1)
        self.assertEqual(json.loads(buffer.getvalue().strip())["error"], "no key")

    def test_malformed_input_is_reported_as_json_not_a_traceback(self):
        # The desktop layer parses stdout. A traceback there is an unreadable
        # result rather than a message the user can act on.
        import io

        buffer = io.StringIO()
        with mock.patch.object(voice_preview.sys, "stdout", buffer), mock.patch.object(
            voice_preview.sys, "stdin", io.StringIO("not json")
        ):
            code = voice_preview.main([])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buffer.getvalue().strip())["ok"])

    def test_a_json_array_is_refused_rather_than_indexed(self):
        import io

        buffer = io.StringIO()
        with mock.patch.object(voice_preview.sys, "stdout", buffer), mock.patch.object(
            voice_preview.sys, "stdin", io.StringIO("[1, 2, 3]")
        ):
            code = voice_preview.main([])
        self.assertEqual(code, 1)
        self.assertIn("object", json.loads(buffer.getvalue().strip())["error"])


class CeilingTests(unittest.TestCase):
    """The limits are the ones the payload budget actually needs."""

    def test_the_clip_ceiling_is_a_few_seconds_not_a_paragraph(self):
        self.assertLessEqual(voice_preview.MAX_SECONDS, 10.0)
        self.assertGreaterEqual(voice_preview.MAX_SECONDS, 2.0)

    def test_the_payload_ceiling_fits_the_longest_allowed_clip(self):
        # Worst case: the ceiling in seconds, at a generous sample rate, base64'd.
        # If this failed, the cap would be refusing clips it is supposed to allow.
        worst = int(voice_preview.MAX_SECONDS * 48_000) * 2 * 2  # 48 kHz stereo, 16-bit
        import base64

        self.assertLess(len(base64.b64encode(b"\x00" * worst)), voice_preview.MAX_BASE64_CHARS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
