"""The browsing tool: what it refuses, what it asks, and what it never types.

Five claims, in order because each depends on the last:

1. **Only public web pages.** A URL is normalized to http(s) with no
   credentials in it; localhost and private-network addresses are refused --
   the pivot that lets a hostile page aim the assistant's browser at the
   user's router -- and the route guard applies the *same* verdict to the
   requests a page makes itself, not only to the navigation we asked for.
2. **The page cannot choose its own permission class.** Classification reads
   DOM facts (tag, type, form membership), never labels; a form submission
   is always sensitive; and with no approval channel a submit refuses with
   the standard sentence while the click is never attempted.
3. **Passwords are refused, not asked about.** Before any question, and
   again inside the DOM at the moment of typing, so a page that swaps
   elements under a reference cannot collect a keystroke.
4. **A stale reference fails closed.** Refused shapes and vanished elements
   are errors that did nothing, never a click at whatever is there now.
5. **A refused argument never moves the page.** A key outside the closed
   list, or a direction that does not exist, is an error raised before the
   browser is touched -- never the nearest available thing done instead.

Everything runs against fakes: no test here starts a browser, resolves a
name over the network, or opens a window. The one real gate exercised here
is the gate with nobody in the room -- the case that has to refuse.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent.runtime import audit_log
from agent.runtime.permission_gate import MSG_NO_GATE
from agent.tools.browser import actions, session
from agent.tools.browser import tools as browse_module
from agent.tools.browser.tools import browse
from agent.tools.tools_compat import ToolError
from agent.tools.tool_results import MAX_PAGE_READ_CHARS, UNTRUSTED_OPEN
from agent.tools.tools_permission import TOOL_ACTION_CLASS, permission_floor


class AuditRedirect(unittest.TestCase):
    """A refusal records an audit line; a test must not use the real file."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ,
            {audit_log.AUDIT_PATH_ENV: str(Path(tmp.name) / "audit.jsonl")},
        )
        patcher.start()
        self.addCleanup(patcher.stop)


def run_browse(action: str, **kwargs) -> str:
    """Drive the tool synchronously against a session-less context."""
    return asyncio.run(browse(SimpleNamespace(), action, **kwargs))


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


