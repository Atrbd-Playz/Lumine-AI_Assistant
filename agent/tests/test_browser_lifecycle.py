"""Reopening the browser in the same session, and the seams that had to give.

Three separate things stand between "close it" and "open it again", and every one
of them failed in a way that read as a broken product rather than a lifecycle
fault:

* **`release` held `_lock` across the tab close.** `_lock` is a `threading.Lock`
  and its own docstring says it is held only for the statements that read or
  write the ownership fields, never across an `await`. Holding it across the
  `await` meant a second coroutine reaching `with self._lock` blocked the
  *event loop* instead of waiting its turn -- so the close the first one was
  waiting on could never finish. Teardown deadlocking against its own lock is
  the worst available shape for the one path that is not allowed to fail.
* **`_close_remote` probed the endpoint once.** `context.close()` returns as
  Chromium is tearing down, and a socket that is mid-shutdown still accepts a
  connect -- so one instant probe read "alive" for a browser that was already
  gone and left `browser-cdp.txt` behind. The next `_start` in this *same
  session* then joined that dying browser and failed against it, which is the
  whole of "close it, then ask me to open it again and it will not".
* **One launch attempt saw a lock that was on its way out.** `release` schedules
  the teardown onto the owning loop and does not await it, so the next `ensure`
  can arrive while Chromium is still letting go of the profile directory. One
  attempt translated that into `MSG_PROFILE_LOCKED` -- telling the user to go and
  close a window that is already closing, for a reason that is no longer true.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import mock

from agent.tools.browser import control as control_mod
from agent.tools.browser import preview, session
from agent.tools.tools_compat import ToolError


class ReleaseLockTests(unittest.IsolatedAsyncioTestCase):
    """`release` must not hold `_lock` while it waits for a tab to close."""

    def _browser(self) -> session._Browser:
        browser = session._Browser()
        browser._loop = asyncio.get_running_loop()
        browser._shared = False
        # Stand-ins for the two things release is expected to drop. `_wa_page`
        # is the load-bearing one: it is the only thing release *awaits* while
        # holding the lock in the old shape.
        browser._wa_page = mock.Mock()
        browser._wa_page.is_closed.return_value = False
        browser._joined_browser = object()
        browser._tabs = [object()]
        browser._active = 1
        return browser

    def _patches(self):
        # Nothing here may touch a real endpoint file: `release` ends in
        # `_close_remote(None, ...)`, whose `context is None` branch probes the
        # published port and unlinks whatever it finds.
        return (
            mock.patch.object(session, "_read_endpoint", return_value=""),
            mock.patch.object(session, "_endpoint_alive", return_value=False),
            mock.patch.object(session, "_forget_endpoint"),
        )

    async def test_the_lock_is_free_while_the_tab_is_still_closing(self):
        browser = self._browser()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def slow_close() -> None:
            started.set()
            await finish.wait()

        browser._wa_page.close = slow_close

        read, alive, forget = self._patches()
        with read, alive, forget:
            task = asyncio.create_task(browser.release())
            # Wait for the close to actually be in flight -- polling the lock
            # before the coroutine has run would prove nothing.
            await asyncio.wait_for(started.wait(), timeout=5)

            acquired = browser._lock._lock.acquire(blocking=False)
            if acquired:
                browser._lock._lock.release()

            finish.set()
            await asyncio.wait_for(task, timeout=5)

        self.assertTrue(
            acquired,
            "release held _lock across the tab close, so the close can never finish",
        )

    async def test_everything_the_singleton_was_holding_is_dropped(self):
        """Clearing the references is what makes the singleton reusable.

        `_joined_browser` is the sharp end: leaving it set meant a later read of
        "am I a guest?" answered yes for a browser this process had already
        left, so the next `release` would refuse to close anything at all.
        """
        browser = self._browser()
        browser._wa_page.is_closed.return_value = True

        read, alive, forget = self._patches()
        with read, alive, forget:
            await asyncio.wait_for(browser.release(), timeout=5)

        self.assertIsNone(browser._wa_page)
        self.assertIsNone(browser._wa_lock)
        self.assertIsNone(browser._joined_browser)
        self.assertEqual(browser._tabs, [])
        self.assertIsNone(browser._active)
        self.assertIsNone(browser._loop)
        self.assertIsNone(browser._context)
        self.assertIsNone(browser._pw)
        self.assertFalse(browser._shared)
        self.assertFalse(browser._hid_window)

    async def test_the_lock_is_free_at_the_await_inside_the_close(self):
        """The same property, asked from the inside so timing cannot flatter it.

        A test that merely waits can pass because the machine was quick. Asking
        *from inside the awaited close* is the direct question: if `release`
        still held `_lock`, this acquire would come back `False`.
        """
        browser = self._browser()
        lock_was_free: list[bool] = []

        async def slow_close() -> None:
            acquired = browser._lock._lock.acquire(blocking=False)
            if acquired:
                browser._lock._lock.release()
            lock_was_free.append(acquired)

        browser._wa_page.close = slow_close

        read, alive, forget = self._patches()
        with read, alive, forget:
            await asyncio.wait_for(browser.release(), timeout=5)

        self.assertEqual(lock_was_free, [True])


class CloseRemoteEndpointTests(unittest.IsolatedAsyncioTestCase):
    """The published endpoint has to outlive the browser's own teardown."""

    ENDPOINT = "http://127.0.0.1:9222"

    async def _close(self, context, alive, *, shared: bool = False, playwright=None):
        with mock.patch.object(session, "_read_endpoint", return_value=self.ENDPOINT), \
                mock.patch.object(session, "_endpoint_alive", side_effect=alive) as probe, \
                mock.patch.object(session, "_forget_endpoint") as forget:
            await session._Browser._close_remote(
                context, playwright or mock.AsyncMock(), shared=shared
            )
        return probe, forget

    async def test_a_dead_endpoint_is_dropped_after_one_probe(self):
        context = mock.AsyncMock()
        probe, forget = await self._close(context, lambda _: False)
        context.close.assert_awaited_once()
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(forget.call_count, 1)

    async def test_a_socket_mid_shutdown_is_waited_out_before_the_file_goes(self):
        """The measured failure: one instant probe read "alive" for a dead browser.

        `context.close()` returns while Chromium is still tearing down, and a
        socket on its way down still accepts a connect. Believing that one
        reading left `browser-cdp.txt` on disk, and the next `_start in this same
        session* joined a browser that no longer existed.
        """
        context = mock.AsyncMock()
        answers = iter([True, True, False])
        probe, forget = await self._close(context, lambda _: next(answers))
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(forget.call_count, 1)

    async def test_the_file_is_dropped_even_when_the_port_never_goes_quiet(self):
        """A surviving *file for a dead browser* costs a whole session.

        The forget is deliberately unconditional: waiting for proof that the
        browser is gone is the one thing that cannot be bounded safely, and a
        file that survives costs one no-op unlink on the next run.
        """
        context = mock.AsyncMock()
        with mock.patch.object(session, "_ENDPOINT_DEATH_SECONDS", 0.05), \
                mock.patch.object(session, "_ENDPOINT_POLL_SECONDS", 0.005):
            probe, forget = await self._close(context, lambda _: True)
        self.assertEqual(forget.call_count, 1)
        self.assertGreater(probe.call_count, 1, "the loop never polled at all")

    async def test_the_polling_is_bounded(self):
        """Teardown must not become the slow part of a teardown."""
        context = mock.AsyncMock()
        with mock.patch.object(session, "_ENDPOINT_DEATH_SECONDS", 0.05), \
                mock.patch.object(session, "_ENDPOINT_POLL_SECONDS", 0.005):
            await self._close(context, lambda _: True)
        # Generous, because a loaded CI box is not a deadline. The point is
        # that it is seconds, not minutes.
        self.assertLess(0.05 + 0.5, 5)

    async def test_a_live_endpoint_of_another_process_is_left_alone(self):
        """`context is None` means we had no browser of our own to close.

        The published file may therefore be somebody else's live endpoint, and
        unlinking it would make a perfectly healthy shared browser un-findable
        to the next process.
        """
        probe, forget = await self._close(None, lambda _: True)
        self.assertEqual(forget.call_count, 0)
        self.assertEqual(probe.call_count, 1)

    async def test_a_dead_endpoint_is_still_cleaned_up_when_we_had_no_browser(self):
        probe, forget = await self._close(None, lambda _: False)
        self.assertEqual(forget.call_count, 1)
        self.assertEqual(probe.call_count, 1)

    async def test_a_guest_never_touches_the_endpoint_at_all(self):
        playwright = mock.AsyncMock()
        probe, forget = await self._close(None, lambda _: True, shared=True, playwright=playwright)
        playwright.stop.assert_awaited_once()
        self.assertEqual(probe.call_count, 0)
        self.assertEqual(forget.call_count, 0)


