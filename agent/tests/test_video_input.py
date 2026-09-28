"""A camera frame has to reach a model that can read it, or it is not sent.

LiveKit documents the failure mode precisely, and it is the reason this file
exists:

    Enabling `video_input` with an audio-only realtime model silently ignores the
    video frames - no error is raised but the model won't process video.

Nothing raises, nothing is logged, and the session looks healthy. The user turns
a camera on, the OS lights a recording indicator, and Lumine cannot see a single
frame. That is worse than the current state, where the camera is an honest local
preview, so the subscription is derived from the catalog's own `input_modalities`
rather than decided anywhere in the factory.

Every assertion here goes through `_build_realtime_session` and reads the
`RoomOptions` the session would actually be started with. A test of a helper that
nothing calls is how the thinking-level rule came to be "tested" while the real
path still sent a level the model refused.
"""

import asyncio
import contextlib
import unittest
from unittest import mock

from agent.config_store import ResolvedStage, resolve_profile
from agent.pipeline_factory import (
    _build_cascade_session,
    _build_realtime_session,
    _room_options,
    _stage_sees_frames,
)
from agent.providers import PROVIDERS, get_model

#: Every realtime model the catalog offers. The feature is not allowed to work
#: for one of them and silently not for another.
REALTIME_MODELS = [
    (provider_id, model.id)
    for provider_id, provider in PROVIDERS.items()
    for model in provider.models
    if model.capability == "realtime" and model.is_usable()
]


def realtime_profile(model: str = "gemini-3.8-live") -> dict:
    return {
        "id": "r",
        "name": "R",
        "kind": "realtime",
        "realtime": {
            "provider": "google",
            "model": model,
            "voice": "Sulafat",
            "output": {"mode": "model_voice"},
        },
    }


def pipeline_profile(llm_model: str = "openai/gpt-oss-20b") -> dict:
    return {
        "id": "p",
        "name": "P",
        "kind": "pipeline",
        "pipeline": {
            "stt": {"provider": "groq", "model": "whisper-large-v3-turbo"},
            "llm": {"provider": "groq", "model": llm_model},
            "tts": {
                "provider": "cartesia",
                "model": "sonic-3",
                "voice": "f786b574-daa5-4673-aa0c-cbe3e8534c02",
            },
        },
    }


def subscribes_to_video(components) -> bool:
    """Whether these components would make the session receive frames."""
    assert components.room_options is not None, "session.start rejects None"
    return components.room_options.get_video_input_options() is not None


def build_realtime(profile: dict):
    resolved = resolve_profile(profile, interruption_mode="barge_in")
    with mock.patch("agent.pipeline_factory._build_realtime", new=mock.AsyncMock()):
        return asyncio.run(_build_realtime_session(resolved))


def build_cascade(profile: dict):
    """A cascade session with every provider constructor stubbed out.

    Only the room options are under test here, so the stages themselves are
    replaced wholesale. `provider_module` is patched because the builder uses it
    to decide whether the profile's plugins are installed, and a cascade built
    for tests that has to be believed about that is not worth having.
    """
    resolved = resolve_profile(profile, interruption_mode="barge_in")
    stubs = [
        mock.patch(f"agent.pipeline_factory.{name}", new=mock.AsyncMock())
        for name in ("_build_stt", "_build_llm", "_build_tts", "_build_vad")
    ]
    with contextlib.ExitStack() as stack:
        for patch in stubs + [
            mock.patch("agent.pipeline_factory.provider_module", return_value=object()),
            mock.patch("agent.pipeline_factory.connect_max_retry", return_value=2),
        ]:
            stack.enter_context(patch)
        return asyncio.run(_build_cascade_session(resolved))


class VideoModalitiesCatalogTests(unittest.TestCase):
    def test_every_usable_realtime_model_declares_image(self):
        """If one of these does not see, the control has to say so per model.

        This is the assertion that makes the feature safe to offer at all. The
        whole design rests on "the realtime family can see", and a family-wide
        claim is exactly the kind of thing that is true until one member leaves.
        """
        self.assertTrue(REALTIME_MODELS, "no usable realtime models in the catalog")
        for provider_id, model_id in REALTIME_MODELS:
            with self.subTest(model=model_id):
                model = get_model(provider_id, model_id, "realtime")
                self.assertIsNotNone(model)
                assert model is not None
                self.assertTrue(
                    model.sees,
                    f"{model_id} declares {model.input_modalities}, so it cannot "
                    "read a frame and must not be advertised as able to",
                )

    def test_the_transport_sees_nothing(self):
        """Nothing in the catalog is a room. It carries frames; it reads none.

        A model that inherited `image` from the capability default would let a
        camera be offered against a stack that structurally cannot use it.
        """
        for provider_id, provider in PROVIDERS.items():
            for model in provider.models:
                if model.capability == "transport":
                    with self.subTest(model=model.id):
                        self.assertEqual(model.input_modalities, ())

    def test_a_cascade_llm_is_text_only(self):
        model = get_model("groq", "openai/gpt-oss-20b", "llm")
        assert model is not None
        self.assertEqual(model.input_modalities, ("text",))
        self.assertFalse(model.sees)


