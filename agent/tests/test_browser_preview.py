"""A screenshot has two destinations, and they cost very different amounts.

The preview reaches the user and is paid for once. The model's copy joins the chat
context and is re-billed on every later turn for the rest of the session. Getting
that backwards is expensive in one direction and dishonest in the other, so both
budgets and both delivery paths are pinned here.

The load-bearing fact underneath all of it: **a LiveKit function tool cannot return
an image.** ``llm/utils.py``'s ``_is_valid_function_output`` accepts only
``str | int | float | bool | complex | None`` and nested containers, and anything
else is replaced with "the tool returned an invalid output". So ``for_model`` has
to insert into the chat context rather than return a picture, and this file exists
to make sure nobody "simplifies" it back into a return value.
"""

from __future__ import annotations

import asyncio
import base64
import unittest
from types import SimpleNamespace

from agent.tools.browser import preview, state


class FakeSession:
    """A session the weak-keyed registries can actually hold.

    `SimpleNamespace` cannot be weakly referenced, so registering one is silently
    dropped by `progress.register_publisher` and every publish test would fail for
    the wrong reason. A real `AgentSession` is a normal class, and so is this.
    """

    def __init__(self, name: str = "fake") -> None:
        self.name = name


def jpeg(size: int = 32) -> bytes:
    """Bytes that are not a real JPEG.

    Nothing here decodes them: the tests are about budgets and routing, and a real
    encode per test would make the suite slower for no extra claim.
    """
    return b"\xff\xd8\xff\xe0" + b"\x00" * size


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def emit_and_publish(self, topic: str, record: dict) -> dict:
        self.published.append((topic, record))
        return record


class HostilePublisher:
    """A publisher that raises, which is what a dead room looks like."""

    def emit_and_publish(self, topic: str, record: dict) -> dict:
        raise OSError("data channel is gone")


def register(fake_session, pub) -> None:
    from agent.runtime import progress

    progress.register_publisher(fake_session, pub)


class EncodingTests(unittest.TestCase):
    def test_a_jpeg_becomes_a_rendable_data_url(self):
        encoded = preview.encode_jpeg(jpeg())
        self.assertTrue(encoded.startswith("data:image/jpeg;base64,"))
        # The app renders this on an <img src>, so the bytes have to survive the
        # round trip rather than just look like base64.
        raw = base64.b64decode(encoded.split(",", 1)[1])
        self.assertEqual(raw, jpeg())

    def test_nothing_becomes_nothing_rather_than_an_empty_data_url(self):
        # A `data:` URL with no payload is a decode error in the renderer, which
        # would look like "the worker sent a broken screenshot" rather than
        # "there was nothing to send".
        for empty in (b"", None):
            with self.subTest(value=empty):
                self.assertEqual(preview.encode_jpeg(empty), "")


class RecordTests(unittest.TestCase):
    def test_the_record_carries_its_age_and_its_source(self):
        record = preview.preview_record(
            jpeg(), url="https://example.com/x", title="Example", age_ms=42.0
        )
        self.assertEqual(record["type"], "browser_preview")
        self.assertEqual(record["url"], "https://example.com/x")
        self.assertEqual(record["age_ms"], 42.0)
        self.assertIn("image", record)
        self.assertGreater(record["captured_at"], 0)

    def test_a_picture_with_no_timestamp_is_marked_rather_than_assumed_fresh(self):
        # This is the `visual_state` rule applied to a screenshot: a preview with
        # no age reads as live, and Lumine is holding a photograph.
        record = preview.preview_record(jpeg())
        self.assertNotIn("age_ms", record)

    def test_the_metadata_is_clipped(self):
        record = preview.preview_record(jpeg(), url="u" * 400, title="t" * 400)
        self.assertLessEqual(len(record["url"]), 200)
        self.assertLessEqual(len(record["title"]), 90)

    def test_an_empty_capture_is_still_a_record_that_says_so(self):
        # Better a preview the UI can explain than a record that silently has no
        # `image` and renders as a broken tile.
        record = preview.preview_record(b"")
        self.assertNotIn("image", record)
        self.assertEqual(record["bytes"], 0)
        self.assertIn("no picture", record["imageOmitted"])