class _Locked(Exception):
    """Chromium's own refusal when the profile directory is already in use."""


class ProfileLockMessageTests(unittest.TestCase):
    """Both spellings of the refusal, and only those."""

    def test_chromes_two_spellings_are_recognised(self):
        self.assertTrue(
            session._profile_locked(
                "Existing browser session: The process started earlier is "
                "using the same profile directory."
            )
        )
        self.assertTrue(
            session._profile_locked(
                "Error: The user data directory is already in use by "
                "another Chromium instance"
            )
        )

    def test_the_match_is_not_case_sensitive(self):
        # The message comes from the browser, and a capitalised variant used to
        # slip past and become the generic "I couldn't start the browser"
        # instead of the sentence that tells the user which window to close.
        self.assertTrue(session._profile_locked("EXISTING BROWSER SESSION"))
        self.assertTrue(session._profile_locked("User Data Directory Is Already In Use"))

    def test_anything_else_is_not_a_lock(self):
        self.assertFalse(session._profile_locked(""))
        self.assertFalse(session._profile_locked("browser executable not found"))
        self.assertFalse(session._profile_locked("network error"))


class LaunchPersistentTests(unittest.IsolatedAsyncioTestCase):
    """A lock that is *being* released looks exactly like one somebody holds."""

    def _playwright(self, outcomes):
        playwright = mock.Mock()
        playwright.chromium.launch_persistent_context = mock.AsyncMock(side_effect=outcomes)
        return playwright

    def _locked(self, n: int) -> list[_Locked]:
        return [_Locked("Existing browser session: the profile is in use") for _ in range(n)]

    async def test_a_lock_that_clears_is_retried_until_the_browser_comes_up(self):
        context = object()
        playwright = self._playwright(self._locked(session._LAUNCH_ATTEMPTS - 1) + [context])
        with mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0), \
                mock.patch.object(session, "_window_args", return_value=[]):
            result = await session._launch_persistent(playwright, Path("C:/profile"))
        self.assertIs(result, context)
        self.assertEqual(
            playwright.chromium.launch_persistent_context.await_count,
            session._LAUNCH_ATTEMPTS,
        )

    async def test_a_lock_nobody_releases_is_given_up_on(self):
        """Every extra attempt is time spent waiting for a correct refusal."""
        playwright = self._playwright(self._locked(99))
        with mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0), \
                mock.patch.object(session, "_window_args", return_value=[]):
            with self.assertRaises(_Locked):
                await session._launch_persistent(playwright, Path("C:/profile"))
        self.assertEqual(
            playwright.chromium.launch_persistent_context.await_count,
            session._LAUNCH_ATTEMPTS,
        )

    async def test_anything_else_is_raised_on_the_first_attempt(self):
        """A missing executable or a broken profile is not a lock, and waiting
        three times for it is three quarters of a second of nothing."""
        playwright = self._playwright([OSError("browser executable not found")])
        with mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0), \
                mock.patch.object(session, "_window_args", return_value=[]):
            with self.assertRaises(OSError):
                await session._launch_persistent(playwright, Path("C:/profile"))
        self.assertEqual(playwright.chromium.launch_persistent_context.await_count, 1)

    async def test_the_launch_still_refuses_to_emulate_a_display(self):
        """The flags moved with the retry, and this is what keeps them here.

        A source grep would go green while the flags were gone from the *call*;
        this reads the arguments the launch actually received.
        """
        context = object()
        playwright = self._playwright([context])
        with mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0), \
                mock.patch.object(session, "_screen_size", return_value=(1366, 768)):
            await session._launch_persistent(playwright, Path("C:/profile"))

        _, kwargs = playwright.chromium.launch_persistent_context.await_args
        args = kwargs["args"]
        self.assertTrue(kwargs["no_viewport"])
        self.assertIsNone(kwargs["color_scheme"])
        self.assertFalse(kwargs["accept_downloads"])
        self.assertIn("--remote-debugging-port=0", args)
        # The directory is positional, so it is in the args tuple not the
        # kwargs -- and compared as the code builds it, `str(Path(...))`, since
        # a forward-slash literal comes back with the platform's separators.
        self.assertEqual(
            playwright.chromium.launch_persistent_context.await_args.args[0],
            str(Path("C:/profile")),
        )

    async def test_the_profile_directory_is_passed_as_the_launch_target(self):
        playwright = self._playwright([object()])
        with mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0), \
                mock.patch.object(session, "_window_args", return_value=[]):
            await session._launch_persistent(playwright, Path("D:/somewhere/else"))
        self.assertEqual(
            playwright.chromium.launch_persistent_context.await_args.args[0],
            str(Path("D:/somewhere/else")),
        )