class NormalizeUrlTests(unittest.TestCase):
    def test_a_bare_address_gains_https(self):
        self.assertEqual(actions.normalize_url("example.com"), "https://example.com")
        self.assertEqual(actions.normalize_url("example.com/docs"), "https://example.com/docs")

    def test_case_and_port_survive_as_the_browser_sees_them(self):
        self.assertEqual(
            actions.normalize_url("HTTPS://Example.COM:8443/Path?q=1#top"),
            "https://example.com:8443/Path?q=1#top",
        )

    def test_a_url_that_carries_a_secret_refuses_itself(self):
        # user:pass@host would be echoed into the approval card, the audit
        # digest, and the transcript. Not ours to repeat back.
        for raw in ("user:pass@example.com", "https://user:pass@example.com/x"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError, "credentials"):
                    actions.normalize_url(raw)

    def test_only_web_schemes_survive(self):
        with self.assertRaisesRegex(ValueError, "scheme"):
            actions.normalize_url("file:///etc/passwd")
        with self.assertRaisesRegex(ValueError, "scheme"):
            actions.normalize_url("data:text/html,<script>x</script>")
        # A scheme without // does not look like a scheme at all; the point
        # is that it raises rather than being opened somehow.
        with self.assertRaises(ValueError):
            actions.normalize_url("javascript:alert(1)")

    def test_nonsense_is_refused_with_a_reason(self):
        for raw, code in (
            ("", "empty"),
            ("   ", "empty"),
            ("https://exa mple.com", "whitespace"),
            ("https://", "no host"),
            ("https://example.com:abc/", "unparsable"),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    actions.normalize_url(raw)
                self.assertEqual(str(caught.exception), code)

    def test_hostname_of_is_the_lowercase_host(self):
        self.assertEqual(actions.hostname_of("https://Example.com:8443/x"), "example.com")
        self.assertEqual(actions.hostname_of("not a url"), "")


class HostVerdictTests(unittest.TestCase):
    def test_localhost_and_its_relatives_are_local(self):
        for host in ("localhost", "router.local", "svc.internal", "x.localhost"):
            with self.subTest(host=host):
                ok, why = actions.host_verdict(host, [])
                self.assertFalse(ok)
                self.assertEqual(why, "it is a local name")

    def test_private_and_link_local_addresses_are_not_public(self):
        for host in ("192.168.1.5", "10.0.0.1", "172.16.0.1", "127.0.0.1", "169.254.1.1", "::1"):
            with self.subTest(host=host):
                ok, why = actions.host_verdict(host, [])
                self.assertFalse(ok)
                self.assertIn("not a public address", why)

    def test_a_public_literal_address_is_allowed_without_a_lookup(self):
        ok, _why = actions.host_verdict("8.8.8.8", [])
        self.assertTrue(ok)

    def test_a_hostname_speaks_through_its_addresses(self):
        ok, _why = actions.host_verdict("dns.google", ["8.8.8.8"])
        self.assertTrue(ok)
        # One private address among public ones is enough: the browser must
        # not fetch a name that resolves inward, however it resolves.
        ok, why = actions.host_verdict("weird.example", ["8.8.8.8", "10.0.0.1"])
        self.assertFalse(ok)
        self.assertIn("not a public address", why)

    def test_a_hostname_that_could_not_be_resolved_is_refused(self):
        ok, why = actions.host_verdict("nope.example", [])
        self.assertFalse(ok)
        self.assertEqual(why, "it could not be resolved")

    def test_an_empty_host_is_refused(self):
        ok, _why = actions.host_verdict("", [])
        self.assertFalse(ok)

    def test_the_documented_override_opens_the_local_network(self):
        # For the person who wants their assistant on the NAS. Off by
        # default; asserted here so the switch cannot quietly vanish.
        ok, _why = actions.host_verdict("router.local", [], allow_local=True)
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# Clicks and fills
# ---------------------------------------------------------------------------


class ClassifyClickTests(unittest.TestCase):
    def test_a_form_submitter_is_always_sensitive(self):
        self.assertEqual(
            actions.classify_click(tag="input", type_="submit", in_form=True), "sensitive"
        )
        self.assertEqual(
            actions.classify_click(tag="input", type_="image", in_form=True), "sensitive"
        )
        # The HTML rule: a <button> inside a form submits unless it says
        # otherwise, and "otherwise" is an explicit type.
        self.assertEqual(
            actions.classify_click(tag="button", type_="", in_form=True), "sensitive"
        )
        self.assertEqual(
            actions.classify_click(tag="button", type_="submit", in_form=True), "sensitive"
        )

    def test_an_ordinary_control_is_an_act(self):
        self.assertEqual(
            actions.classify_click(tag="button", type_="button", in_form=True), "act"
        )
        self.assertEqual(
            actions.classify_click(tag="button", type_="", in_form=False), "act"
        )
        self.assertEqual(actions.classify_click(tag="input", type_="text", in_form=True), "act")
        self.assertEqual(actions.classify_click(tag="a", type_="", in_form=False), "act")

    def test_an_unrecognised_control_claiming_to_submit_is_asked_about(self):
        # The only wrong direction is away from asking, so an odd tag with a
        # submit type gets the question rather than the pass.
        self.assertEqual(
            actions.classify_click(tag="div", type_="submit", in_form=False), "sensitive"
        )


class NormalizeKeyTests(unittest.TestCase):
    def test_the_list_is_closed_and_speaks_the_browsers_spelling(self):
        for raw in ("enter", "ESCAPE", "ArrowDown", "pagedown", "  Tab  "):
            with self.subTest(raw=raw):
                self.assertIn(actions.normalize_key(raw), actions.KEYS)

    def test_aliases_resolve_into_the_list_rather_than_widening_it(self):
        # "esc" and "return" are what a person types; the browser wants
        # "Escape" and "Enter". The alias table maps, it never admits.
        self.assertEqual(actions.normalize_key("esc"), "Escape")
        self.assertEqual(actions.normalize_key("return"), "Enter")
        self.assertEqual(actions.normalize_key("space"), " ")
        self.assertEqual(actions.normalize_key("arrowup"), "ArrowUp")

    def test_a_key_that_would_reach_the_operating_system_is_refused(self):
        # A key is a chord with the OS: Ctrl+W closes the window, Alt+F4
        # closes the app, F5 reloads. None of them reach the DOM, so no
        # amount of inspecting DOM facts could ever classify one.
        for raw in ("Ctrl+W", "ctrl+a", "Alt+F4", "F5", "Meta+d", "", "   ", None):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    actions.normalize_key(raw)


class ClassifyKeyTests(unittest.TestCase):
    def test_enter_inside_a_form_is_a_submission_whatever_the_control_is(self):
        # HTML's implicit submission: Enter on an ordinary text field sends
        # the form. Data moving toward someone else's server is the line
        # this tool holds, and it holds it before looking at the tag.
        #
        # `search=False` throughout, and it has to be named: a *search* field
        # is the deliberate exception, and a test that left the flag at its
        # default while asserting a bare text field would pass whether or not
        # the exception existed.
        for tag, type_ in (("input", "text"), ("textarea", ""), ("input", "search")):
            with self.subTest(tag=tag, type=type_):
                self.assertEqual(
                    actions.classify_key(
                        "Enter", tag=tag, type_=type_, in_form=True, search=False
                    ),
                    "sensitive",
                )


class SearchControlTests(unittest.TestCase):
    """The one fact that lowers a class, and the boundary around it."""

    def test_a_search_form_is_recognised_by_the_form_own_role(self):
        # YouTube, Google and Amazon all ship `<form role="search">`. This is
        # the strongest signal because it describes the form rather than the
        # field, so a page cannot get it by styling one input.
        self.assertTrue(
            actions.is_search_control(
                tag="input", type_="", in_form=True, form_role="search"
            )
        )
        self.assertTrue(
            actions.is_search_control(tag="button", type_="", in_form=True, form_role="search")
        )

    def test_a_searchbox_role_is_recognised_on_its_own(self):
        # `role` is the settled answer for the *control*, so it needs no
        # corroboration from `soleField`.
        self.assertTrue(
            actions.is_search_control(tag="input", type_="", in_form=True, role="searchbox")
        )
        # A bare `type="search"` is not enough, and the reason is the third
        # signal rather than a missing rule: `type=search` on one field of a
        # larger form is a filter box, not a search form, and treating it as one
        # would hand a page a way to make an ordinary submission ask-free.
        # `roleOf` in `state` reports such a field as `searchbox` regardless,
        # which is why the role above and the type below disagree on purpose.
        self.assertFalse(actions.is_search_control(tag="input", type_="search", in_form=True))

    def test_the_search_typed_field_needs_to_be_the_only_one(self):
        # The sole-field half is what stops a page treating its comment box as
        # a search: one search box *and* a comment box is a comment form.
        self.assertTrue(
            actions.is_search_control(
                tag="input", type_="search", in_form=True, sole_field=True
            )
        )
        self.assertFalse(
            actions.is_search_control(
                tag="input", type_="search", in_form=True, sole_field=False
            ),
            "a search-typed field that is not alone in its form is not a search form",
        )

    def test_an_ordinary_text_field_in_a_form_is_not_a_search(self):
        # The negative case, and the one that matters: a comment box, a
        # checkout, a sign-in form and a contact form all look exactly like
        # this, and all of them still ask.
        self.assertFalse(actions.is_search_control(tag="input", type_="text", in_form=True))
        self.assertFalse(actions.is_search_control(tag="textarea", type_="", in_form=True))
        self.assertFalse(
            actions.is_search_control(tag="input", type_="text", in_form=True, sole_field=True),
            "the sole-field signal only qualifies a search-typed field",
        )
        self.assertFalse(actions.is_search_control(tag="input", type_="", in_form=False))

    def test_a_search_submit_is_an_act_and_not_a_read(self):
        # `act`, never `read`: the query did go to somebody's server, so this
        # is not a pretence that nothing left the machine.
        self.assertEqual(
            actions.classify_click(tag="input", type_="submit", in_form=True, search=True), "act"
        )
        self.assertEqual(
            actions.classify_click(tag="button", type_="", in_form=True, search=True), "act"
        )

    def test_enter_in_a_search_form_does_not_ask(self):
        # The reported fault: "search YouTube for X" and "press Enter on the
        # YouTube search box" both interrupted the user. Both go through here.
        self.assertEqual(
            actions.classify_key("Enter", tag="input", type_="", in_form=True, search=True), "act"
        )
        self.assertEqual(
            actions.classify_key("Enter", tag="input", type_="text", in_form=True, search=True),
            "act",
        )

    def test_a_search_form_does_not_mute_the_other_forms_on_the_page(self):
        # The class is per control, so a sign-in form beside a search box keeps
        # asking. One form being a search form is not a licence for the page.
        self.assertEqual(
            actions.classify_key(
                "Enter", tag="input", type_="password", in_form=True, search=False
            ),
            "sensitive",
        )
        self.assertEqual(
            actions.classify_key("Enter", tag="textarea", type_="", in_form=True, search=False),
            "sensitive",
        )

    def test_enter_outside_a_form_is_an_ordinary_press(self):
        # No form, nothing to submit: pressing Enter on a control is an act.
        self.assertEqual(
            actions.classify_key("Enter", tag="input", type_="text", in_form=False), "act"
        )
        self.assertEqual(
            actions.classify_key("Enter", tag="button", type_="button", in_form=False), "act"
        )

    def test_a_key_that_only_moves_focus_or_the_caret_is_an_act(self):
        for key in ("Escape", "Tab", "ArrowDown", "PageUp", "Home", "Backspace"):
            with self.subTest(key=key):
                self.assertEqual(
                    actions.classify_key(key, tag="input", type_="text", in_form=True),
                    "act",
                )

    def test_a_key_that_activates_a_submitter_is_classified_as_that_click(self):
        # Space on a submit button does what clicking it does, so it gets
        # the click's verdict -- which is the one that asks.
        self.assertEqual(
            actions.classify_key(" ", tag="button", type_="submit", in_form=True),
            "sensitive",
        )

    def test_a_space_typed_into_a_field_is_not_a_submission(self):
        # The same key, a different control: this one only adds a
        # character, and the form does not move.
        self.assertEqual(
            actions.classify_key(" ", tag="input", type_="text", in_form=True), "act"
        )


class FillDenyTests(unittest.TestCase):
    def test_password_fields_are_refused(self):
        self.assertTrue(
            actions.fill_deny_reason(tag="input", type_="password")
        )
        self.assertTrue(
            actions.fill_deny_reason(tag="INPUT", type_="PASSWORD")
        )

    def test_everything_else_may_be_filled(self):
        for tag, type_ in (("input", "text"), ("textarea", ""), ("input", "email"), ("div", "")):
            with self.subTest(tag=tag, type=type_):
                self.assertIsNone(actions.fill_deny_reason(tag=tag, type_=type_))


class ObservePayloadTests(unittest.TestCase):
    @staticmethod
    def _payload(**overrides):
        data = {
            "title": "A page",
            "url": "https://example.com/",
            "text": "the words on it",
            "controls": [{"ref": "r1", "tag": "a", "type": "", "label": "Next"}],
        }
        data.update(overrides)
        return actions.observe_payload(**data)

    def test_the_payload_is_keyed_json_with_the_reference_in_it(self):
        parsed = json.loads(self._payload())
        self.assertEqual(parsed["controls"][0]["ref"], "r1")

    def test_the_scroll_window_rides_along_when_the_page_reports_one(self):
        # The text is windowed, so its position belongs beside it -- without
        # the number "scroll down and read again" is a sentence the model
        # cannot confirm it followed.
        parsed = json.loads(self._payload(scroll={"y": 1400, "max": 4200}))
        self.assertEqual(parsed["scroll"], {"y": 1400, "max": 4200})

    def test_no_scroll_key_when_the_page_could_not_answer(self):
        # Absent rather than null: a page with no layout reports nothing, and
        # `scroll: null` reads as a position the model then reasons about.
        for empty in (None, {}, {"y": "over", "max": 4}):
            with self.subTest(scroll=empty):
                self.assertNotIn("scroll", json.loads(self._payload(scroll=empty)))

    def test_a_repeat_read_notice_rides_along_with_the_text_it_is_about(self):
        parsed = json.loads(self._payload(notice="This is the same text you just read."))
        self.assertIn("same text", parsed["notice"])

    def test_no_notice_key_when_there_is_nothing_to_say(self):
        self.assertNotIn("notice", json.loads(self._payload()))
        self.assertNotIn("notice", json.loads(self._payload(notice="   ")))

    def test_controls_are_capped_and_bad_references_are_dropped(self):
        flood = [{"ref": f"r{i}", "tag": "a", "type": "", "label": f"Link {i}"} for i in range(30)]
        flood.append({"ref": 'x"]', "tag": "a", "type": "", "label": "not a ref"})
        parsed = json.loads(self._payload(controls=flood))
        self.assertEqual(len(parsed["controls"]), actions.MAX_REPORTED_CONTROLS)
        self.assertTrue(all(item["ref"].startswith("r") for item in parsed["controls"]))

    def test_every_piece_is_clipped_before_it_meets_the_budget(self):
        payload = self._payload(
            title="T" * 500,
            url="https://example.com/" + "p" * 500,
            text="word " * 1000,
            controls=[{"ref": "r1", "tag": "input", "type": "submit", "label": "L" * 300}],
        )
        # The browser path's own ceiling (`MAX_PAGE_READ_CHARS`), not the shared
        # `MAX_RESULT_CHARS`: an observe exists to answer a question about *this*
        # page, and 800 characters of a product page is its navigation rather than
        # its price. The global number is deliberately untouched -- this budget is
        # paid per call and only by the browser paths.
        self.assertLessEqual(len(payload), MAX_PAGE_READ_CHARS)
        parsed = json.loads(payload)
        self.assertLessEqual(len(parsed["title"]), actions.TITLE_CHARS)
        self.assertLessEqual(len(parsed["text"]), actions.OBSERVE_TEXT_CHARS)
        self.assertEqual(parsed["controls"][0]["kind"], "input:submit")

    def test_a_full_result_that_exceeds_the_budget_says_it_was_cut(self):
        # A full page of max-length controls plus full text does not fit the
        # ceiling, and a model told a list is complete answers as though it
        # were -- so the cut is named, not silent.
        #
        # **And the result must still be JSON.** It used to be a rendered string
        # cut at the budget with `" (truncated)"` appended, which is not an object
        # at all: the model could not read the controls that *were* in it. The cut
        # is now taken off the list and recorded as a field.
        flood = [
            {"ref": f"r{i}", "tag": "input", "type": "submit", "label": "L" * actions.LABEL_CHARS}
            for i in range(1, actions.MAX_REPORTED_CONTROLS + 1)
        ]
        payload = self._payload(
            title="T" * actions.TITLE_CHARS,
            url="https://example.com/" + "p" * actions.URL_CHARS,
            text="word " * (actions.OBSERVE_TEXT_CHARS * 3),
            controls=flood,
        )
        self.assertLessEqual(len(payload), MAX_PAGE_READ_CHARS)
        parsed = json.loads(payload)
        self.assertTrue(parsed.get("truncated") or parsed.get("omitted"), "a cut list must say so")
        # And the part that survived is readable, which is the whole point.
        self.assertEqual(parsed["controls"][0]["label"], "L" * actions.LABEL_CHARS)


# ---------------------------------------------------------------------------
# The tool: open, observe, close
# ---------------------------------------------------------------------------


class OpenActionTests(unittest.TestCase):
    def test_an_unknown_action_is_refused_by_name(self):
        with self.assertRaisesRegex(ToolError, "open, observe"):
            run_browse("teleport")

    def test_open_without_a_url_says_which_argument_it_needed(self):
        with self.assertRaisesRegex(ToolError, "page to open"):
            run_browse("open")

    def test_open_refuses_anything_that_is_not_a_web_page(self):
        with mock.patch.object(browse_module, "open_page") as opener:
            with self.assertRaises(ToolError):
                run_browse("open", url="file:///etc/passwd")
        opener.assert_not_called()

    def test_open_is_a_read_that_normalizes_and_wraps(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module,
            "open_page",
            return_value={"title": "Example Domain", "url": "https://example.com/"},
        ) as opener:
            result = run_browse("open", url="example.com")

        opener.assert_called_once_with("https://example.com")
        self.assertEqual(approval.call_args.kwargs["action_class"], "read")
        # Wrapped: the title is text a website chose, and the model reads it.
        self.assertIn(UNTRUSTED_OPEN, result)
        self.assertIn("Example Domain", result)

    def test_a_refused_open_never_reaches_the_browser(self):
        with mock.patch.object(
            browse_module,
            "require_approval",
            new_callable=mock.AsyncMock,
            side_effect=ToolError(MSG_NO_GATE),
        ), mock.patch.object(browse_module, "open_page") as opener:
            with self.assertRaises(ToolError):
                run_browse("open", url="https://example.com")
        opener.assert_not_called()

    def test_a_browser_that_could_not_open_the_page_reports_failure(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module, "open_page", side_effect=ToolError("I couldn't open example.com: nope")
        ):
            with self.assertRaisesRegex(ToolError, "couldn't open"):
                run_browse("open", url="https://example.com")


class ObserveAndCloseTests(unittest.TestCase):
    def test_observe_wraps_page_text_as_retrieved_data(self):
        page = {
            "title": "Site",
            "url": "https://x.test/",
            "text": "words from the page",
            "controls": [{"ref": "r1", "tag": "a", "type": "", "label": "Next"}],
        }
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(browse_module, "observe_page", return_value=page):
            result = run_browse("observe")

        self.assertEqual(approval.call_args.kwargs["action_class"], "read")
        self.assertIn(UNTRUSTED_OPEN, result)
        self.assertIn("words from the page", result)

    def test_close_releases_the_browser_and_says_so(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(browse_module, "release", new_callable=mock.AsyncMock) as release:
            result = run_browse("close")

        release.assert_called_once()
        self.assertEqual(approval.call_args.kwargs["action_class"], "act")
        self.assertIn("closed", result.lower())


# ---------------------------------------------------------------------------
# The tool: back, forward, scroll
# ---------------------------------------------------------------------------


class HistoryFlowTests(AuditRedirect):
    """A history step is a read: it changes what is on screen and nothing else."""

    MOVED = {"moved": True, "title": "Fixture", "url": "http://x.test/one"}
    STUCK = {"moved": False, "title": "Fixture", "url": "http://x.test/one"}

    def test_back_runs_without_a_gate_because_it_is_a_read(self):
        # The real require_approval, unpatched: no session, no gate, and a
        # history step still goes through -- the default policy for a read,
        # by the same rule that lets `open` run.
        with mock.patch.object(
            browse_module, "go_history", return_value=self.MOVED
        ) as history:
            result = run_browse("back")

        history.assert_called_once_with("back")
        self.assertEqual(result, "Went back to Fixture.")

    def test_forward_passes_its_direction_and_records_it_as_a_read(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module, "go_history", return_value=self.MOVED
        ) as history:
            run_browse("forward")

        history.assert_called_once_with("forward")
        self.assertEqual(approval.call_args.kwargs["action"], "forward")
        self.assertEqual(approval.call_args.kwargs["action_class"], "read")

    def test_a_step_the_browser_did_not_take_does_not_claim_it_moved(self):
        # Playwright answers "nothing" for both *no earlier page* and *a
        # navigation that was cancelled*, and we cannot tell which, so the
        # sentence must not commit to either.
        with mock.patch.object(
            browse_module, "go_history", return_value=self.STUCK
        ):
            result = run_browse("back")

        self.assertEqual(
            result,
            "The browser did not go back -- there may be no earlier page.",
        )

    def test_a_long_title_is_clipped_in_the_sentence_it_becomes(self):
        # The title is text the page chose; it goes back into a spoken
        # sentence, so it is clipped like every other caption we hand on.
        with mock.patch.object(
            browse_module,
            "go_history",
            return_value={"moved": True, "title": "T" * 300, "url": "http://x.test/"},
        ):
            result = run_browse("back")
        self.assertLessEqual(len(result), 120)


class ScrollFlowTests(AuditRedirect):
    def test_scroll_defaults_to_down_and_is_a_read(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module, "scroll_page", return_value={"scrolled": True, "y": 400, "max": 4000}
        ) as scroll:
            result = run_browse("scroll")

        scroll.assert_called_once_with("down")
        self.assertEqual(approval.call_args.kwargs["action"], "scroll")
        self.assertEqual(approval.call_args.kwargs["action_class"], "read")
        self.assertTrue(result.startswith("Scrolled down."))
        # The position is *in* the sentence, and that is the point of the
        # change: reads are windowed, so the model compares this number
        # against the next read's `scroll.y`. A scroll that reports "down"
        # while landing on the same line is then visible as the no-op it is,
        # rather than as another identical read.
        self.assertIn("Now at 400 of 4000.", result)

    def test_reaching_the_bottom_says_so(self):
        with mock.patch.object(
            browse_module,
            "scroll_page",
            return_value={"scrolled": True, "y": 4000, "max": 4000},
        ):
            result = run_browse("scroll", text="down")
        self.assertIn("That is the bottom of the page.", result)

    def test_reaching_the_top_says_so(self):
        with mock.patch.object(
            browse_module, "scroll_page", return_value={"scrolled": True, "y": 0, "max": 4000}
        ):
            result = run_browse("scroll", text="up")
        self.assertIn("That is the top of the page.", result)

    def test_top_and_bottom_are_their_own_destinations(self):
        for direction, sentence in (
            ("top", "Scrolled to the top of the page."),
            ("bottom", "Scrolled to the bottom of the page."),
        ):
            with self.subTest(direction=direction):
                with mock.patch.object(
                    browse_module,
                    "scroll_page",
                    return_value={"scrolled": True, "y": 0, "max": 4000},
                ) as scroll:
                    result = run_browse("scroll", text=direction)
                scroll.assert_called_once_with(direction)
                self.assertEqual(result, sentence)

    def test_a_page_with_nothing_below_it_says_so_instead_of_scrolling(self):
        # "Scrolled down" about a page that never moved would be a claim
        # with nothing behind it, and the user watching the window saw it
        # stay still.
        with mock.patch.object(
            browse_module, "scroll_page", return_value={"scrolled": False, "y": 0, "max": 0}
        ):
            result = run_browse("scroll")
        self.assertEqual(result, "That page fits on screen -- there is nothing to scroll.")

    def test_scrolling_up_mid_page_reports_where_it_landed(self):
        # The same number matters in both directions: "scrolled up" from
        # 1400 lands somewhere specific, and a model reading the next
        # window's `scroll.y` has something to check it against.
        with mock.patch.object(
            browse_module, "scroll_page", return_value={"scrolled": True, "y": 600, "max": 4000}
        ):
            result = run_browse("scroll", text="up")
        self.assertTrue(result.startswith("Scrolled up."))
        self.assertIn("Now at 600 of 4000.", result)

    def test_a_direction_that_does_not_exist_never_reaches_the_browser(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(browse_module, "scroll_page") as scroll:
            with self.assertRaisesRegex(ToolError, "down, up, top"):
                run_browse("scroll", text="sideways")

        approval.assert_not_called()
        scroll.assert_not_called()


# ---------------------------------------------------------------------------
# The tool: act
# ---------------------------------------------------------------------------


class ActArgumentTests(unittest.TestCase):
    def test_a_reference_of_the_wrong_shape_is_refused_before_any_page_call(self):
        # `ref` is interpolated into a selector after this check; a shape we
        # never minted is not a selector at all.
        with mock.patch.object(browse_module, "inspect_ref") as inspect:
            with self.assertRaisesRegex(ToolError, "observe"):
                run_browse("act", ref='r1"] , body')
        inspect.assert_not_called()

    def test_a_reference_the_page_has_dropped_is_refused(self):
        with mock.patch.object(browse_module, "inspect_ref", return_value={"found": False}):
            with self.assertRaisesRegex(ToolError, "observe"):
                run_browse("act", ref="r9")


class ClickFlowTests(AuditRedirect):
    """Audit-redirected: the submit test's refusal writes a line."""

    LINK = {
        "found": True,
        "tag": "a",
        "type": "",
        "inForm": False,
        "sig": "a||0",
        "url": "https://x.test/page",
        "label": "Next page",
    }
    SUBMIT = {
        "found": True,
        "tag": "input",
        "type": "submit",
        "inForm": True,
        "sig": "input|submit|1",
        "url": "https://x.test/form",
        "label": "Send",
    }

    def test_clicking_a_link_is_an_act_and_runs_without_a_gate(self):
        # The real require_approval, unpatched: no session, no gate, and an
        # act-class click still goes through -- the default policy.
        with mock.patch.object(browse_module, "inspect_ref", return_value=self.LINK), mock.patch.object(
            browse_module,
            "click_ref",
            return_value={
                "ok": True,
                "moved": False,
                "after": {"url": "https://x.test/list", "title": "List"},
            },
        ) as clicker:
            result = run_browse("act", ref="r2")

        # Where the page ended up is part of the claim. "Clicked Next page" on its
        # own is how a click that landed somewhere else entirely was reported as a
        # success.
        self.assertIn("Clicked Next page on x.test", result)
        self.assertIn("https://x.test/list", result)
        # The signature travels with the click: the page re-checks it inside
        # the same evaluation that performs it.
        clicker.assert_called_once_with("r2", "a||0")

    def test_a_click_that_navigated_says_where_it_went(self):
        # The reported bug: clicking a video result on YouTube produced
        # "Clicked <title> on www.google.com." -- a success -- and the model then
        # read whatever text was left on screen and answered from it.
        with mock.patch.object(browse_module, "inspect_ref", return_value=self.LINK), mock.patch.object(
            browse_module,
            "click_ref",
            return_value={
                "ok": True,
                "moved": True,
                "after": {"url": "https://www.youtube.com/watch?v=abc", "title": "Song"},
            },
        ):
            result = run_browse("act", ref="r2")

        self.assertIn("https://www.youtube.com/watch?v=abc", result)
        self.assertNotIn("stayed on", result, "the page did move, so this is not a 'did not move'")

    def test_a_click_that_went_to_the_wrong_place_is_still_reported_as_a_landing(self):
        # The same failure with a different destination: the honest report is the
        # address, and the model can compare it against what it meant to open.
        with mock.patch.object(browse_module, "inspect_ref", return_value=self.LINK), mock.patch.object(
            browse_module,
            "click_ref",
            return_value={
                "ok": True,
                "moved": True,
                "after": {"url": "https://www.youtube.com/", "title": "YouTube"},
            },
        ):
            result = run_browse("act", ref="r2")

        self.assertIn("https://www.youtube.com/", result)

    def test_a_click_that_moved_nothing_is_not_reported_as_a_navigation(self):
        # Plenty of controls expand a menu or load more rows without navigating.
        # Calling those failures would train the model to distrust working clicks,
        # so the sentence says the click landed and the page stayed -- and asks for
        # a re-read rather than a second identical click.
        with mock.patch.object(browse_module, "inspect_ref", return_value=self.LINK), mock.patch.object(
            browse_module,
            "click_ref",
            return_value={
                "ok": True,
                "moved": False,
                "after": {"url": "https://x.test/list", "title": "List"},
            },
        ):
            result = run_browse("act", ref="r2")

        self.assertIn("the page stayed on", result)
        self.assertIn("read the page again", result)

    def test_a_click_with_no_readable_destination_reports_only_the_click(self):
        # A session that could not be read after the click is not a reason to
        # refuse, and inventing an address would be worse than omitting one.
        with mock.patch.object(browse_module, "inspect_ref", return_value=self.LINK), mock.patch.object(
            browse_module, "click_ref", return_value={"ok": True}
        ):
            result = run_browse("act", ref="r2")

        self.assertEqual(result, "Clicked Next page on x.test.")

    def test_submitting_a_form_without_an_approval_channel_does_nothing(self):
        # The fail-closed claim, through the real gate module: nobody to ask
        # means no, and the click is never attempted.
        with mock.patch.object(browse_module, "inspect_ref", return_value=self.SUBMIT), mock.patch.object(
            browse_module, "click_ref"
        ) as clicker:
            with self.assertRaises(ToolError) as caught:
                run_browse("act", ref="r3")

        self.assertEqual(str(caught.exception), MSG_NO_GATE)
        clicker.assert_not_called()

    def test_a_submit_clicks_only_after_a_sensitive_approval(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module, "inspect_ref", return_value=self.SUBMIT
        ), mock.patch.object(browse_module, "click_ref", return_value={"ok": True}) as clicker:
            result = run_browse("act", ref="r3")

        self.assertEqual(approval.call_args.kwargs["action_class"], "sensitive")
        # The read-back names the control and the host: "Click the button"
        # is not a question anybody can answer.
        self.assertIn("Send", approval.call_args.kwargs["summary"])
        self.assertIn("x.test", approval.call_args.kwargs["summary"])
        clicker.assert_called_once()

    def test_a_link_click_asks_about_nothing_but_still_records_its_class(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module, "inspect_ref", return_value=self.LINK
        ), mock.patch.object(browse_module, "click_ref", return_value={"ok": True}):
            run_browse("act", ref="r2")
        self.assertEqual(approval.call_args.kwargs["action_class"], "act")

    def test_a_control_that_changed_under_the_reference_stops(self):
        # The page swapped elements between inspect and click; the atomic
        # re-check inside the click refused, and we report that as nothing
        # done rather than as a click.
        with mock.patch.object(browse_module, "inspect_ref", return_value=self.LINK), mock.patch.object(
            browse_module, "click_ref", return_value={"ok": False, "reason": "changed"}
        ):
            with self.assertRaisesRegex(ToolError, "observe it again"):
                run_browse("act", ref="r2")


class FillFlowTests(unittest.TestCase):
    FIELD = {
        "found": True,
        "tag": "input",
        "type": "text",
        "inForm": True,
        "sig": "input|text|1",
        "url": "https://x.test/form",
        "label": "Your name",
    }
    PASSWORD = {**FIELD, "type": "password", "label": "Password"}

    def test_password_fields_are_refused_before_any_question(self):
        # A card the user answers "yes" to is not consent to have a password
        # typed anywhere: refused outright, no approval call at all.
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module, "inspect_ref", return_value=self.PASSWORD
        ), mock.patch.object(browse_module, "fill_ref") as filler:
            with self.assertRaisesRegex(ToolError, "password"):
                run_browse("act", ref="r1", text="hunter2")

        approval.assert_not_called()
        filler.assert_not_called()

    def test_typing_is_an_act_that_reaches_the_field(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module, "inspect_ref", return_value=self.FIELD
        ), mock.patch.object(browse_module, "fill_ref", return_value={"ok": True}) as filler:
            result = run_browse("act", ref="r4", text="hello world")

        self.assertEqual(approval.call_args.kwargs["action_class"], "act")
        filler.assert_called_once_with("r4", "hello world")
        self.assertIn("Typed into", result)

    def test_a_page_that_swapped_in_a_password_field_at_typing_time_still_refuses(self):
        # The second refusal, the one inside the DOM: the swap between our
        # inspection and our typing changes nothing.
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(browse_module, "inspect_ref", return_value=self.FIELD), mock.patch.object(
            browse_module, "fill_ref", return_value={"ok": False, "reason": "password"}
        ):
            with self.assertRaisesRegex(ToolError, "password"):
                run_browse("act", ref="r4", text="hunter2")

    def test_a_control_that_takes_no_text_says_so(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(browse_module, "inspect_ref", return_value=self.FIELD), mock.patch.object(
            browse_module, "fill_ref", return_value={"ok": False, "reason": "not fillable"}
        ):
            with self.assertRaisesRegex(ToolError, "doesn't take text"):
                run_browse("act", ref="r4", text="hello")


# ---------------------------------------------------------------------------
# The tool: act -- pressing a key
# ---------------------------------------------------------------------------


class PressFlowTests(AuditRedirect):
    """A key press, through the same facts-then-question-then-act path."""

    BUTTON = {
        "found": True,
        "tag": "button",
        "type": "",
        "inForm": False,
        "sig": "button||0",
        "url": "https://x.test/page",
        "label": "Poke",
    }
    FIELD = {
        "found": True,
        "tag": "input",
        "type": "text",
        "inForm": True,
        "sig": "input|text|1",
        "url": "https://x.test/form",
        "label": "Your name",
    }

    def test_a_key_that_cannot_submit_runs_without_a_gate(self):
        # The real require_approval, unpatched: an arrow key is an act, and
        # with nobody in the room an act still goes through.
        with mock.patch.object(
            browse_module, "inspect_ref", return_value=self.BUTTON
        ), mock.patch.object(
            browse_module, "press_ref", return_value={"ok": True}
        ) as press:
            result = run_browse("act", ref="r2", key="ArrowDown")

        # The facts travel with the key: the page re-checks them at the
        # moment of pressing, so a swapped control is a refusal, not a
        # press on whatever is there now.
        press.assert_called_once_with("r2", "button||0", "ArrowDown")
        self.assertEqual(result, "Pressed ArrowDown on Poke on x.test.")

    def test_enter_inside_a_form_refuses_with_nobody_to_ask(self):
        # The whole reason `classify_key` exists: implicit submission, a
        # real gate, no session -- and the press is never attempted.
        with mock.patch.object(
            browse_module, "inspect_ref", return_value=self.FIELD
        ), mock.patch.object(browse_module, "press_ref") as press:
            with self.assertRaises(ToolError) as caught:
                run_browse("act", ref="r4", key="Enter")

        self.assertEqual(str(caught.exception), MSG_NO_GATE)
        press.assert_not_called()

    def test_enter_on_a_control_with_no_form_around_it_is_an_act(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module, "inspect_ref", return_value=self.BUTTON
        ), mock.patch.object(
            browse_module, "press_ref", return_value={"ok": True}
        ):
            run_browse("act", ref="r2", key="Enter")

        self.assertEqual(approval.call_args.kwargs["action_class"], "act")
        self.assertEqual(approval.call_args.kwargs["action"], "press")

    def test_a_key_outside_the_closed_list_never_touches_the_page(self):
        # Refused before even the facts are read: a key we will not press
        # must never become the nearest key that was available instead.
        with mock.patch.object(browse_module, "inspect_ref") as inspect, mock.patch.object(
            browse_module, "press_ref"
        ) as press:
            with self.assertRaisesRegex(ToolError, "arrow key"):
                run_browse("act", ref="r2", key="Ctrl+W")

        inspect.assert_not_called()
        press.assert_not_called()

    def test_text_and_a_key_together_are_two_requests_and_both_are_refused(self):
        with mock.patch.object(browse_module, "inspect_ref") as inspect:
            with self.assertRaises(ToolError):
                run_browse("act", ref="r4", text="hello", key="Enter")
        inspect.assert_not_called()

    def test_an_alias_is_normalized_before_it_reaches_the_browser(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module, "inspect_ref", return_value=self.BUTTON
        ), mock.patch.object(
            browse_module, "press_ref", return_value={"ok": True}
        ) as press:
            result = run_browse("act", ref="r2", key="arrowdown")

        press.assert_called_once_with("r2", "button||0", "ArrowDown")
        self.assertEqual(result, "Pressed ArrowDown on Poke on x.test.")

    def test_space_is_spoken_as_a_word_rather_than_a_blank(self):
        # The sentence goes out loud: "Pressed  on Poke" is a sentence
        # with a hole in it.
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module, "inspect_ref", return_value=self.BUTTON
        ), mock.patch.object(
            browse_module, "press_ref", return_value={"ok": True}
        ) as press:
            result = run_browse("act", ref="r2", key="Space")

        press.assert_called_once_with("r2", "button||0", " ")
        self.assertEqual(result, "Pressed Space on Poke on x.test.")

    def test_a_control_that_will_not_take_the_key_says_so(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module, "inspect_ref", return_value=self.BUTTON
        ), mock.patch.object(
            browse_module, "press_ref", return_value={"ok": False, "reason": "not pressable"}
        ):
            with self.assertRaisesRegex(ToolError, "won't take the key"):
                run_browse("act", ref="r2", key="Enter")

    def test_a_control_that_changed_under_the_reference_stops_before_the_key(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module, "inspect_ref", return_value=self.FIELD
        ), mock.patch.object(
            browse_module, "press_ref", return_value={"ok": False, "reason": "changed"}
        ):
            with self.assertRaisesRegex(ToolError, "observe it again"):
                run_browse("act", ref="r4", key="ArrowDown")


# ---------------------------------------------------------------------------
# The tool: act -- choosing an option
# ---------------------------------------------------------------------------


class SelectFlowTests(AuditRedirect):
    """A dropdown's text is a choice, decided by the control's tag, not its argument."""

    DROPDOWN = {
        "found": True,
        "tag": "select",
        "type": "",
        "inForm": False,
        "sig": "select||0",
        "url": "https://x.test/signup",
        "label": "Country",
    }
    IN_FORM = {**DROPDOWN, "inForm": True, "sig": "select||1"}
    FIELD = {
        "found": True,
        "tag": "input",
        "type": "text",
        "inForm": True,
        "sig": "input|text|1",
        "url": "https://x.test/signup",
        "label": "Your name",
    }

    def test_typing_into_a_dropdown_chooses_an_option(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module, "inspect_ref", return_value=self.DROPDOWN
        ), mock.patch.object(
            browse_module,
            "select_ref",
            return_value={"ok": True, "chosen": "United States"},
        ) as chooser:
            result = run_browse("act", ref="r7", text="United States")

        self.assertEqual(approval.call_args.kwargs["action"], "select")
        self.assertEqual(approval.call_args.kwargs["action_class"], "act")
        # The facts travel with the choice, so a control that is no longer
        # the dropdown we classified is a refusal rather than a choice made
        # in something else.
        chooser.assert_called_once_with("r7", "select||0", "United States")
        # The read-back reports what the *page* says it chose, not only
        # what we asked for.
        self.assertEqual(result, "Chose United States in Country on x.test.")

    def test_a_dropdown_inside_a_form_is_still_an_act(self):
        # Choosing an option changes what the page *would* send; the send
        # is a separate click, classified on its own. This is the decision
        # pinned: a select never borrows the form's class.
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module, "inspect_ref", return_value=self.IN_FORM
        ), mock.patch.object(
            browse_module, "select_ref", return_value={"ok": True, "chosen": "bd"}
        ):
            run_browse("act", ref="r7", text="Bangladesh")

        self.assertEqual(approval.call_args.kwargs["action_class"], "act")

    def test_a_text_field_is_still_filled_not_chosen(self):
        # The tag decides, not the argument: the same text into an input
        # is a fill, and the choice path is never entered.
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module, "inspect_ref", return_value=self.FIELD
        ), mock.patch.object(
            browse_module, "fill_ref", return_value={"ok": True}
        ) as filler, mock.patch.object(
            browse_module, "select_ref"
        ) as chooser:
            result = run_browse("act", ref="r8", text="hello")

        filler.assert_called_once_with("r8", "hello")
        chooser.assert_not_called()
        self.assertIn("Typed into", result)

    def test_a_choice_the_page_does_not_offer_says_so(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module, "inspect_ref", return_value=self.DROPDOWN
        ), mock.patch.object(
            browse_module, "select_ref", return_value={"ok": False, "reason": "no such option"}
        ):
            with self.assertRaisesRegex(ToolError, "doesn't offer that choice"):
                run_browse("act", ref="r7", text="Atlantis")

    def test_a_control_that_changed_under_the_reference_stops_before_choosing(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module, "inspect_ref", return_value=self.DROPDOWN
        ), mock.patch.object(
            browse_module, "select_ref", return_value={"ok": False, "reason": "changed"}
        ):
            with self.assertRaisesRegex(ToolError, "observe it again"):
                run_browse("act", ref="r7", text="United States")