class StageSeesFramesTests(unittest.TestCase):
    def test_it_reads_the_catalog(self):
        stage = ResolvedStage(provider="google", model="gemini-3.8-live", options={})
        self.assertTrue(_stage_sees_frames(stage, "realtime"))

    def test_an_uncatalogued_model_answers_no(self):
        """The safe direction, and the one that is easy to get wrong.

        A model added to a provider without a catalog update still builds --
        refusing would be worse than trying. So an unknown model has to be
        treated as unable to see, because subscribing anyway is the failure that
        is invisible, and not subscribing is merely conservative.
        """
        stage = ResolvedStage(provider="google", model="not-a-real-model", options={})
        self.assertFalse(_stage_sees_frames(stage, "realtime"))

    def test_a_text_only_model_answers_no(self):
        stage = ResolvedStage(provider="groq", model="openai/gpt-oss-20b", options={})
        self.assertFalse(_stage_sees_frames(stage, "llm"))


class RoomOptionsTests(unittest.TestCase):
    def test_off_is_expressible(self):
        self.assertIsNone(_room_options(False).get_video_input_options())

    def test_on_is_expressible(self):
        self.assertIsNotNone(_room_options(True).get_video_input_options())


class RealtimeVideoInputTests(unittest.TestCase):
    def test_a_seeing_model_subscribes(self):
        self.assertTrue(subscribes_to_video(build_realtime(realtime_profile())))

    def test_it_declares_a_sampler(self):
        """The frame rate is stated, not inherited.

        The library default happens to be these numbers, so nothing changes --
        but Gemini tokenizes every frame by its dimensions, and a rate nobody
        chose is a rate nobody can reason about when the bill arrives.
        """
        components = build_realtime(realtime_profile())
        sampler = components.session_kwargs["video_sampler"]
        self.assertEqual(sampler.speaking_fps, 1.0)
        self.assertEqual(sampler.silent_fps, 0.3)

    def test_every_usable_realtime_model_subscribes(self):
        for provider_id, model_id in REALTIME_MODELS:
            if provider_id != "google":
                continue
            with self.subTest(model=model_id):
                self.assertTrue(
                    subscribes_to_video(build_realtime(realtime_profile(model_id)))
                )

    def test_an_uncatalogued_realtime_model_does_not_subscribe(self):
        """The silent-failure case, refused.

        Without this the model builds, the room subscribes, the OS shows a
        recording light, and nothing consumes the frames.
        """
        components = build_realtime(realtime_profile("some-model-added-yesterday"))
        self.assertFalse(subscribes_to_video(components))
        self.assertNotIn("video_sampler", components.session_kwargs)

    def test_no_sampler_without_a_subscription(self):
        # A sampler with nothing to sample is a reader of the code that has to
        # work out which of the two settings actually governs cost.
        components = build_realtime(realtime_profile("some-model-added-yesterday"))
        self.assertNotIn("video_sampler", components.session_kwargs)


class CascadeVideoInputTests(unittest.TestCase):
    def test_the_cascade_does_not_subscribe(self):
        """Frame injection into a cascade works, and is still not this feature.

        LiveKit will put the latest frame into the chat context as an image
        message on each user turn. That genuinely works -- but the LLM stage here
        is handed a history of strings and declares `("text",)`, so turning the
        room subscription on would promise frames the stage cannot use.
        """
        components = build_cascade(pipeline_profile())
        self.assertFalse(subscribes_to_video(components))
        self.assertNotIn("video_sampler", components.session_kwargs)

    def test_the_cascade_even_with_a_vision_llm_does_not_subscribe(self):
        # A vision LLM in the cascade is a real model the catalog could grow into
        # this. Until the stage's declared modalities say image, the room must
        # not subscribe -- otherwise a profile could be half-configured into a
        # state that validates and then does nothing.
        components = build_cascade(pipeline_profile("google/gemini-3.8-flash"))
        self.assertFalse(subscribes_to_video(components))


class RoomOptionsAreAlwaysPresentTests(unittest.TestCase):
    """`session.start` rejects a `None` options object rather than defaulting.

    `AgentSession.start(room_options=None)` reaches `RoomOptions._ensure_options`,
    which treats a supplied-but-wrong type as an error and raises. "Off" therefore
    has to be an options object that says off, or the session fails to start --
    which is the mirror image of the bug these tests exist to prevent.
    """

    def test_realtime_always_carries_options(self):
        self.assertIsNotNone(build_realtime(realtime_profile()).room_options)

    def test_realtime_carries_options_even_when_off(self):
        components = build_realtime(realtime_profile("some-model-added-yesterday"))
        self.assertIsNotNone(components.room_options)

    def test_cascade_always_carries_options(self):
        self.assertIsNotNone(build_cascade(pipeline_profile()).room_options)


if __name__ == "__main__":
    unittest.main()