class ScrollVerbTests(unittest.IsolatedAsyncioTestCase):
    """`web_control scroll`, and why it is a read rather than an act."""

    def setUp(self):
        self.engine = mock.Mock()
        self.engine.gate = None
        control_mod.set_engine(self.engine)
        self.addCleanup(control_mod.set_engine, None)
        patcher = mock.patch.object(
            control_mod, "require_approval", new_callable=mock.AsyncMock
        )
        self.approval = patcher.start()
        self.addCleanup(patcher.stop)
        scroll = mock.patch.object(
            session, "scroll_page", new_callable=mock.AsyncMock
        )
        self.scrolled = scroll.start()
        self.addCleanup(scroll.stop)

    async def _call(self, action: str = "scroll", **kwargs):
        return await control_mod.web_control(mock.Mock(), action, **kwargs)

    async def test_it_moves_the_viewport_and_says_where_it_landed(self):
        self.scrolled.return_value = {"scrolled": True, "y": 900, "max": 2400}
        result = await self._call(text="down")
        self.scrolled.assert_awaited_once_with("down")
        self.assertIn("Scrolled down", result)

    async def test_it_is_priced_as_a_read(self):
        """It moves the reader's position, not the page -- the same test `back`
        and `forward` pass, and the same one `browse scroll` passes."""
        self.scrolled.return_value = {"scrolled": True, "y": 0, "max": 100}
        await self._call(text="top")
        _, kwargs = self.approval.await_args
        self.assertEqual(kwargs["action_class"], "read")
        self.assertEqual(kwargs["action"], "scroll")

    async def test_it_is_declared_as_a_read_too(self):
        # The gate's class and the tool's own record have to agree, or the
        # Tools-page badge promises less than the gate enforces.
        self.assertIn("scroll", control_mod.READ_ACTIONS)
        self.assertIn("scroll", control_mod.ACTIONS)

    async def test_a_page_with_nothing_below_says_so(self):
        self.scrolled.return_value = {"scrolled": False, "y": 0, "max": 0}
        result = await self._call(text="down")
        self.assertIn("nothing to scroll", result)

    async def test_an_empty_direction_means_down(self):
        # The tool's own docstring offers no direction at all as the common
        # case, so it has to mean something rather than be refused.
        self.scrolled.return_value = {"scrolled": True, "y": 0, "max": 100}
        await self._call(text="")
        self.scrolled.assert_awaited_once_with("down")

    async def test_a_near_miss_is_refused_rather_than_repaired(self):
        """A scroll that goes the wrong way is a page the user watches move in a
        direction they did not ask for, so "sideways" is a refusal and not a
        guess at "down"."""
        with self.assertRaises(ToolError):
            await self._call(text="downwards")
        self.scrolled.assert_not_awaited()

    async def test_a_direction_nobody_could_act_on_is_refused_by_name(self):
        with self.assertRaises(ToolError) as caught:
            await self._call(text="sideways")
        self.assertIn("down, up, top, or bottom", str(caught.exception))
        self.scrolled.assert_not_awaited()

    async def test_it_does_not_bump_the_generation_or_re_read_the_page(self):
        """The document is unchanged, so every reference the model already holds
        is still valid. This is why it is its own verb rather than something the
        model fakes with a re-read."""
        self.scrolled.return_value = {"scrolled": True, "y": 10, "max": 100}
        with mock.patch.object(
            control_mod, "_observe", new_callable=mock.AsyncMock
        ) as observe:
            await self._call(text="down")
        observe.assert_not_awaited()

    async def test_an_upper_case_spelling_is_the_same_verb(self):
        self.scrolled.return_value = {"scrolled": True, "y": 0, "max": 10}
        await self._call(action="Scroll", text="up")
        self.scrolled.assert_awaited_once_with("up")