class BudgetTests(unittest.TestCase):
    """The preview and the model's copy are capped very differently, on purpose."""

    def test_the_preview_is_bounded_and_smaller_than_the_capture_cap(self):
        # 900 KB against a 4 MB capture: the preview crosses a data channel and
        # then lands in the DOM, so the decode and the paint are both on the
        # critical path of a voice turn.
        self.assertLess(state.MAX_PREVIEW_BYTES, state.MAX_SCREENSHOT_BYTES)
        self.assertLessEqual(state.MAX_PREVIEW_BYTES, state.MAX_SCREENSHOT_BYTES // 2)

    def test_the_model_copy_is_bounded_and_far_smaller_than_the_preview(self):
        # The load-bearing asymmetry. A tool result is re-sent on every later
        # turn, so an image handed to the model is paid for again and again,
        # while the preview is paid for once.
        self.assertLess(state.MAX_MODEL_IMAGE_BYTES, state.MAX_PREVIEW_BYTES)
        self.assertLess(state.MODEL_IMAGE_MAX_EDGE, state.PREVIEW_MAX_EDGE)

    def test_an_oversized_picture_is_dropped_rather_than_shipped_and_flagged(self):
        # A flag on a payload that was sent anyway is the worst outcome: it claims
        # a bound nothing enforced while the cost still lands on the channel and
        # the renderer. So the bytes must be absent, not merely labelled.
        oversized = b"\x00" * preview.MAX_PREVIEW_CHARS
        record = preview.preview_record(oversized)
        self.assertNotIn("image", record)
        self.assertIn("too large", record["imageOmitted"])
        # The metadata is the part that is still worth sending.
        self.assertEqual(record["type"], "browser_preview")

    def test_an_ordinary_preview_carries_its_picture(self):
        record = preview.preview_record(jpeg(64))
        self.assertIn("image", record)
        self.assertNotIn("imageOmitted", record)

    def test_a_preview_stays_under_the_cap_it_declares(self):
        record = preview.preview_record(jpeg(900 * 1024))
        if "image" in record:
            self.assertLessEqual(len(record["image"]), preview.MAX_PREVIEW_CHARS)


class PublishTests(unittest.TestCase):
    def setUp(self):
        from agent.runtime import progress

        self.progress = progress
        self.session = FakeSession()
        self.addCleanup(progress.unregister_publisher, self.session)

    def test_a_preview_goes_out_on_its_own_topic(self):
        # Not `lumine.tool`: that is the transient toast/status contract, and a
        # few hundred KB of base64 in it would put a screenshot in the same queue
        # as "opening WhatsApp".
        pub = FakePublisher()
        register(self.session, pub)
        self.assertTrue(
            preview.publish_preview(self.session, preview.preview_record(jpeg()))
        )
        self.assertEqual([topic for topic, _ in pub.published], [preview.TOPIC])
        self.assertNotEqual(preview.TOPIC, "lumine.tool")

    def test_a_run_context_is_accepted_as_well_as_a_session(self):
        # Every other registry in this codebase resolves both spellings, and a
        # lookup that only worked for one would be a preview that silently
        # stopped appearing at exactly one call site.
        pub = FakePublisher()
        register(self.session, pub)
        context = SimpleNamespace(session=self.session)
        self.assertTrue(preview.publish_preview(context, preview.preview_record(jpeg())))

    def test_a_dead_publisher_is_swallowed(self):
        # A screenshot that cannot be delivered is still a screenshot that was
        # taken. Raising here would turn a lost preview into a lost search.
        register(self.session, HostilePublisher())
        self.assertFalse(preview.publish_preview(self.session, preview.preview_record(jpeg())))

    def test_no_publisher_at_all_is_reported_as_not_sent(self):
        # A unit test, a script, or a worker that never joined a room. The tool
        # must still finish, and must say the preview did not arrive.
        self.assertFalse(preview.publish_preview(SimpleNamespace(), preview.preview_record(jpeg())))


class ShowToModelTests(unittest.TestCase):
    """`for_model=true`, and why it inserts rather than returns."""

    added: list = []

    class RecordingSession:
        """A chat context that records what it was asked to hold.

        Reads the list off the *outer* class rather than declaring one of its own:
        a nested class body is a separate namespace, so a list declared here would
        be a different list from the one the test asserts on.
        """

        def __init__(self) -> None:
            self.chat_ctx = self

        def add_message(self, *, role, content):
            ShowToModelTests.added.append((role, content))

    def setUp(self):
        ShowToModelTests.added = []
        self.session = self.RecordingSession()

    def test_the_picture_is_inserted_into_the_chat_context(self):
        said = preview.show_to_model(
            SimpleNamespace(session=self.session), jpeg(), caption="Example"
        )
        role, content = ShowToModelTests.added[0]
        self.assertEqual(role, "user")
        self.assertIn("I looked at the page.", said)
        # The caption is what makes the picture evidence rather than an orphan:
        # without it the model has an image and no idea which page it belongs to.
        self.assertTrue(any("Example" in str(part) for part in content[:-1]))
        self.assertTrue(content[-1].image.startswith("data:image/jpeg;base64,"))

    def test_the_result_is_a_sentence_because_a_tool_cannot_return_an_image(self):
        said = preview.show_to_model(SimpleNamespace(session=self.session), jpeg())
        # Anything that is not a str/int/float/bool/None is replaced by LiveKit
        # with "the tool returned an invalid output", so this must be a sentence.
        self.assertIsInstance(said, str)
        self.assertTrue(said.strip())

    def test_a_session_with_no_mutable_context_says_so_instead_of_raising(self):
        class ReadOnly:
            @property
            def chat_ctx(self):
                return SimpleNamespace(items=[])

        said = preview.show_to_model(SimpleNamespace(session=ReadOnly()), jpeg())
        self.assertIn("read the page instead", said)

    def test_no_picture_is_reported_rather_than_shown_as_an_empty_image(self):
        said = preview.show_to_model(SimpleNamespace(session=self.session), b"")
        self.assertIn("no picture", said)

    def test_a_broken_image_content_does_not_kill_the_tool(self):
        class Hostile:
            @property
            def chat_ctx(self):
                return self

            def add_message(self, **kwargs):
                raise RuntimeError("the model rejected this")

        said = preview.show_to_model(SimpleNamespace(session=Hostile()), jpeg())
        self.assertIn("couldn't look", said)


class FormPayloadTests(unittest.TestCase):
    """`state` reports what a form holds, and the payload has to stay readable.

    The failure this guards against is specific: `tool_results.as_payload` enforces
    its ceiling by *cutting* the rendered JSON, so an over-budget payload arrives
    as `{"forms":[{"name":"address_line_one_address_line_...` -- which is not a
    shorter answer, it is no answer, for the same tokens. The rows are dropped until
    the whole thing fits instead.
    """

    AROUND = {
        "title": "T" * 70,
        "url": "https://example.com/" * 8,
        "generation": 3,
        "controls": 400,
        "ready": "complete",
        "atBottom": False,
        "moreFields": 99999,
    }

    def _renders(self, rows) -> tuple[dict, str]:
        import json as _json

        from agent.tools import tool_results

        kept = state.form_values_payload(rows, around=self.AROUND)
        payload = {k: v for k, v in self.AROUND.items() if k != "moreFields"}
        payload["forms"] = kept
        if len(kept) < len(rows):
            payload["moreFields"] = len(rows) - len(kept)
        return kept, tool_results.as_payload(payload)

    def test_a_long_checkout_form_arrives_as_parseable_json(self):
        import json as _json

        rows = [{"name": "field_" + str(i) * 3, "type": "text", "value": "x" * 200} for i in range(40)]
        kept, body = self._renders(rows)
        self.assertLess(len(kept), len(rows), "the payload must have given something up")
        # The property that matters: the model can read it.
        self.assertIsInstance(_json.loads(body), dict)

    def test_many_short_fields_stay_under_the_cap(self):
        from agent.tools import tool_results

        rows = [{"name": f"f{i}", "type": "text", "value": "v"} for i in range(40)]
        _kept, body = self._renders(rows)
        self.assertLessEqual(len(body), tool_results.MAX_RESULT_CHARS)

    def test_a_password_field_is_redacted_even_if_the_page_forgot_to_flag_it(self):
        # `_JS_FORM` redacts in the page, which is the right place for it. This is
        # the second line of defence: a missed flag must not be the only thing
        # standing between somebody's password and the model's context.
        rows = [{"name": "pw", "type": "password", "value": "hunter2"}]
        kept, _body = self._renders(rows)
        self.assertTrue(kept[0]["redacted"])
        self.assertEqual(kept[0]["value"], "")

    def test_a_password_field_is_kept_rather_than_filtered_out(self):
        # Dropping it would read as "this page has no such field", which is the
        # one conclusion the model must not draw.
        rows = [{"name": "email", "type": "text", "value": "a@b.c"},
                {"name": "pw", "type": "password", "value": "hunter2"}]
        kept, _body = self._renders(rows)
        self.assertEqual([r["name"] for r in kept], ["email", "pw"])

    def test_a_password_survives_the_row_dropping(self):
        # Dropped from the end, so a password at the end of a long form is the one
        # that gets dropped -- and that is acceptable, because the remaining rows
        # are still whole and the field count is reported. What must never happen is
        # a *value* surviving unredacted.
        rows = [{"name": "pw", "type": "password", "value": "hunter2"}] + [
            {"name": "f" + str(i) * 4, "type": "text", "value": "y" * 200} for i in range(30)
        ]
        kept, _body = self._renders(rows)
        for row in kept:
            self.assertNotEqual(row.get("value"), "hunter2")

    def test_an_empty_form_reports_nothing_rather_than_failing(self):
        kept, _body = self._renders([])
        self.assertEqual(kept, [])

    def test_one_field_is_always_kept(self):
        # A page with a single field must be able to report it. The fitting pass
        # never drops below one row, so a caller that reserved too much room for
        # its own keys gets a short answer rather than none.
        rows = [{"name": "q", "type": "text", "value": "hi"}]
        kept, _body = self._renders(rows)
        self.assertEqual(len(kept), 1)


class ViewportClipTests(unittest.TestCase):
    """The downscaling math, which is pure and so is tested without a browser."""

    def _page(self, width: int, height: int):
        return SimpleNamespace(viewport_size={"width": width, "height": height})

    def test_a_wide_viewport_is_clipped_to_the_budget_keeping_its_ratio(self):
        from agent.tools.browser.engine import _viewport_clip

        clip = _viewport_clip(self._page(1920, 1080), 900)
        self.assertEqual(clip["width"], 900)
        # 1080 * (900/1920) = 506.25, and the aspect ratio is the thing that
        # matters: a screenshot with distorted proportions is worse than a small
        # one, because it misrepresents the page it is evidence of.
        self.assertEqual(clip["height"], 506)
        self.assertAlmostEqual(clip["width"] / clip["height"], 1920 / 1080, places=2)

    def test_a_viewport_inside_the_budget_is_captured_whole(self):
        from agent.tools.browser.engine import _viewport_clip

        # Never enlarged: blowing a small page up produces a blurrier, larger
        # image and no more information.
        self.assertIsNone(_viewport_clip(self._page(800, 600), 900))

    def test_an_unknown_viewport_is_not_a_reason_to_fail(self):
        from agent.tools.browser.engine import _viewport_clip

        self.assertIsNone(_viewport_clip(SimpleNamespace(viewport_size=None), 900))
        self.assertIsNone(_viewport_clip(self._page(0, 0), 900))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()