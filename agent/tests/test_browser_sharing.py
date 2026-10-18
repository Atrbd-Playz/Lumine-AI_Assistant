"""One browser, many agents -- and a window that fits the screen.

Two problems, both reported from using it rather than from reading it.

**"Other agents can't reach the browser."** Chromium takes an *exclusive* lock on a
user-data directory, so a second process launching against the same profile failed
with Chromium's own paragraph about sessions. Persistent is not optional here --
being signed into WhatsApp is the entire reason the browser exists -- so the lock
cannot be avoided. It is worked with instead: the browser publishes a DevTools
endpoint, and a later process *joins* it.

**"The window looks odd -- it changes the theme and overflows my screen."** Measured
rather than guessed: this machine's screen is 1366x768 and the page reported a
viewport of 1422x800. Playwright substitutes a viewport and forces
``color_scheme="light"``, so the window was genuinely larger than the display and
every site rendered light on a machine set to dark.
"""

from __future__ import annotations

import socket
import unittest
from pathlib import Path
from unittest import mock

from agent.tools.browser import session


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


class WindowGeometryTests(unittest.TestCase):
    """The window has to fit the screen it is on."""

    def test_the_window_is_sized_from_the_screen_not_from_a_default(self):
        with mock.patch.object(session, "_screen_size", return_value=(1920, 1080)):
            args = session._window_args()
        joined = " ".join(args)
        self.assertIn("--window-size=1920,1020", joined)
        self.assertIn("--window-position=0,0", joined)

    def test_the_height_leaves_room_for_the_taskbar(self):
        # Flush against the bezel is what reads as "it overflows my screen".
        with mock.patch.object(session, "_screen_size", return_value=(1366, 768)):
            args = session._window_args()
        size = next(a for a in args if a.startswith("--window-size="))[len("--window-size="):]
        _, _, height = size.partition(",")
        self.assertLess(int(height), 768)
        self.assertGreater(int(height), 480)

    def test_an_unknowable_screen_still_yields_a_usable_window(self):
        with mock.patch.object(session, "_screen_size", return_value=(0, 0)):
            args = " ".join(session._window_args())
        width = int(args.split("--window-size=")[1].split(",")[0])
        self.assertGreaterEqual(width, 640)

    def test_no_viewport_and_no_forced_colour_scheme(self):
        """The theme complaint, and the overflow, have one cause each.

        Playwright's defaults substitute a viewport and force light. Both have to
        be turned off explicitly -- ``None`` means "emulate nothing", which is what
        any window the user opens does.
        """
        import inspect

        # The launch arguments live in `_launch_persistent`, which `_start` calls:
        # they moved there with the bounded retry for a profile lock, and a test
        # that grepped `_start` would have gone green while the flags were gone.
        source = inspect.getsource(session._launch_persistent)
        self.assertIn("no_viewport=True", source)
        self.assertIn("color_scheme=None", source)
        self.assertIn("_launch_persistent(", inspect.getsource(session._Browser._start))