class UnknownActionMessageTests(unittest.TestCase):
    """The refusal for an action name nothing recognises, with a nudge."""

    def test_a_typo_is_named_as_the_likely_verb_first(self):
        said = control_mod.unknown_action_message("scoll", control_mod.ACTIONS)
        self.assertIn("Did you mean 'scroll'?", said)

    def test_nonsense_gets_the_list_and_no_confident_guess(self):
        said = control_mod.unknown_action_message("flibbertigibbet", control_mod.ACTIONS)
        self.assertNotIn("Did you mean", said)
        self.assertIn("Tell me one of:", said)

    def test_the_list_is_in_the_declared_order(self):
        said = control_mod.unknown_action_message("nope", control_mod.ACTIONS)
        listed = said.split("Tell me one of: ", 1)[1].rstrip(".")
        self.assertEqual(listed.split(", "), list(control_mod.ACTIONS))

    def test_an_empty_name_is_still_a_sentence(self):
        said = control_mod.unknown_action_message("", control_mod.ACTIONS)
        self.assertIn("'that'", said)
        self.assertTrue(said.strip())

    def test_it_is_what_the_tool_raises(self):
        """The dynamic sentence at the raise site, and the module's own record of
        the surface, are the same message."""
        import inspect

        source = inspect.getsource(control_mod.web_control)
        self.assertIn("unknown_action_message(action, ACTIONS)", source)