# ---------------------------------------------------------------------------
# The guard: what the page itself may fetch
# ---------------------------------------------------------------------------


class FakeRoute:
    def __init__(self) -> None:
        self.continued = False
        self.aborted = False

    async def continue_(self):
        self.continued = True

    async def abort(self):
        self.aborted = True


class FakeRequest:
    def __init__(self, url: str, navigation: bool = True) -> None:
        self.url = url
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class RouteGuardTests(unittest.TestCase):
    def _guard(self, url: str, navigation: bool = True) -> FakeRoute:
        route, request = FakeRoute(), FakeRequest(url, navigation)
        asyncio.run(session._browser._guard(route, request))
        return route

    def test_a_private_address_is_aborted_even_for_a_page_js_fetch(self):
        for url in ("http://192.168.1.1/reboot", "http://localhost:8080/admin"):
            with self.subTest(url=url):
                route = self._guard(url, navigation=False)
                self.assertTrue(route.aborted)
                self.assertFalse(route.continued)

    def test_a_public_address_is_allowed(self):
        route = self._guard("http://8.8.8.8/", navigation=False)
        self.assertTrue(route.continued)

    def test_a_file_url_is_never_fetched(self):
        route = self._guard("file:///etc/passwd")
        self.assertTrue(route.aborted)

    def test_inline_data_is_content_not_a_page(self):
        # data: URIs are how the web is built -- allowed as a subresource,
        # where they reach nowhere; refused as a navigation, where they are
        # not a page anybody asked for.
        image = self._guard("data:image/png;base64,iVBORw0KGgo=", navigation=False)
        self.assertTrue(image.continued)
        page = self._guard("data:text/html,<h1>hi</h1>", navigation=True)
        self.assertTrue(page.aborted)


