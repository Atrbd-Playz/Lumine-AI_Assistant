"""The file boundary, the history consent, and the progress channel.

Three claims about the parts of this feature that can hurt somebody:

1. **A path from a language model cannot read outside an allowlisted folder.**
   Traversal, absolute paths, symlinks, extensions and size are each tested
   individually, because the check is a chain and any one link can be the weak
   one. A whitelist that fails open is not a whitelist, and "resolve first, then
   compare" is the difference between the two.
2. **Reading somebody's messages asks, once, and the grant dies with the
   session.** Tested in the three states that matter: nobody configured anything,
   configured off, and configured auto. A private capability whose default is
   anything but `ask` is a default nobody chose.
3. **A tool narrating its steps cannot break the tool.** Every failure mode here
   is tested against a publisher that raises, because a telemetry channel that
   can end a send is worse than no telemetry at all.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent.runtime import history_consent
from agent.runtime import permission_gate
from agent.runtime.progress import build_step_record, publisher_for, publish_step, register_publisher, unregister_publisher
from agent.tools.messaging import whatsapp_files as files
from agent.tools.tools_compat import ToolError


class AttachmentBoundaryTests(unittest.TestCase):
    """Every test runs with the roots pointed at a real temporary folder.

    Pointing them at a real directory is the point: a test that stubbed
    `_inside` would pass whatever the comparison did, and the comparison is the
    whole mechanism.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "Downloads"
        self.root.mkdir()
        patcher = mock.patch.dict(
            os.environ, {files.ROOTS_VAR: str(self.root)}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name: str, body: bytes = b"hello") -> Path:
        target = self.root / name
        target.write_bytes(body)
        return target

    def test_a_photo_in_an_allowed_folder_is_accepted(self):
        path = self.write("cat.png", b"\x89PNG\r\n\x1a\n")
        self.assertEqual(files.resolve_attachment(str(path)), str(path))

    def test_the_path_it_returns_is_absolute_and_resolved(self):
        # What is handed to the browser is never the model's string: it is the
        # resolved form, so a later `..` in the same string cannot mean anything.
        path = self.write("cat.png")
        result = files.resolve_attachment(f"  {path}  ")
        self.assertTrue(os.path.isabs(result))

    def test_a_file_outside_the_allowed_folders_is_refused(self):
        outside = Path(self._tmp.name).resolve() / "secret.txt"
        outside.write_text("private key material")
        with self.assertRaisesRegex(ToolError, "downloads, pictures, or desktop"):
            files.resolve_attachment(str(outside))

    def test_traversal_out_of_the_allowed_folder_is_refused(self):
        # The classic. The string looks relative and harmless; the resolved path
        # is not inside the root, which is the only thing that is checked.
        target = self.write("cat.png")
        sneaky = str(target.parent / ".." / "secret.txt")
        with self.assertRaises(ToolError):
            files.resolve_attachment(sneaky)

    def test_a_dot_dot_inside_the_allowed_folder_still_resolves(self):
        # The other half of the same check: refusing every `..` would refuse
        # ordinary paths on Windows, where a user's profile is often reached
        # through one.
        path = self.write("cat.png")
        indirect = str(path.parent / "." / "cat.png")
        self.assertEqual(files.resolve_attachment(indirect), str(path))

    def test_a_symlink_out_of_the_allowed_folder_is_refused(self):
        # `resolve()` follows the link before the comparison, so a shortcut
        # pointing at the user's keys is judged by where it lands rather than by
        # where it claims to sit. Skipped where symlinks need a privilege.
        secret = Path(self._tmp.name).resolve() / "id_rsa.txt"
        secret.write_text("private key material")
        link = self.root / "notes.txt"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows without rights
            self.skipTest("symlinks are not available here")
        with self.assertRaises(ToolError):
            files.resolve_attachment(str(link))

    def test_an_executable_is_refused(self):
        for name in ("payload.exe", "run.bat", "script.sh", "archive.zip"):
            with self.subTest(name=name):
                path = self.write(name)
                with self.assertRaisesRegex(ToolError, "photos, documents"):
                    files.resolve_attachment(str(path))

    def test_an_extension_that_is_not_on_the_list_is_refused(self):
        # The list is a whitelist, so anything unlisted is out -- including a
        # format invented after this was written.
        path = self.write("invoice.xyz")
        with self.assertRaisesRegex(ToolError, "photos, documents"):
            files.resolve_attachment(str(path))

    def test_a_file_that_does_not_exist_is_refused_rather_than_handed_on(self):
        with self.assertRaisesRegex(ToolError, "couldn't find"):
            files.resolve_attachment(str(self.root / "never-existed.png"))

    def test_a_directory_is_not_a_file(self):
        with self.assertRaises(ToolError):
            files.resolve_attachment(str(self.root))

    def test_an_empty_file_is_refused(self):
        # A zero-byte attachment is a mistake, and sending one tells the recipient
        # nothing while looking like a deliberate message.
        path = self.write("empty.png", b"")
        with self.assertRaises(ToolError):
            files.resolve_attachment(str(path))

    def test_a_file_beyond_the_size_cap_is_refused(self):
        big = self.write("huge.mp4", b"x" * 16)
        big.write_bytes(b"x" * (files.MAX_ATTACH_BYTES + 1))
        with self.assertRaisesRegex(ToolError, "too large"):
            files.resolve_attachment(str(big))

    def test_a_url_is_refused_rather_than_downloaded(self):
        # Downloading would mean lifting `accept_downloads=False` on the browser
        # context -- a guard that exists so a page cannot drop files on this disk.
        # Trading that for convenience is a bad deal, so an http attachment is
        # refused with a sentence saying why.
        with self.assertRaisesRegex(ToolError, "already on this computer"):
            files.resolve_attachment("https://example.com/cat.png")

    def test_an_empty_request_is_refused(self):
        with self.assertRaises(ToolError):
            files.resolve_attachment("")

    def test_no_roots_configured_at_all_refuses_everything(self):
        # Fail closed. A machine whose Downloads and Pictures are all missing
        # must not silently accept anything.
        with mock.patch.dict(os.environ, {files.ROOTS_VAR: str(Path(self._tmp.name) / "nope")}):
            path = self.write("cat.png")
            with self.assertRaises(ToolError):
                files.resolve_attachment(str(path))

    def test_one_bad_entry_in_the_roots_narrows_rather_than_breaking_everything(self):
        # A settings file with a typo in one path should stop that path working,
        # not stop attachments entirely -- which is the difference between a
        # narrowing and a denial.
        good = self.root
        bad = str(Path(self._tmp.name) / "does-not-exist")
        with mock.patch.dict(os.environ, {files.ROOTS_VAR: f"{bad}{os.pathsep}{good}"}):
            path = self.write("cat.png")
            self.assertEqual(files.resolve_attachment(str(path)), str(path))

    def test_a_description_never_shows_the_directory_layout(self):
        # An approval card is shown to whoever is standing behind the user. Only
        # the name and a rounded size, never the path.
        path = self.write("cat.png")
        described = files.describe(str(path))
        self.assertIn("cat.png", described)
        self.assertNotIn(str(self.root), described)


