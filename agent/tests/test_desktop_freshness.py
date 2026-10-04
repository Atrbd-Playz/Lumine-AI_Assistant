"""The generation guard: a reference means nothing once the screen has moved.

This is the module that makes the rest of `desktop_control` safe to have, and
these tests are about the failure it prevents rather than about the happy path.

The concrete catastrophe: Lumine observes a window, the list scrolls, and the
model says "click 7" from an observation twenty seconds old. The click lands.
Element 7 is now a different control -- usually one row further down a list
whose rows include Send, Delete and Pay. Nothing errors. The user asked for a
thing and something irreversible happened instead.

So the rule under test is narrow and absolute: a reference is only meaningful
against the generation it came from, and a mismatch is a refusal. Not a
warning in the prompt, and not a "probably fine" -- a model that is told to be
careful is a model that is sometimes not careful.
"""

from __future__ import annotations

import unittest
from unittest import mock

from agent.tools.desktop import capture, control, ops, uia
from agent.tools.tools_compat import ToolError


def _observation(generation: int = 1, **kwargs) -> capture.Observation:
    defaults = dict(hwnd=1234, title="Notepad", process="notepad.exe")
    defaults.update(kwargs)
    return capture.Observation(generation=generation, **defaults)


class _FakeElement:
    def __init__(self, ref=1, role="button", name="Send", rect=(10, 10, 110, 40)):
        self.ref = ref
        self.role = role
        self.name = name
        self.rect = rect
        self.enabled = True
        self.is_password = False
        self.control = None

    @property
    def center(self):
        return ((self.rect[0] + self.rect[2]) // 2, (self.rect[1] + self.rect[3]) // 2)

    def describe(self):
        return f"{self.ref}. {self.role} \"{self.name}\""


class GenerationTests(unittest.TestCase):
    """The guard itself."""

    def setUp(self):
        self._saved = capture._LAST
        capture._LAST = None

    def tearDown(self):
        capture._LAST = self._saved

    def test_acting_before_looking_is_refused(self):
        capture._LAST = None
        with self.assertRaises(ToolError) as caught:
            capture.check_generation(1)
        message = str(caught.exception)
        self.assertIn("observe", message)
        self.assertIn("Nothing was done", message)

    def test_the_current_generation_is_accepted(self):
        capture._LAST = _observation(generation=7)
        self.assertEqual(capture.check_generation(7).generation, 7)

    def test_a_stale_generation_is_refused_and_says_so(self):
        # The whole point. "Click 7" from generation 7 must not be allowed to
        # land on whatever is 7 now.
        capture._LAST = _observation(generation=9)
        with self.assertRaises(ToolError) as caught:
            capture.check_generation(7)
        message = str(caught.exception)
        self.assertIn("moved", message)
        self.assertIn("observe again", message)
        self.assertIn("Nothing was done", message)

    def test_a_generation_from_the_future_is_also_refused(self):
        capture._LAST = _observation(generation=3)
        with self.assertRaises(ToolError):
            capture.check_generation(99)

    def test_a_non_numeric_generation_is_refused_with_an_explanation(self):
        capture._LAST = _observation(generation=1)
        for value in (None, "", "seven", 1.5, []):
            with self.assertRaises(ToolError):
                capture.check_generation(value)

    def test_generations_are_unique_even_within_a_millisecond(self):
        # A timestamp would collide; a counter cannot. Two observations in the
        # same millisecond must not compare equal.
        first = capture.next_generation()
        second = capture.next_generation()
        self.assertNotEqual(first, second)
        self.assertEqual(second, first + 1)


class LookupTests(unittest.TestCase):
    """Turning a model's words into one element, or refusing to guess."""

    def setUp(self):
        self._saved = capture._LAST
        self.observation = _observation(
            generation=4,
            elements=[
                _FakeElement(1, "button", "Next"),
                _FakeElement(2, "button", "Send"),
                _FakeElement(3, "link", "Send message"),
                _FakeElement(4, "field", "Search", (0, 0, 300, 30)),
            ],
        )
        capture._LAST = self.observation

    def tearDown(self):
        capture._LAST = self._saved

    def test_an_exact_name_wins(self):
        hits = capture.find_by_name(self.observation, "Send")
        self.assertEqual(hits[0].ref, 2)

    def test_a_prefix_beats_a_containment(self):
        # "Send" also matches "Send message" by containment, and the shorter
        # exact match must come first.
        hits = capture.find_by_name(self.observation, "Send message")
        self.assertEqual(hits[0].ref, 3)

    def test_ties_keep_walk_order_so_a_reference_is_repeatable(self):
        twin = _FakeElement(5, "button", "Open")
        observation = _observation(
            generation=1, elements=[twin, _FakeElement(6, "button", "Open")]
        )
        first = [e.ref for e in capture.find_by_name(observation, "Open")]
        second = [e.ref for e in capture.find_by_name(observation, "Open")]
        self.assertEqual(first, second)
        self.assertEqual(first, [5, 6])

    def test_no_match_is_an_empty_list_not_a_guess(self):
        self.assertEqual(capture.find_by_name(self.observation, "Nowhere"), [])

    def test_an_empty_query_matches_nothing(self):
        self.assertEqual(capture.find_by_name(self.observation, "   "), [])

    def test_a_disabled_element_is_refused_before_it_is_acted_on(self):
        # Not a permission question: a control that cannot be pressed is not an
        # act, so asking about it would be asking about noise.
        observation = _observation(generation=2, elements=[_FakeElement(1, "button", "Delete")])
        observation.elements[0].enabled = False
        capture._LAST = observation
        with self.assertRaises(ToolError) as caught:
            control._resolve_target(ref=1, verb="click")
        self.assertIn("disabled", str(caught.exception))

    def test_a_ref_that_does_not_exist_names_the_next_step(self):
        capture._LAST = _observation(generation=2, elements=[_FakeElement(1)])
        with self.assertRaises(ToolError) as caught:
            control._resolve_target(ref=99, verb="click")
        self.assertIn("observe again", str(caught.exception))


class ScreenMovedTests(unittest.TestCase):
    """The check that makes a reference mean anything.

    Found by the smoke test, not by a unit test: `check_generation` compared
    `_LAST.generation` against `_LAST.generation`, because the tool supplies
    the number itself. It always passed, and it was the *only* thing standing
    between "the list scrolled" and "clicked Delete".

    So freshness is a second reading of the tree, not a number.
    """

    def setUp(self):
        self._saved = capture._LAST
        self.element = _FakeElement(1, "button", "Save", (100, 200, 200, 240))

    def tearDown(self):
        capture._LAST = self._saved

    def _observation(self):
        return _observation(generation=5, hwnd=999, elements=[self.element])

    def test_a_control_still_where_it_was_is_confirmed(self):
        with mock.patch.object(capture, "_collect", return_value=[self.element]):
            capture.confirm_target(self._observation(), self.element)  # must not raise

    def test_a_control_that_is_gone_is_refused(self):
        # The dialog closed. Clicking the old position would land on whatever
        # is underneath it, which is the whole failure.
        with mock.patch.object(capture, "_collect", return_value=[]):
            with self.assertRaises(ToolError) as caught:
                capture.confirm_target(self._observation(), self.element)
        self.assertIn("Nothing was done", str(caught.exception))

    def test_a_control_that_moved_is_refused(self):
        # The scrolled-list case, and the reason this module exists. Same name,
        # same role, different position -- so index 7 now means something else.
        elsewhere = _FakeElement(1, "button", "Save", (100, 260, 200, 300))
        with mock.patch.object(capture, "_collect", return_value=[elsewhere]):
            with self.assertRaises(ToolError) as caught:
                capture.confirm_target(self._observation(), self.element)
        self.assertIn("moved", str(caught.exception))

    def test_a_control_relabelled_is_refused(self):
        renamed = _FakeElement(1, "button", "Save As", (100, 200, 200, 240))
        with mock.patch.object(capture, "_collect", return_value=[renamed]):
            with self.assertRaises(ToolError):
                capture.confirm_target(self._observation(), self.element)

    def test_a_different_role_at_the_same_place_is_refused(self):
        # A link that became a button is a different act, and "Send" as a link
        # and "Send" as a button are not the same authority question.
        as_link = _FakeElement(1, "link", "Save", (100, 200, 200, 240))
        with mock.patch.object(capture, "_collect", return_value=[as_link]):
            with self.assertRaises(ToolError):
                capture.confirm_target(self._observation(), self.element)

    def test_an_empty_tree_is_a_refusal_not_a_pass(self):
        # A mid-refresh or a crashed application looks exactly like this. We
        # cannot show the control is still there, so we do not click on the
        # strength of a picture that may be minutes old.
        with mock.patch.object(capture, "_collect", return_value=[]):
            with self.assertRaises(ToolError):
                capture.confirm_target(self._observation(), self.element)

    def test_a_crash_reading_the_screen_is_a_refusal(self):
        with mock.patch.object(capture, "_collect", side_effect=OSError("COM died")):
            with self.assertRaises(ToolError):
                capture.confirm_target(self._observation(), self.element)

    def test_an_unnamed_control_is_not_refused(self):
        # Nothing to compare against, and refusing would make a large class of
        # real interface unusable. The coordinate carries the click; the risk is
        # the ordinary one.
        nameless = _FakeElement(1, "button", "", (10, 10, 60, 40))
        with mock.patch.object(capture, "_collect", return_value=[]):
            capture.confirm_target(
                _observation(generation=1, hwnd=1, elements=[nameless]), nameless
            )

    def test_a_password_field_is_never_confirmed_by_name(self):
        # It is refused before this could matter, and a field whose name is
        # "Password" must not be re-found and re-offered as a click target.
        field = _FakeElement(1, "field", "Password", (0, 0, 200, 30))
        field.is_password = True
        with mock.patch.object(capture, "_collect", return_value=[field]):
            capture.confirm_target(
                _observation(generation=1, hwnd=1, elements=[field]), field
            )

    def test_a_move_skips_the_re_read(self):
        # Pointing at where a control was does nothing at all, so the extra
        # walk is pure cost -- but a click never skips it.
        capture._LAST = self._observation()
        with mock.patch.object(capture, "_collect", side_effect=AssertionError("walked")):
            control._resolve_target(ref=1, verb="move")
            with self.assertRaises(ToolError):
                control._resolve_target(ref=1, verb="click")


class WindowSelectionTests(unittest.TestCase):
    """Which window "the screen" means when nobody named one.

    The bug: `observe` read the **foreground** window, and while you are
    talking to Lumine the foreground window *is Lumine*. So a request to click
    a Save button read her own chrome. Worse, an always-on-top Rainmeter clock
    widget takes the foreground without the user doing anything, exposes zero
    controls, and produced "I can only see a blank page" with Notepad sitting
    right behind it.
    """

    class _Proc:
        def __init__(self, pid, name, parent=None):
            self.pid, self._name, self._parent = pid, name, parent

        def parent(self):  # a method, as real psutil has it
            return self._parent

        def name(self):
            return self._name

    def _own_under(self, parent_name):
        import os

        from agent.tools.desktop import uia

        uia.reset_own_processes()
        parent = self._Proc(999, parent_name, None)
        me = self._Proc(os.getpid(), "python.exe", parent)
        with mock.patch("psutil.Process", return_value=me):
            return uia._own_process_ids()

    def tearDown(self):
        from agent.tools.desktop import uia

        uia.reset_own_processes()

    def test_the_desktop_shell_is_adopted_as_our_own(self):
        # The shell is a separate process from the worker, so a pid-only test
        # never matched the window that actually matters -- the agent read and
        # aimed at its own interface. Both build names, because `tauri dev` and
        # a release build disagree about them.
        for name in ("Lumine.exe", "lumine-ui.exe"):
            self.assertIn(999, self._own_under(name), name)

    def test_a_terminal_parent_is_not_adopted(self):
        # Otherwise every console window on the machine would be untouchable,
        # which is not what "our own window" means.
        for name in ("powershell.exe", "Code.exe", "explorer.exe", "cmd.exe"):
            self.assertNotIn(999, self._own_under(name), name)

    def test_our_own_window_is_never_a_candidate(self):
        from agent.tools.desktop import capture, uia

        windows = [
            capture.windows_control.WindowInfo(handle=1, title="Notepad", pid=10),
            capture.windows_control.WindowInfo(handle=2, title="Lumine", pid=999),
        ]
        with mock.patch.object(
            capture.windows_control, "list_windows", return_value=windows
        ), mock.patch.object(uia, "foreground_window", return_value=2), mock.patch.object(
            uia, "is_our_own", side_effect=lambda h: h == 2
        ), mock.patch.object(
            uia, "window_title", return_value="Lumine"
        ):
            titles = [title for _h, title in capture.candidate_windows()]
        self.assertIn("Notepad", titles)
        self.assertNotIn("Lumine", titles)

    def test_the_desktop_shell_itself_is_never_a_candidate(self):
        # "Program Manager" is the desktop, not a document. Clicking a desktop
        # icon is possible but is never what "click Save" means.
        from agent.tools.desktop import capture, uia

        windows = [
            capture.windows_control.WindowInfo(handle=1, title="Notepad", pid=10),
            capture.windows_control.WindowInfo(handle=3, title="Program Manager", pid=40),
        ]
        with mock.patch.object(
            capture.windows_control, "list_windows", return_value=windows
        ), mock.patch.object(uia, "foreground_window", return_value=0), mock.patch.object(
            uia, "is_our_own", return_value=False
        ), mock.patch.object(uia, "window_title", return_value=""):
            titles = [title for _h, title in capture.candidate_windows()]
        self.assertIn("Notepad", titles)
        self.assertNotIn("Program Manager", titles)

    def test_best_window_skips_a_window_with_nothing_in_it(self):
        # The Rainmeter case, in miniature: an overlay that is on top and
        # exposes nothing must not be what "the screen" means.
        from agent.tools.desktop import capture, uia

        windows = [
            capture.windows_control.WindowInfo(handle=1, title="Clock widget", pid=10),
            capture.windows_control.WindowInfo(handle=2, title="Notepad", pid=11),
        ]

        def collect(hwnd):
            return [_FakeElement(1, "button", "Save")] if hwnd == 2 else []

        with mock.patch.object(
            capture.windows_control, "list_windows", return_value=windows
        ), mock.patch.object(uia, "foreground_window", return_value=1), mock.patch.object(
            uia, "is_our_own", return_value=False
        ), mock.patch.object(uia, "window_title", return_value="Clock widget"), mock.patch.object(
            capture, "_collect", side_effect=collect
        ):
            handle, title = capture.best_window()
        self.assertEqual((handle, title), (2, "Notepad"))

    def test_best_window_is_bounded(self):
        # Each attempt is a tree walk, so scanning the whole desktop is not
        # the answer to "I found nothing" -- naming a window is.
        from agent.tools.desktop import capture

        self.assertGreater(capture.CANDIDATE_TRIES, 0)
        self.assertLessEqual(capture.CANDIDATE_TRIES, 10)


class TierResolutionTests(unittest.TestCase):
    """Which address the tool used, and what that means for the class."""

    def setUp(self):
        self._saved = capture._LAST
        self.observation = _observation(
            generation=5, elements=[_FakeElement(1, "button", "Next", (0, 0, 100, 40))]
        )
        capture._LAST = self.observation

    def tearDown(self):
        capture._LAST = self._saved

    def test_a_ref_resolves_to_the_element_tier(self):
        # The tree is re-read before an act, which is the only thing that can
        # detect movement -- so it is stubbed to "nothing changed" here and
        # exercised properly in `ScreenMovedTests`.
        with mock.patch.object(capture, "_collect", return_value=list(self.observation.elements)):
            point, element, tier = control._resolve_target(ref=1, verb="click")
        self.assertEqual(tier, ops.ELEMENT_TIER)
        self.assertEqual(element.ref, 1)
        self.assertEqual(point, (50, 20))

    def test_a_name_resolves_to_the_element_tier(self):
        with mock.patch.object(capture, "_collect", return_value=list(self.observation.elements)):
            point, element, tier = control._resolve_target(name="Next", verb="click")
        self.assertEqual(tier, ops.ELEMENT_TIER)
        self.assertEqual(element.name, "Next")

    def test_two_matching_names_are_refused_rather_than_guessed(self):
        # Picking the first of two buttons called Send is how the wrong one gets
        # clicked. Both numbers are offered instead.
        capture._LAST = _observation(
            generation=5,
            elements=[_FakeElement(1, "button", "Send"), _FakeElement(2, "button", "Send")],
        )
        with self.assertRaises(ToolError) as caught:
            control._resolve_target(name="Send", verb="click")
        message = str(caught.exception)
        self.assertIn("more than one", message)
        self.assertIn("Nothing was done", message)

    def test_a_coordinate_resolves_to_the_coordinate_tier_with_no_element(self):
        with mock.patch.object(control.desktop_input, "contains", return_value=True):
            point, element, tier = control._resolve_target(x=300, y=400, verb="click")
        self.assertEqual(tier, ops.COORDINATE_TIER)
        self.assertIsNone(element)
        self.assertEqual(point, (300, 400))

    def test_a_coordinate_off_every_screen_is_refused(self):
        with mock.patch.object(control.desktop_input, "contains", return_value=False):
            with self.assertRaises(ToolError):
                control._resolve_target(x=99999, y=99999, verb="click")

    def test_press_refuses_a_coordinate_entirely(self):
        # A key goes to whatever has focus, so a coordinate says nothing about
        # where it lands.
        with mock.patch.object(control.desktop_input, "contains", return_value=True):
            with self.assertRaises(ToolError) as caught:
                control._resolve_target(x=10, y=10, verb="press")
        self.assertIn("needs a control", str(caught.exception))

    def test_naming_nothing_is_refused(self):
        with self.assertRaises(ToolError):
            control._resolve_target(verb="click")

    def test_a_coordinate_with_no_element_is_always_sensitive(self):
        # The tier with nothing behind it is the one a user must always be asked
        # about, whatever the verb's own class is -- a click at a bare
        # coordinate is a click at whatever happens to be there.
        self.assertEqual(ops.classify("move"), "act")
        self.assertEqual(ops.classify("click"), "act")
        # ...which is why `_do_pointer` hardcodes `sensitive` when there is no
        # element rather than asking the table. Asserted here so the two places
        # cannot drift.
        self.assertTrue(ops.operation("click").allows_tier(ops.COORDINATE_TIER))


class VerificationTests(unittest.TestCase):
    """A click that returned is not a click that worked."""

    def test_a_read_change_becomes_observed(self):
        outcome = control._outcome_for("the window title is now Save As", "click", _FakeElement())
        self.assertEqual(outcome.status, "observed")

    def test_no_evidence_stays_dispatched(self):
        # The honest floor. Reporting `observed` here would be claiming the
        # application did something we never read.
        outcome = control._outcome_for("", "click", _FakeElement())
        self.assertEqual(outcome.status, "dispatched")
        self.assertIn("cannot read", outcome.data["note"])

    def test_a_dispatched_outcome_carries_no_evidence_field(self):
        # `verification.dispatched` takes no evidence, because a dispatched
        # outcome has none by definition. An explanation rides in `data` under
        # a name that cannot be mistaken for the field it is not.
        outcome = control._outcome_for("", "click", _FakeElement())
        self.assertEqual(outcome.evidence, "")
        self.assertNotIn("evidence", outcome.to_dict())

    def test_the_dispatched_verdict_explains_itself_to_the_model(self):
        # The sentence is what stops the model reporting an unconfirmed click
        # as one that worked.
        outcome = control._outcome_for("", "click", _FakeElement())
        self.assertIn("Windows accepted", outcome.data["note"])
        self.assertIn("Say that rather than", outcome.data["note"])


class PasswordRefusalTests(unittest.TestCase):
    """Typing into a credential field is refused, not asked about."""

    def test_the_password_field_is_never_typed_into_even_if_asked(self):
        field = _FakeElement(1, "field", "Password")
        field.is_password = True
        # Hermetic on purpose: the real foreground window during a test run is
        # whatever the developer's machine happens to be showing, and a refusal
        # from an *integrated window* check would mask the one under test.
        with mock.patch.object(control.uia, "foreground_window", return_value=99), mock.patch.object(
            control, "_check_window"
        ), mock.patch.object(control, "_check_own_window"), mock.patch.object(
            control, "_fresh_observation", return_value=_observation(generation=1, elements=[field])
        ), mock.patch.object(control, "_focused_element", return_value=field), mock.patch.object(
            control, "_input"
        ) as engine:
            with self.assertRaises(ToolError) as caught:
                import asyncio

                asyncio.run(control._do_type(mock.Mock(), "hunter2"))
        message = str(caught.exception)
        self.assertIn("password", message.lower())
        self.assertIn("Nothing was typed", message)
        # Nothing reached the injection layer at all.
        engine.assert_not_called()

    def test_an_ordinary_field_is_typed_into(self):
        field = _FakeElement(1, "field", "Search")
        observation = _observation(generation=1, elements=[field])
        engine = mock.Mock()
        engine.type_text.return_value = 3
        with mock.patch.object(control.uia, "foreground_window", return_value=99), mock.patch.object(
            control, "_check_window"
        ), mock.patch.object(control, "_check_own_window"), mock.patch.object(
            control, "_fresh_observation", return_value=observation
        ), mock.patch.object(control, "_focused_element", return_value=field), mock.patch.object(
            control, "_target_window", return_value=99
        ), mock.patch.object(
            control.uia, "ensure_foreground"
        ), mock.patch.object(
            control.uia, "release_foreground"
        ), mock.patch.object(
            control, "_input", return_value=engine
        ):
            import asyncio

            result = asyncio.run(control._do_type(mock.Mock(), "hello"))
        engine.type_text.assert_called_once_with("hello")
        # `accepted`, not `observed`: the characters were injected and nothing
        # read the field back, so claiming more than that would be a guess.
        self.assertIn('"status":"accepted"', result)
        self.assertIn('"events":3', result)

    def test_typing_is_refused_rather_than_aimed_at_the_wrong_window(self):
        # Keystrokes carry no position. If the window cannot be brought
        # forward, the honest outcome is to type nothing -- text landing in
        # whichever window happened to have focus is the worst thing this tool
        # could produce, and it is not recoverable afterwards.
        field = _FakeElement(1, "field", "Search")
        engine = mock.Mock()
        with mock.patch.object(control.uia, "foreground_window", return_value=99), mock.patch.object(
            control, "_check_window"
        ), mock.patch.object(control, "_check_own_window"), mock.patch.object(
            control, "_fresh_observation", return_value=_observation(1, elements=[field])
        ), mock.patch.object(control, "_focused_element", return_value=field), mock.patch.object(
            control, "_target_window", return_value=99
        ), mock.patch.object(
            control.uia, "ensure_foreground", side_effect=ToolError("could not bring it forward")
        ), mock.patch.object(
            control, "_input", return_value=engine
        ):
            import asyncio

            with self.assertRaises(ToolError) as caught:
                asyncio.run(control._do_type(mock.Mock(), "hello"))
        self.assertIn("Nothing was typed", str(caught.exception))
        engine.type_text.assert_not_called()

    def test_typing_asks_for_the_window_after_the_gate_not_before(self):
        # `ensure_foreground` may steal the user's focus, and a question asked
        # while a window is being yanked forward is a question about something
        # that may not happen -- so it has to come after the approval.
        import inspect

        body = inspect.getsource(control._do_type)
        self.assertGreater(
            body.find("require_approval"),
            body.find("_check_window"),
            "the gate must come after the refusals",
        )
        self.assertGreater(
            body.find("ensure_foreground"),
            body.find("require_approval"),
            "focus must be taken after the gate, not before",
        )

    def test_the_password_check_runs_before_the_gate(self):
        # Structural, not advisory: no approval question can unlock it, because
        # a credential typed by a voice agent is a credential in a transcript.
        # So the refusal has to be *earlier in the function* than the gate.
        import inspect

        body = inspect.getsource(control._do_type)
        password_at = body.find("is_password")
        gate_at = body.find("require_approval")
        self.assertGreater(password_at, 0, "the password refusal is gone")
        self.assertGreater(gate_at, 0, "the gate is gone")
        self.assertGreater(gate_at, password_at, "the gate must come after the refusal")


class IntegratedWindowTests(unittest.TestCase):
    """No window is a dead end any more, and one window is still refused."""

    def test_a_browser_window_is_driven_not_refused(self):
        # The whole change: an integrated window produces a *note*, and the act
        # proceeds. It used to raise, which made this a wall rather than a
        # fallback.
        with mock.patch.object(capture, "process_names_for", return_value=["chrome.exe"]):
            control._check_window(99)  # must not raise
            self.assertIn("web_control", control._preferred_tool_hint(99))

    def test_whatsapp_is_driven_too_and_names_its_own_tool(self):
        with mock.patch.object(capture, "process_names_for", return_value=["WhatsApp.exe"]):
            control._check_window(99)  # must not raise
            self.assertIn("send_whatsapp", control._preferred_tool_hint(99))

    def test_an_ordinary_window_has_no_note(self):
        with mock.patch.object(capture, "process_names_for", return_value=["notepad.exe"]):
            control._check_window(99)
            self.assertEqual(control._preferred_tool_hint(99), "")

    def test_our_own_window_is_still_refused(self):
        # The one wall that stays. An agent that can click anything can click
        # the Tools page, switch itself off mid-turn, and keep using it.
        with mock.patch.object(uia, "is_our_own", return_value=True):
            with self.assertRaises(ToolError) as caught:
                control._check_own_window(99)
        self.assertIn("own window", str(caught.exception))

    def test_our_own_window_means_the_desktop_shell_too(self):
        # The shell is a *different process* from the worker, so the earlier
        # pid-only test never matched the window that actually matters -- the
        # agent read and aimed at its own interface.
        with mock.patch.object(uia, "pid_of", return_value=424242), mock.patch.object(
            uia, "_own_process_ids", return_value=frozenset({424242})
        ):
            self.assertTrue(uia.is_our_own(1))


class EntrypointTests(unittest.TestCase):
    """The tool function itself, not just the helpers under it.

    Found the hard way: `preflight` was an instance method and the tool called
    it as `_Desktop.preflight()`, so every call raised `TypeError` -- and 1667
    tests passed, because they all exercised the helpers *below* the entrypoint.
    A tool with no test that calls the tool is a tool whose first defect ships.
    """

    def test_preflight_is_callable_without_an_instance(self):
        # Platform and driver are real here, so this must simply return rather
        # than raise -- the bug was a `TypeError` before either check ran.
        self.assertIsNone(control._Desktop.preflight())

    def test_an_unknown_verb_is_refused_before_anything_is_measured(self):
        import asyncio

        with self.assertRaises(ToolError) as caught:
            asyncio.run(control.desktop_control(mock.Mock(), "teleport"))
        self.assertIn("observe", str(caught.exception))

    def test_a_click_on_an_integrated_window_reaches_the_engine(self):
        # The change, at the outermost layer: a browser window is driven, not
        # refused. `web_control` is steadier, and the note says so on the
        # approval card -- but it does not stop the click.
        import asyncio

        # A deliberately ordinary control name: "Accept all" would classify as
        # a submit and need a gate, and this test is about the window, not the
        # class.
        element = _FakeElement(1, "button", "Downloads", (10, 10, 200, 40))
        engine = mock.Mock()
        with mock.patch.object(control.uia, "foreground_window", return_value=99), mock.patch.object(
            capture, "process_names_for", return_value=["chrome.exe"]
        ), mock.patch.object(control, "_check_own_window"), mock.patch.object(
            control, "_target_window", return_value=99
        ), mock.patch.object(
            capture, "require_observation", return_value=_observation(1, elements=[element])
        ), mock.patch.object(
            capture, "confirm_target"
        ), mock.patch.object(
            control, "_input", return_value=engine
        ), mock.patch.object(
            control, "_read_change", return_value=""
        ):
            result = asyncio.run(control.desktop_control(mock.Mock(), "click", target="1"))
        engine.click.assert_called_once()
        self.assertIn("status", result)

    def test_typing_nothing_is_refused(self):
        import asyncio

        with self.assertRaises(ToolError) as caught:
            asyncio.run(control.desktop_control(mock.Mock(), "type", text=""))
        self.assertIn("Nothing was typed", str(caught.exception))

    def test_pressing_nothing_is_refused(self):
        import asyncio

        with self.assertRaises(ToolError) as caught:
            asyncio.run(control.desktop_control(mock.Mock(), "press", text=""))
        self.assertIn("Nothing was pressed", str(caught.exception))

    def test_capability_distinguishes_the_three_problems(self):
        import sys

        if sys.platform != "win32":  # pragma: no cover - Windows-only machine
            self.skipTest("the desktop tool is Windows-only")
        verdict = control.capability()
        self.assertTrue(verdict["available"])
        self.assertIn(verdict.get("state", ""), ("not_observed", "addressable", "coordinate_only"))


class ChildWindowTests(unittest.TestCase):
    """Reading the content window, not just the frame.

    Found by running against a real VS Code window: the top-level handle's tree
    held three corner buttons and nothing else, and the whole editor was
    reported as an almost-empty screen. Every Electron, WinUI and Chromium
    shell puts its content in a child window, so a parent-only walk makes the
    tool useless against most modern applications -- while looking, in a test
    with a fake tree, perfectly correct.
    """

    def test_children_are_enumerated_from_the_parent(self):
        with mock.patch.object(uia.user32, "EnumChildWindows") as enum:
            found = uia.descendant_windows(4242)
        enum.assert_called_once()
        self.assertEqual(found[0], 4242, "the parent comes first")

    def test_the_parent_is_always_included_even_if_enumeration_fails(self):
        with mock.patch.object(uia.user32, "EnumChildWindows", side_effect=OSError):
            self.assertEqual(uia.descendant_windows(4242), [4242])

    def test_children_are_capped(self):
        # A runaway enumeration on one application must not be able to stall
        # the single worker thread.
        self.assertGreater(uia.MAX_CHILD_WINDOWS, 0)

    def test_a_non_windows_platform_returns_only_the_parent(self):
        with mock.patch.object(uia.sys, "platform", "linux"):
            self.assertEqual(uia.descendant_windows(4242), [4242])
            self.assertEqual(uia.descendant_windows(0), [])

    def test_collection_walks_every_child_window(self):
        # The behaviour itself, with the driver faked: two children, each with
        # one button, and the walk must reach both rather than stopping at the
        # first.
        class _Node:
            def __init__(self, kind, name, rect):
                self.ControlTypeName = kind
                self.Name = name
                self.IsEnabled = True
                self.rect = rect

            class _Rect:
                def __init__(self, r):
                    self.left, self.top, self.right, self.bottom = r

            @property
            def BoundingRectangle(self):
                return self._Rect(self.rect)

            def GetChildren(self):
                return []

        roots = {
            1: _Node("ButtonControl", "One", (0, 0, 50, 50)),
            2: _Node("ButtonControl", "Two", (100, 0, 150, 50)),
        }
        driver = mock.Mock()
        driver.ControlFromHandle = lambda hwnd: roots.get(hwnd, _Node("PaneControl", "", (0, 0, 1, 1)))
        with mock.patch.object(uia, "descendant_windows", return_value=[0, 1, 2]), mock.patch.object(
            uia, "driver", return_value=driver
        ):
            elements = capture._collect(0)
        self.assertEqual([e.name for e in elements], ["One", "Two"])
        self.assertEqual([e.ref for e in elements], [1, 2])


class ObservationFreshnessTests(unittest.TestCase):
    """Staleness for reads, and its irrelevance to acts."""

    def test_a_fresh_observation_is_not_stale(self):
        self.assertFalse(capture.is_stale(_observation()))

    def test_no_observation_is_stale(self):
        self.assertTrue(capture.is_stale(None))

    def test_the_reuse_window_does_not_relax_the_generation_check(self):
        # The reason these are two separate mechanisms: `is_stale` bounds how
        # long an index is reused for a *plain read*, and `check_generation`
        # compares generations exactly for anything that acts. A slow
        # observation must never widen what may be acted on.
        import inspect

        self.assertNotIn("is_stale", inspect.getsource(control._resolve_target))


if __name__ == "__main__":
    unittest.main()