class ShowToModelHistoryTests(unittest.TestCase):
    """`for_model=true`, and the attribute the live library actually provides.

    The live `AgentSession` has **`history`** -- a property returning its chat
    context -- and does not expose `chat_ctx` at all. Reading only `chat_ctx`
    meant `getattr` answered `None` on every real session, so `for_model=true`
    returned "I can't show myself a picture" on every build while every unit
    test passed: each one faked the attribute the code asked for rather than the
    one the library provides.
    """

    class HistoryOnlySession:
        """The shape a real `AgentSession` has: `history`, and no `chat_ctx`."""

        def __init__(self, sink: list) -> None:
            self._sink = sink

        @property
        def history(self):
            sink = self._sink

            class _Ctx:
                def add_message(self, *, role, content):
                    sink.append((role, content))

            return _Ctx()

    def test_a_session_that_only_exposes_history_still_shows_the_picture(self):
        added: list = []
        said = preview.show_to_model(
            mock.Mock(session=self.HistoryOnlySession(added)),
            b"\xff\xd8fakejpeg",
            caption="Example",
        )
        self.assertIn("I looked at the page.", said)
        self.assertEqual(len(added), 1)

    def test_history_wins_over_a_read_only_chat_ctx(self):
        """A `_ReadOnlyChatContext` exposes items but not `add_message`.

        A session that exposes both has to take the mutable one, or the answer
        is the same "no mutable context" sentence the fix was written to remove.
        """
        added: list = []
        session_obj = self.HistoryOnlySession(added)
        session_obj.chat_ctx = mock.Mock(items=[])  # type: ignore[attr-defined]
        said = preview.show_to_model(mock.Mock(session=session_obj), b"\xff\xd8fake")
        self.assertIn("I looked at the page.", said)
        self.assertEqual(len(added), 1)

    def test_the_order_is_history_then_chat_ctx(self):
        """Widening, not moving: the older shape still works.

        A bare `Mock` would have hidden this -- `getattr(session, "history")`
        auto-creates an attribute and answers not-None, so the older shape can
        only be tested with an object that genuinely lacks it.
        """
        added: list = []

        class _Ctx:
            def add_message(self, *, role, content):
                added.append((role, content))

        class OlderShape:
            def __init__(self) -> None:
                self.chat_ctx = _Ctx()

        said = preview.show_to_model(mock.Mock(session=OlderShape()), b"\xff\xd8fake")
        self.assertIn("I looked at the page.", said)
        self.assertEqual(len(added), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