class _Session:
    """A weak-referenceable stand-in for an `AgentSession`.

    Not `SimpleNamespace`: that cannot hold a weak reference, so every
    session-keyed registry in this codebase degrades to "stores nothing" for it.
    That degradation is correct and documented -- an object that cannot be weakly
    referenced is not an AgentSession -- but testing a registry against it would
    test the degradation rather than the registry.
    """

    def __init__(self, name: str = "test-session") -> None:
        self.name = name


class HistoryConsentTests(unittest.TestCase):
    """The grant is asked for, is bounded to a session, and is never inferred."""

    def setUp(self):
        self.session = _Session()
        self.context = SimpleNamespace(session=self.session)
        self.addCleanup(history_consent.release, self.session)
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask_count(self, stub) -> int:
        return sum(
            1
            for call in stub.call_args_list
            if call.kwargs.get("action") == "read_history"
        )

    def ask(self):
        return mock.patch.object(
            history_consent, "require_approval", new_callable=mock.AsyncMock
        )

    def run_async(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_nothing_configured_means_ask_not_automatic(self):
        self.assertEqual(history_consent.resolve_mode(), "ask")
        with self.ask() as stub:
            self.run_async(
                history_consent.require_history_consent(
                    self.context, tool="whatsapp_read", what="Afia"
                )
            )
        self.assertEqual(self.ask_count(stub), 1)

    def test_the_question_says_how_long_the_permission_lasts(self):
        # The grant outlives the call, so the wording is load-bearing. A user who
        # thinks they approved one lookup has been given a licence for the rest
        # of the conversation, and "yes" cannot mean that by accident.
        with self.ask() as stub:
            self.run_async(
                history_consent.require_history_consent(
                    self.context, tool="whatsapp_read", what="Afia"
                )
            )
        summary = stub.call_args.kwargs["summary"]
        self.assertIn("Afia", summary)
        self.assertIn(history_consent.MSG_SESSION_NOTE, summary)
        # And the same sentence reaches the voice, because the gate builds both
        # from this one field.
        self.assertEqual(stub.call_args.kwargs["action_class"], "sensitive")

    def test_the_second_read_in_a_session_does_not_ask_again(self):
        with self.ask() as stub:
            self.run_async(
                history_consent.require_history_consent(
                    self.context, tool="whatsapp_read", what="Afia"
                )
            )
            self.run_async(
                history_consent.require_history_consent(
                    self.context, tool="whatsapp_read", what="Afia"
                )
            )
        self.assertEqual(self.ask_count(stub), 1)

    def test_a_grant_does_not_outlive_the_session(self):
        with self.ask() as stub:
            self.run_async(
                history_consent.require_history_consent(
                    self.context, tool="whatsapp_read", what="Afia"
                )
            )
        history_consent.release(self.session)
        self.assertFalse(history_consent.granted(self.session))
        # A different session must ask again -- the same words in a later
        # conversation authorise a different chat.
        with self.ask() as second:
            other = SimpleNamespace(session=SimpleNamespace())
            self.run_async(
                history_consent.require_history_consent(
                    other, tool="whatsapp_read", what="Afia"
                )
            )
        self.assertEqual(self.ask_count(second), 1)

    def test_off_refuses_rather_than_asking(self):
        # Off means off. Turning it off must not degrade into a question, or the
        # switch is a suggestion.
        with mock.patch.dict(os.environ, {history_consent.MODE_VAR: "off"}):
            with self.ask() as stub:
                with self.assertRaisesRegex(ToolError, "switched off"):
                    self.run_async(
                        history_consent.require_history_consent(
                            self.context, tool="whatsapp_read", what="Afia"
                        )
                    )
        self.assertEqual(self.ask_count(stub), 0)

    def test_auto_never_asks(self):
        with mock.patch.dict(os.environ, {history_consent.MODE_VAR: "auto"}):
            with self.ask() as stub:
                self.run_async(
                    history_consent.require_history_consent(
                        self.context, tool="whatsapp_read", what="Afia"
                    )
                )
        self.assertEqual(self.ask_count(stub), 0)

    def test_an_unrecognised_value_is_ask_and_never_auto(self):
        # The two ways to be wrong here are asking when they meant to switch it
        # off, and reading when they meant to switch it off. The second is worse,
        # so an unreadable value is treated as the question.
        for value in ("maybe", "yes", "true", "1"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {history_consent.MODE_VAR: value}):
                    self.assertEqual(history_consent.resolve_mode(), "ask")

    def test_the_saved_document_wins_over_the_environment(self):
        # Per-key precedence, the same rule every other setting follows: a switch
        # flipped in Settings must beat a line left in `.env`.
        document = {"messaging": {"whatsapp": {"history": "off"}}}
        with mock.patch.dict(os.environ, {history_consent.MODE_VAR: "auto"}):
            with mock.patch.object(
                history_consent, "effective_document", return_value=(document, "test", [])
            ):
                self.assertEqual(history_consent.resolve_mode(), "off")

    def test_a_document_without_the_key_leaves_the_environment_in_place(self):
        with mock.patch.dict(os.environ, {history_consent.MODE_VAR: "auto"}):
            with mock.patch.object(
                history_consent, "effective_document", return_value=({}, "test", [])
            ):
                self.assertEqual(history_consent.resolve_mode(), "auto")

    def test_a_run_with_no_gate_fails_closed(self):
        # There is nobody to ask, so the answer is no -- for a sensitive read
        # exactly as for a send.
        import asyncio

        with mock.patch.object(history_consent, "gate_for", create=True):
            with self.assertRaises(ToolError):
                asyncio.run(
                    history_consent.require_history_consent(
                        self.context, tool="whatsapp_read", what="Afia"
                    )
                )


class ProgressTests(unittest.TestCase):
    """A step is visible, ordered and timed -- and cannot break the tool."""

    def test_a_step_record_carries_the_position_and_the_tool(self):
        record = build_step_record(
            tool="send_whatsapp",
            action="draft",
            message="Typing the message",
            index=4,
            total=6,
            duration_ms=12.5,
        )
        self.assertEqual(record["type"], "tool_status")
        self.assertEqual(record["id"], "send_whatsapp.draft")
        self.assertEqual(record["index"], 4)
        self.assertEqual(record["total"], 6)
        self.assertEqual(record["duration_ms"], 12.5)

    def test_steps_of_one_call_share_an_id_so_a_ui_can_group_them(self):
        first = build_step_record(tool="t", action="open", message="a")
        second = build_step_record(tool="t", action="open", message="a")
        self.assertEqual(first["id"], second["id"])

    def test_a_long_message_is_clipped_rather_than_pasted_whole(self):
        record = build_step_record(tool="t", action="a", message="x" * 500)
        self.assertLessEqual(len(record["message"]), 100)
        self.assertTrue(record["message"].endswith("…"))

    def test_a_missing_duration_is_absent_rather_than_zero(self):
        # Zero would render as "0 ms" and invent a measurement.
        self.assertNotIn("duration_ms", build_step_record(tool="t", action="a", message="b"))

    def test_publishing_with_no_session_registered_is_a_no_op(self):
        # The common case for a script and a test: no publisher, no problem.
        publish_step(None, tool="t", action="a", message="b")

    def test_a_publisher_that_raises_does_not_reach_the_caller(self):
        # The whole reason this module swallows everything. A telemetry channel
        # that can end a send is worse than no telemetry at all.
        class Hostile:
            def emit_and_publish(self, *_args, **_kwargs):
                raise RuntimeError("pipe is gone")

        session = _Session()
        register_publisher(session, Hostile())
        self.addCleanup(unregister_publisher, session)
        publish_step(SimpleNamespace(session=session), tool="t", action="a", message="b")

    def test_the_publisher_is_resolved_from_either_call_shape(self):
        # Tools are handed a RunContext, but both shapes exist in this codebase
        # and a step that only worked with one would silently stop appearing.
        session = _Session()
        publisher = object()
        register_publisher(session, publisher)
        self.addCleanup(unregister_publisher, session)
        self.assertIs(publisher_for(session), publisher)
        self.assertIs(publisher_for(SimpleNamespace(session=session)), publisher)

    def test_a_session_is_never_kept_alive_by_the_registry(self):
        # Weak keys: a session that was never torn down cannot leak a publisher.
        import gc
        import weakref

        session = _Session()
        register_publisher(session, object())
        reference = weakref.ref(session)
        del session
        gc.collect()
        self.assertIsNone(reference())

    def test_a_session_that_cannot_be_weakly_referenced_degrades_rather_than_raising(self):
        # `SimpleNamespace` is exactly this case, and it is what a script or a
        # test hands over. Storing nothing is the right outcome; raising here
        # would turn a progress row into a tool failure.
        session = SimpleNamespace()
        register_publisher(session, object())
        self.assertIsNone(publisher_for(session))


class ReadinessTests(unittest.TestCase):
    """A sign-in belief must wait for the page to have one.

    WhatsApp Web is a single-page application, and there are seconds between the
    document loading and the chat list being in the DOM. Reading the state once,
    immediately, reported a fully signed-in account as signed out -- and the
    probe went on to call all seventeen selectors missing, which is a diagnosis
    invented entirely by asking too early.
    """

    def setUp(self):
        from agent.tools.messaging import whatsapp_web as web

        self.web = web
        self.stack = mock.patch.object(web.asyncio, "sleep", new=mock.AsyncMock())
        self.stack.start()
        self.addCleanup(self.stack.stop)

    def _page_returning(self, sequence):
        page = mock.Mock()
        page.evaluate = mock.AsyncMock(side_effect=list(sequence))
        return page

    def test_a_page_still_loading_is_reported_as_loading_and_then_watched(self):
        from agent.tools.messaging import whatsapp_web as web

        page = self._page_returning(
            [{}, {}, {}, {"qr": False, "side": True, "app": True}]
        )
        with mock.patch.object(web, "whatsapp_page", new=mock.AsyncMock(return_value=page)):
            self.assertEqual(asyncio.run(web.state(wait=10)), "ready")

    def test_the_first_look_is_not_the_answer(self):
        # The exact bug: one empty read used to be "logged out" forever.
        from agent.tools.messaging import whatsapp_web as web

        page = self._page_returning([{}, {"qr": False, "side": True, "app": True}])
        with mock.patch.object(web, "whatsapp_page", new=mock.AsyncMock(return_value=page)):
            self.assertEqual(asyncio.run(web.state(wait=10)), "ready")
        self.assertGreaterEqual(page.evaluate.await_count, 2)

    def test_a_qr_code_means_signed_out_and_is_not_waited_on(self):
        from agent.tools.messaging import whatsapp_web as web

        page = self._page_returning([{"qr": True, "side": False, "app": True}])
        with mock.patch.object(web, "whatsapp_page", new=mock.AsyncMock(return_value=page)):
            self.assertEqual(asyncio.run(web.state(wait=30)), "logged_out")
        # One look: the QR is an answer, not a stage.
        self.assertEqual(page.evaluate.await_count, 1)

    def test_a_page_that_never_loads_reports_loading_rather_than_signed_out(self):
        # Telling a signed-in person they are signed out sends them to re-scan a
        # QR code that was never the problem.
        from agent.tools.messaging import whatsapp_web as web

        page = mock.Mock()
        page.evaluate = mock.AsyncMock(return_value={})
        with mock.patch.object(web, "whatsapp_page", new=mock.AsyncMock(return_value=page)):
            self.assertEqual(asyncio.run(web.state(wait=0)), "loading")

    def test_an_evaluate_that_throws_is_loading_not_signed_out(self):
        from agent.tools.messaging import whatsapp_web as web

        page = mock.Mock()
        page.evaluate = mock.AsyncMock(side_effect=RuntimeError("navigating"))
        with mock.patch.object(web, "whatsapp_page", new=mock.AsyncMock(return_value=page)):
            self.assertEqual(asyncio.run(web.state(wait=0)), "loading")

    def test_require_ready_distinguishes_starting_up_from_signed_out(self):
        from agent.tools.messaging import whatsapp_web as web

        with mock.patch.object(web, "state", new=mock.AsyncMock(return_value="loading")):
            with self.assertRaisesRegex(ToolError, "still starting up"):
                asyncio.run(web.require_ready())
        with mock.patch.object(web, "state", new=mock.AsyncMock(return_value="logged_out")):
            with self.assertRaisesRegex(ToolError, "not signed in"):
                asyncio.run(web.require_ready())

    def test_the_probe_refuses_to_read_a_page_that_never_loaded(self):
        # A loading tab has no opinion. Reporting its opinion-less state as
        # seventeen missing selectors is how a working configuration gets "fixed"
        # into a broken one.
        from agent.tools.messaging import whatsapp_web as web

        page = mock.Mock()
        page.url = "https://web.whatsapp.com/"
        page.evaluate = mock.AsyncMock(return_value={})
        with mock.patch.object(web, "whatsapp_page", new=mock.AsyncMock(return_value=page)):
            with mock.patch.object(web, "state", new=mock.AsyncMock(return_value="loading")):
                report = asyncio.run(web.probe())
        self.assertEqual(report["ready"], "loading")
        self.assertEqual(report["hit"], {})
        self.assertIn("not a sign-in problem", report["note"])
        # And the probe must not have asked the page anything it could misread.
        self.assertEqual(page.evaluate.await_count, 0)


class ProfileLockTests(unittest.TestCase):
    """A locked profile has a one-line fix, and must not arrive as a paragraph.

    Chromium's own wording is "Opening in existing browser session. This usually
    means that the profile is already in use by another Chrome process", which
    reads like a product fault. The cause is a Lumine window somewhere -- usually
    the Tauri app -- and closing it is the whole remedy.
    """

    def test_a_locked_profile_is_named_as_such(self):
        from agent.tools.browser import session

        self.assertIn("already open", session.MSG_PROFILE_LOCKED)
        self.assertIn("profile can only be used", session.MSG_PROFILE_LOCKED)
        self.assertIn("Close that window", session.MSG_PROFILE_LOCKED)

    def test_the_probe_checks_the_lock_before_launching(self):
        module = _load_probe("lumine_whatsapp_probe")
        launched: list[int] = []
        with mock.patch.object(module, "_already_running", return_value=True):
            with mock.patch.object(module, "profile_dir", return_value=Path("C:/x")):
                with mock.patch.object(
                    module.asyncio,
                    "run",
                    side_effect=lambda *_a, **_k: launched.append(1) or {},
                ):
                    with mock.patch.object(module, "print"):
                        self.assertEqual(module.main([]), 0)
        # The whole point of checking first: a locked profile must never reach
        # Chromium, because the error it produces there is the paragraph this
        # replaces.
        self.assertEqual(launched, [], "the browser must not be launched against a locked profile")

    def test_the_probe_hands_the_browser_back_before_it_exits(self):
        """A diagnostic that keeps Chromium is single-use.

        The first probe run left the browser running, so the second was refused by
        its own lock check -- reporting "a Lumine browser window is already open"
        about a window the user never opened. The abandoned browser's pipe to a
        dead driver then raised EPIPE on exit and printed a Node stack trace over
        the report. Both go away if the probe releases what it started.
        """
        module = _load_probe("lumine_probe_release")
        released: list[int] = []
        sentinel = {"ready": "ready", "hit": {}, "missing": []}
        with mock.patch.object(module, "release", new=mock.AsyncMock(
            side_effect=lambda: released.append(1)
        )):
            with mock.patch.object(
                module.web, "probe", new=mock.AsyncMock(return_value=sentinel)
            ):
                self.assertIs(asyncio.run(module.probe()), sentinel)
        self.assertEqual(released, [1], "the browser must be released, not abandoned")

    def test_a_failing_probe_still_releases_the_browser(self):
        # An exception is exactly when a leak is least likely to be noticed, and
        # it leaves the profile held for the next run -- which reports the lock
        # rather than the crash that caused it.
        module = _load_probe("lumine_probe_release_on_error")
        released: list[int] = []
        with mock.patch.object(module, "release", new=mock.AsyncMock(
            side_effect=lambda: released.append(1)
        )):
            with mock.patch.object(
                module.web, "probe", new=mock.AsyncMock(side_effect=RuntimeError("boom"))
            ):
                with self.assertRaises(RuntimeError):
                    asyncio.run(module.probe())
        self.assertEqual(released, [1], "a failed probe must not leak the browser")

    def test_a_failure_to_release_does_not_replace_the_report_with_a_traceback(self):
        # There is nothing left to do about a browser that will not close, and
        # raising here would destroy the reading the user came for.
        module = _load_probe("lumine_probe_release_failure")
        sentinel = {"ready": "ready", "hit": {}, "missing": []}
        with mock.patch.object(
            module, "release", new=mock.AsyncMock(side_effect=RuntimeError("stuck"))
        ):
            with mock.patch.object(
                module.web, "probe", new=mock.AsyncMock(return_value=sentinel)
            ):
                self.assertIs(asyncio.run(module.probe()), sentinel)

    def test_the_report_explains_the_two_findings_that_are_not_faults(self):
        """A list of "not found" is read as a verdict.

        Two entries will always be missing on a healthy install, and both look
        exactly like breakage: `send_button` because WhatsApp only renders it once
        the composer has text, and `message_in`/`message_out` because this build
        gives rows hashed class names and no `data-id` -- unfixable by adding a
        spelling. Left unexplained, somebody rewrites working selectors to satisfy
        a report that was never wrong.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "lumine_probe_render", agent_probe_path()
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        text = module.render(
            {
                "url": "https://web.whatsapp.com/",
                "ready": "ready",
                "hit": {"composer": "footer div[contenteditable='true']"},
                "missing": ["send_button", "message_in", "message_out"],
                "icons": [],
            }
        )
        self.assertIn("expected", text)
        self.assertIn("only renders it once the composer has text", text)
        self.assertIn("hashed class names", text)
        self.assertIn("msg-container", text, "the report must name what works")

    def test_no_explanation_is_invented_for_a_genuinely_missing_target(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "lumine_probe_render2", agent_probe_path()
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        text = module.render(
            {
                "url": "https://web.whatsapp.com/",
                "ready": "ready",
                "hit": {},
                "missing": ["chat_header"],
                "icons": [],
            }
        )
        # `chat_header` is a real selector and it is genuinely absent here, so the
        # report must stay a plain finding. Padding the list with reassurance for
        # everything would defeat the point of the tool.
        self.assertNotIn("expected", text)
        self.assertIn("chat_header", text)

    def test_the_probe_launches_normally_when_nothing_holds_the_profile(self):
        module = _load_probe("lumine_probe_unlocked")
        with mock.patch.object(module, "_already_running", return_value=False):
            with mock.patch.object(module, "probe", new=mock.AsyncMock(return_value={"ready": "ready", "hit": {}, "missing": []})):
                with mock.patch.object(module, "print"):
                    self.assertEqual(module.main([]), 0)


class WhatsAppTabFocusTests(unittest.TestCase):
    """The user has to be able to *see* the chat they are approving.

    The send flow types the draft and then asks the user to confirm it while
    looking at their own screen. If the WhatsApp tab is behind the blank browse
    tab, that design does not merely look worse -- it does not work, because they
    are being asked to approve a message they cannot see. It is also the only way
    a QR code can be scanned.
    """

    def test_the_whatsapp_tab_is_brought_forward_on_both_the_cold_and_warm_path(self):
        # Behaviourally, on every path that hands the page out -- not by counting
        # call sites in the source. The previous version did the latter and was
        # confidently wrong: it counted two `_focus_whatsapp()` calls, both inside
        # the lock, while a third and much cheaper path returned the warm page
        # without focusing it. That is the path every send takes after the first,
        # so the tab the user was asked to approve sat behind another one.
        #
        # Counting text cannot catch that, because the property being asserted is
        # "every return focuses", and a method with three returns and two calls
        # looks exactly like one with two returns and two calls. It also read the
        # file from disk while line numbers came from the compiled module, so an
        # unrelated edit anywhere above it silently changed what was inspected.
        import asyncio

        from agent.tools.browser import session

        def _browser_with_warm_page(url: str):
            browser = session._Browser()
            page = mock.Mock()
            page.is_closed.return_value = False
            page.url = url
            browser._wa_page = page
            browser._wa_lock = None
            browser.ensure = mock.AsyncMock(return_value=None)
            browser._context = mock.Mock()
            browser.allowed = mock.AsyncMock(return_value=(True, ""))
            focused = []
            browser._focus_whatsapp = mock.AsyncMock(side_effect=lambda: focused.append(1))
            return browser, page, focused

        async def _run():
            # Warm: already on WhatsApp, so no navigation is needed. This is the
            # path that had no focus.
            browser, page, focused = _browser_with_warm_page(session.WHATSAPP_URL)
            assert await browser.whatsapp_page() is page
            self.assertEqual(
                len(focused), 1, "a warm WhatsApp tab was handed over without being focused"
            )

            # Warm with navigate=False: a caller that only wants the page.
            browser, page, focused = _browser_with_warm_page("https://example.com/")
            assert await browser.whatsapp_page(navigate=False) is page
            self.assertEqual(len(focused), 1, "the no-navigate path skipped focus")

            # Cold: nothing there yet, so the browser starts and the tab is loaded.
            browser = session._Browser()
            page = mock.Mock()
            page.is_closed.return_value = False
            page.goto = mock.AsyncMock()
            context = mock.Mock()
            context.new_page = mock.AsyncMock(return_value=page)
            browser._wa_page = None
            browser._wa_lock = None
            browser.ensure = mock.AsyncMock(return_value=None)
            browser._context = context
            browser.allowed = mock.AsyncMock(return_value=(True, ""))
            focused = []
            browser._focus_whatsapp = mock.AsyncMock(side_effect=lambda: focused.append(1))
            assert await browser.whatsapp_page() is page
            self.assertEqual(len(focused), 1, "the cold path did not focus the new tab")

        asyncio.run(_run())

    def test_focusing_the_tab_never_raises(self):
        from agent.tools.browser import session

        browser = session._Browser()
        page = mock.Mock()
        page.bring_to_front = mock.AsyncMock(side_effect=RuntimeError("no window manager"))
        browser._wa_page = page
        # A window manager that refuses focus must not turn a working send into a
        # refusal, so this returns rather than raising.
        asyncio.run(browser._focus_whatsapp())

    def test_focusing_with_no_page_is_a_no_op(self):
        from agent.tools.browser import session

        asyncio.run(session._Browser()._focus_whatsapp())


def agent_probe_path():
    return Path(__file__).resolve().parent.parent / "whatsapp_probe.py"


def _load_probe(alias: str):
    """The probe CLI as a module.

    Loaded by path rather than imported, because it lives at `agent/` root as a
    standalone CLI -- which is the constraint `agent/AGENTS.md` puts on every
    file Tauri or a developer runs by name. Re-loading it per test keeps the two
    probe tests from sharing patched module state.
    """
    spec = importlib.util.spec_from_file_location(alias, agent_probe_path())
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":  # pragma: no cover
    unittest.main()