class SharedBrowserTests(unittest.TestCase):
    """Joining a running browser instead of failing against its lock."""

    def setUp(self):
        self.port = _free_port()
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen.bind(("127.0.0.1", self.port))
        self._listen.listen(1)
        self.addCleanup(self._listen.close)

    def _with_endpoint(self, text: str):
        return mock.patch.object(session, "_endpoint_file", return_value=Path(text))

    def test_a_published_and_live_endpoint_is_found(self):
        with self._with_endpoint(f"C:/fake/{self.port}"):
            with mock.patch.object(
                session, "_endpoint_file", return_value=Path(f"C:/x/{self.port}")
            ):
                import tempfile

                with tempfile.NamedTemporaryFile(
                    "w", suffix=".txt", delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(self.endpoint + "\n")
                    name = handle.name
                with mock.patch.object(session, "_endpoint_file", return_value=Path(name)):
                    self.assertEqual(session._read_endpoint(), self.endpoint)
                    self.assertTrue(session._endpoint_alive(self.endpoint))

    def test_a_dead_port_is_not_an_endpoint(self):
        import tempfile

        dead = f"http://127.0.0.1:{_free_port()}"
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(dead + "\n")
            name = handle.name
        with mock.patch.object(session, "_endpoint_file", return_value=Path(name)):
            self.assertEqual(session._read_endpoint(), dead)
            # A crashed browser leaves its endpoint file behind. Reading that as
            # "a browser is running" would refuse every later run forever.
            self.assertFalse(session._endpoint_alive(dead))

    def test_nonsense_in_the_endpoint_file_is_not_an_endpoint(self):
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("http://evil.example.com:9222\n")
            name = handle.name
        with mock.patch.object(session, "_endpoint_file", return_value=Path(name)):
            # Not loopback. Connecting to an arbitrary host on the strength of a
            # file somebody can write would be a remote-code-execution primitive
            # wearing a convenience feature.
            self.assertEqual(session._read_endpoint(), "")

    @staticmethod
    def _probe_module(alias: str):
        import importlib.util

        agent = Path(__file__).resolve().parent.parent / "whatsapp_probe.py"
        spec = importlib.util.spec_from_file_location(alias, agent)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_running_browser_is_not_a_reason_for_the_probe_to_refuse(self):
        """The refusal that made every tool single-use.

        The probe checked the profile lock and told the user to close a window. With
        sharing, a running browser is the *normal* state once the worker or the
        desktop app has it up -- and refusing is what stopped the worker, the app
        and a diagnostic from coexisting at all.
        """
        probe = self._probe_module("probe_joinable")
        with mock.patch.object(probe, "sharing_endpoint", return_value=(self.endpoint, True)):
            with mock.patch.object(probe, "profile_dir", return_value=Path("C:/nope")):
                self.assertFalse(probe._already_running())

    def test_a_genuinely_held_lock_is_refused_and_a_stale_one_is_not(self):
        """The case sharing cannot help, and the two that look identical on disk.

        Somebody's own Chromium, or a crashed Lumine holding a lock nobody will
        release -- there is no endpoint, so connecting is not an option. That has to
        be told apart from a **stale** lockfile left behind by a crash, which is the
        common case and must be cleaned up rather than refused forever.

        The previous check asked by trying to open the file with ``O_EXCL``, and on
        Windows that **succeeded** -- ``O_EXCL`` means nothing without ``O_CREAT`` --
        so a genuinely held lock was reported as free and the check never once fired.
        A test that only ever used an *unheld* file could not see that, which is
        exactly the fixture that hid it.
        """
        import os as _os
        import tempfile

        probe = self._probe_module("probe_locked")

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            lock = directory / "lockfile"
            lock.write_text("", encoding="utf-8")

            # Stale: the file is there and nobody holds it.
            with mock.patch.object(probe, "sharing_endpoint", return_value=("", False)):
                with mock.patch.object(probe, "profile_dir", return_value=directory):
                    self.assertFalse(probe._already_running())
            # ...and the stale file was cleaned up rather than left to confuse the
            # next run.
            self.assertFalse(lock.exists())

            # Held: re-create it and take an exclusive lock the way Chromium does.
            lock.write_text("x", encoding="utf-8")
            handle = _os.open(str(lock), _os.O_RDWR)
            released = False
            try:
                if _os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
                    released = True
                with mock.patch.object(probe, "sharing_endpoint", return_value=("", False)):
                    with mock.patch.object(probe, "profile_dir", return_value=directory):
                        self.assertTrue(probe._already_running())
            finally:
                if released:
                    import msvcrt

                    try:
                        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
                    except OSError:  # noqa: BLE001
                        pass
                _os.close(handle)

    def test_the_probe_consults_the_shared_helper_not_a_private_one(self):
        """Patching a private in the wrong module copy is a test that lies.

        The probe at `agent/` root imports ``tools.browser.session``; its tests import
        ``agent.tools.browser.session``. Two module objects, one name -- so patching
        the private in one and calling through the other silently tested nothing.
        One public function, imported by name, cannot drift like that.
        """
        probe = self._probe_module("probe_helper")
        self.assertTrue(hasattr(probe, "sharing_endpoint"))
        self.assertTrue(hasattr(session, "sharing_endpoint"))


class GuestTests(unittest.TestCase):
    """A process that *joined* the browser does not tidy up the house.

    Three destructive things used to happen unconditionally in `_start` and
    `release`, and each one is somebody else's tab when the browser is shared:
    closing every page but the first, clearing the WhatsApp tab reference, and
    closing the context -- which for a CDP connection tears down the whole browser,
    including a conversation another room is mid-read on.
    """

    def test_a_guest_detaches_instead_of_closing_the_browser(self):
        import asyncio

        context = mock.AsyncMock()
        playwright = mock.AsyncMock()
        asyncio.run(session._Browser._close_remote(context, playwright, shared=True))
        # `context.close()` on a CDP connection tears down the *browser*, so a
        # guest leaving must not call it. Stopping its own client is all.
        context.close.assert_not_called()
        playwright.stop.assert_awaited_once()

    def test_an_owner_still_closes_the_browser(self):
        browser = session._Browser()
        context = mock.Mock()
        playwright = mock.Mock()
        context.close = mock.AsyncMock()
        playwright.stop = mock.AsyncMock()
        import asyncio

        asyncio.run(browser._close_remote(context, playwright, shared=False))
        context.close.assert_awaited()

    def test_a_guest_keeps_the_endpoint_published(self):
        # The browser is still running, so the next process must still find it.
        # Unpublishing on exit is how a shared browser becomes un-findable after
        # one short-lived diagnostic has run.
        import inspect

        source = inspect.getsource(session._Browser._close_remote)
        early = source.index("if shared:")
        forget = source.index("_forget_endpoint()")
        self.assertLess(early, forget)
        self.assertIn("return", source[early:forget])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()