class AllowedUrlTests(unittest.TestCase):
    def test_a_url_is_openable_only_if_it_is_public_web(self):
        ok, why = asyncio.run(session._browser.allowed("http://192.168.1.1/admin"))
        self.assertFalse(ok)
        self.assertIn("not a public address", why)

    def test_localhost_refuses_by_name(self):
        ok, why = asyncio.run(session._browser.allowed("http://localhost:8080/"))
        self.assertFalse(ok)
        self.assertEqual(why, "it is a local name")

    def test_a_file_url_is_not_a_web_page(self):
        ok, why = asyncio.run(session._browser.allowed("file:///etc/passwd"))
        self.assertFalse(ok)
        self.assertIn("only web pages", why)

    def test_the_local_network_switch_is_real(self):
        with mock.patch.dict(os.environ, {session.ALLOW_LOCAL_VAR: "true"}):
            ok, _why = asyncio.run(session._browser.allowed("http://192.168.1.1/"))
        self.assertTrue(ok)


class ProfileDirTests(unittest.TestCase):
    def test_an_override_wins(self):
        with mock.patch.dict(os.environ, {session.PROFILE_VAR: "Z:/somewhere/profile"}):
            self.assertEqual(session.profile_dir(), Path("Z:/somewhere/profile"))

    def test_the_default_is_lumine_s_own_directory(self):
        # Whatever the platform, the profile is under a "lumine" directory --
        # never a path inside a real browser's profile, which is the whole
        # point of this function existing.
        with mock.patch.dict(os.environ, {session.PROFILE_VAR: ""}):
            parts = session.profile_dir().parts
        self.assertEqual(parts[-2:], ("lumine", "browser-profile"))


class ConcurrentRoomTests(unittest.TestCase):
    """One browser, many rooms -- the shape that stopped her opening anything.

    LiveKit 1.8.3 runs every dispatched room in its own **thread with its own
    event loop** (`_default_job_executor_type = THREAD`, and `ProcClient.run`
    calls `asyncio.new_event_loop()` per job), while the browser is deliberately
    one process-wide singleton. So two rooms reaching `ensure()` at once is the
    normal case, not an edge case.

    The bug these pin: the mutual exclusion was an `asyncio.Lock`, which is not
    thread-safe. The uncontended `acquire()` binds no loop, so the losing room
    parked on a future belonging to *its* loop and the winning room released by
    calling `set_result` on it from the wrong thread -- without
    `call_soon_threadsafe`, so the loser's loop never woke. `open` hung
    forever and the room heard nothing at all.

    Every test here uses a fake page and a stubbed `_start`, so none of them
    launches Chromium. Threads are joined with a timeout and the failure message
    says which thread hung, because "the test timed out" is not a diagnosis.
    """

    JOIN_TIMEOUT = 25.0

    def test_the_ownership_lock_is_a_thread_lock_not_an_asyncio_lock(self):
        """The structural fact the whole fix rests on.

        An `asyncio.Lock` here *is* the bug, so this fails loudly if somebody
        simplifies it back.
        """
        browser = session._Browser()
        self.assertIsInstance(browser._lock, session._OwnerLock)
        self.assertNotIsInstance(browser._lock, asyncio.Lock)

    def test_an_ownership_decision_does_not_await_inside_the_lock(self):
        # Holding a thread lock across an `await` would park every other room
        # behind one browser start. `_OwnerLock` is only ever a context manager
        # over non-blocking statements, so this is a shape check on the class.
        lock = session._OwnerLock()
        with lock:
            self.assertTrue(lock._lock.locked())
        self.assertFalse(lock._lock.locked())

    def test_two_rooms_at_once_do_not_deadlock(self):
        """The regression itself: two rooms, one browser, neither may hang."""
        browser = session._Browser()
        started = threading.Barrier(2, timeout=self.JOIN_TIMEOUT)
        outcomes: dict[str, str] = {}

        async def room(label: str) -> None:
            # Both rooms arrive together, then each tries to ensure a page.
            started.wait()
            try:
                page = await browser.ensure()
                outcomes[label] = f"ok:{page.url}"
            except ToolError as exc:
                outcomes[label] = f"refused:{exc}"

        def drive(label: str) -> None:
            asyncio.run(room(label))

        first = threading.Thread(target=drive, args=("A",), daemon=True)
        second = threading.Thread(target=drive, args=("B",), daemon=True)
        first.start()
        second.start()
        first.join(self.JOIN_TIMEOUT)
        second.join(self.JOIN_TIMEOUT)

        self.assertFalse(
            first.is_alive(),
            "room A deadlocked waiting for the browser lock -- this is the bug",
        )
        self.assertFalse(
            second.is_alive(),
            "room B deadlocked waiting for the browser lock -- this is the bug",
        )
        # Whoever won, exactly one browser was started and both rooms were told
        # something definite. Silence is the failure mode that matters.
        self.assertEqual(len(outcomes), 2, f"a room never returned: {outcomes}")

    def test_a_live_owner_loop_is_still_refused_rather_than_stolen(self):
        """The fix must not have cost the honest refusal.

        A browser a *running* loop is driving belongs to that conversation.
        Taking it would be two rooms fighting over one window, so the refusal
        stands -- it just no longer arrives by hanging. The owner loop here is
        genuinely running in another thread, which is the only condition under
        which refusing is right.
        """
        browser = session._Browser()
        holding = threading.Event()
        may_finish = threading.Event()
        outcome: dict[str, object] = {}

        def owning_room() -> None:
            async def hold() -> None:
                outcome["loop"] = asyncio.get_running_loop()
                holding.set()
                await asyncio.to_thread(may_finish.wait, self.JOIN_TIMEOUT)

            asyncio.run(hold())

        owner = threading.Thread(target=owning_room, daemon=True)
        owner.start()
        holding.wait(self.JOIN_TIMEOUT)
        try:
            browser._loop = outcome["loop"]
            browser._page = SimpleNamespace(is_closed=lambda: False, url="https://example.com/")

            async def ask() -> None:
                with self.assertRaises(ToolError) as caught:
                    await browser.ensure()
                self.assertIn("busy", str(caught.exception))

            asyncio.run(ask())
        finally:
            may_finish.set()
            owner.join(self.JOIN_TIMEOUT)
        self.assertFalse(owner.is_alive(), "the owning room never finished")

    def test_a_dead_owner_loop_is_taken_over_rather_than_refused_forever(self):
        """A room that died without releasing must not brick the worker.

        Refusing on a *running* loop is honesty. Refusing on a loop that has
        been closed is refusing nobody -- the room is gone, nothing will ever
        release its browser, and the whole process-wide singleton would stay
        unusable for the life of the worker. That is how one crashed room turned
        into "her browser is broken, forever".
        """
        browser = session._Browser()
        dead_loop = asyncio.new_event_loop()
        dead_loop.close()  # gone, and nothing will ever release it
        browser._loop = dead_loop
        browser._page = SimpleNamespace(is_closed=lambda: False, url="https://example.com/")
        browser._context = SimpleNamespace()
        browser._pw = SimpleNamespace()

        async def ask() -> str:
            # `_start` is stubbed so this stays a decision test, not a launch.
            async def fake_start(_self, _loop, *, background=False):
                return SimpleNamespace(is_closed=lambda: False, url="https://recovered.example/")

            with mock.patch.object(session._Browser, "_start", fake_start):
                page = await browser.ensure()
                return str(page.url)

        url = asyncio.run(ask())
        self.assertEqual(url, "https://recovered.example/")
        self.assertIsNot(browser._loop, dead_loop)

    def test_release_from_another_loop_still_frees_the_singleton(self):
        """`close_session` awaits `release()` from whichever loop it lands on.

        The old code returned early on a cross-loop call *before* clearing
        state, so the singleton kept pointing at a dead loop and every later
        room was refused with `MSG_OTHER_LOOP` -- for the rest of the worker's
        life. Clearing is what makes the next room work.
        """

        async def make_browser():
            browser = session._Browser()
            browser._loop = asyncio.get_running_loop()
            browser._page = SimpleNamespace(is_closed=lambda: False, url="https://example.com/")
            browser._wa_page = SimpleNamespace(is_closed=lambda: False)
            browser._context = SimpleNamespace()
            browser._pw = SimpleNamespace()
            return browser

        browser = asyncio.run(make_browser())
        released = threading.Event()
        failed: list[BaseException] = []

        def tearing_down_room() -> None:
            # A different loop, running in a different thread, releasing a
            # browser it does not own -- exactly `close_session`'s position when
            # the room's browser was started elsewhere.
            async def release() -> None:
                try:
                    await browser.release()
                except BaseException as exc:  # noqa: BLE001
                    failed.append(exc)
                finally:
                    released.set()

            asyncio.run(release())

        room = threading.Thread(target=tearing_down_room, daemon=True)
        room.start()
        room.join(self.JOIN_TIMEOUT)
        self.assertFalse(room.is_alive(), "the tearing-down room never finished")
        self.assertTrue(released.is_set(), "release() never completed")
        self.assertEqual(failed, [], f"release() raised: {failed}")

        self.assertIsNone(browser._loop, "ownership was not dropped")
        self.assertIsNone(browser._page)
        self.assertIsNone(browser._context)
        self.assertIsNone(browser._pw)
        self.assertIsNone(browser._wa_page)

    def test_a_room_can_browse_after_an_untidy_teardown(self):
        """End to end through the module surface: the user's second attempt.

        This is the sentence the user would have heard on every turn after the
        bug -- "the browser is busy with another conversation" -- so it is worth
        pinning at the level `browse` actually calls.
        """
        browser = session._Browser()

        async def abandoned_room_then_new_room() -> str:
            owner = asyncio.new_event_loop()
            try:
                browser._loop = owner
                browser._page = SimpleNamespace(
                    is_closed=lambda: False, url="https://example.com/"
                )
                browser._context = SimpleNamespace()
                browser._pw = SimpleNamespace()
            finally:
                owner.close()  # the room ended; its release never ran

            async def fake_start(_self, _loop, *, background=False):
                return SimpleNamespace(is_closed=lambda: False, url="https://fresh.example/")

            with mock.patch.object(session._Browser, "_start", fake_start):
                page = await browser.ensure()
                return str(page.url)

        self.assertEqual(asyncio.run(abandoned_room_then_new_room()), "https://fresh.example/")


class CardPromiseTests(unittest.TestCase):
    def test_the_card_promises_asking_and_the_floor_delivers_it(self):
        # The badge on the Tools page comes from the same map the gate reads,
        # so this is one assertion about both.
        self.assertEqual(TOOL_ACTION_CLASS["browse"], "sensitive")
        self.assertEqual(permission_floor("browse"), "ask")


if __name__ == "__main__":
    unittest.main()
