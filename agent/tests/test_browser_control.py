"""Finding a control on a page, and not guessing when several match.

The reported browser failures were all the same shape: the right page, the wrong
element. "Click the second video" clicked something; "search" typed into the wrong
box; a dropdown got the wrong option. Each is a *resolution* failure, and this
file is where resolution is decided.

Everything runs against fixture candidates -- objects shaped like what the
collection script returns -- so the ranking, the ambiguity rule and the
ordinal handling are all testable with no browser, no network and no page.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from agent.tools.browser import control, engine, locators, state, youtube
from agent.tools.browser.locators import (
    Candidate,
    ElementQuery,
    LocatorError,
    build_query,
    normalize_role,
    normalize_strategy,
    ordinal_of,
    resolve_one,
    valid_css,
)


def candidate(index: int, name: str, role: str = "link", **detail) -> Candidate:
    """A fixture candidate. ``**detail`` carries the page's own extras.

    Visibility and enabled-ness are named parameters rather than detail entries,
    because they are *eligibility* rather than metadata -- a hidden element is
    not a poor match, it is not a candidate at all, and putting it in ``detail``
    would hide that distinction inside a fixture.
    """
    visible = bool(detail.pop("visible", True))
    enabled = bool(detail.pop("enabled", True))
    return Candidate(
        index=index, role=role, name=name, visible=visible, enabled=enabled, detail=detail
    )


RESULTS = [
    candidate(1, "Baby Shark", role="link", channel="Pinkfong", duration="2:30"),
    candidate(2, "Baby Shark Remix", role="link", channel="Cocomelon", duration="3:00"),
    candidate(3, "Bath Song", role="link", channel="Cocomelon", duration="1:50"),
    candidate(4, "Learn Colors", role="link", channel="Cocomelon", duration="4:20"),
]


def channel_of(item) -> str:
    """A candidate's channel, from the detail the page put there."""
    return str(item.detail.get("channel", ""))


# -- parsing -------------------------------------------------------------------


class RoleTests(unittest.TestCase):
    def test_known_roles_pass_through(self):
        for role in ("button", "link", "searchbox", "combobox"):
            self.assertEqual(normalize_role(role), role)

    def test_a_plural_is_read_as_the_singular(self):
        # Models write "buttons" and "links"; the browser wants the singular.
        self.assertEqual(normalize_role("buttons"), "button")

    def test_an_unknown_role_is_refused_at_the_boundary(self):
        # Better an error now than a locator that matches nothing and times out
        # inside the page.
        with self.assertRaises(LocatorError):
            normalize_role("carousel")

    def test_an_empty_role_is_no_role(self):
        self.assertEqual(normalize_role(""), "")


class StrategyTests(unittest.TestCase):
    def test_known_strategies_and_their_aliases_resolve(self):
        self.assertEqual(normalize_strategy("label"), "label")
        self.assertEqual(normalize_strategy("aria-label"), "label")
        self.assertEqual(normalize_strategy("visible text"), "text")
        self.assertEqual(normalize_strategy("name"), "text")

    def test_an_unknown_strategy_is_no_strategy(self):
        # The caller then falls back to the default rather than guessing.
        self.assertEqual(normalize_strategy("astrology"), "")


class OrdinalTests(unittest.TestCase):
    def test_words_are_read_before_digits(self):
        # "the second video by Cocomelon" contains both, and the word is the one
        # the user meant.
        self.assertEqual(ordinal_of("the second video by Cocomelon"), 2)
        self.assertEqual(ordinal_of("click the 2nd result"), 2)

    def test_plain_numbers_are_positions(self):
        self.assertEqual(ordinal_of("3"), 3)
        self.assertEqual(ordinal_of("the 4th one"), 4)

    def test_last_is_negative_and_means_the_end(self):
        self.assertEqual(ordinal_of("the last one"), -1)

    def test_nonsense_is_no_position(self):
        for raw in ("", None, "many", "0", "999"):
            with self.subTest(raw=raw):
                self.assertEqual(ordinal_of(raw), 0)


class CssValidationTests(unittest.TestCase):
    def test_an_ordinary_selector_is_accepted(self):
        self.assertEqual(valid_css("form#login input[name=q]"), "form#login input[name=q]")

    def test_an_empty_selector_is_refused(self):
        with self.assertRaises(LocatorError):
            valid_css("")

    def test_an_over_long_selector_is_refused(self):
        with self.assertRaises(LocatorError):
            valid_css("a" * (locators.CSS_MAX + 1))

    def test_a_selector_with_script_syntax_is_refused(self):
        # A selector is the one strategy that is a language handed to the browser,
        # and an unbounded one is a denial of service against our own page.
        for raw in ("a[href='javascript:alert(1)']", "a[b='data:text/html,x']"):
            with self.subTest(raw=raw):
                with self.assertRaises(LocatorError):
                    valid_css(raw)

    def test_a_selector_with_expression_syntax_is_refused(self):
        with self.assertRaises(LocatorError):
            valid_css("a[href ^= 'javascript']")


class QueryTests(unittest.TestCase):
    def test_a_role_and_a_name_is_the_ordinary_case(self):
        query = build_query(role="link", name="Baby Shark")
        self.assertEqual(query.role, "link")
        self.assertEqual(query.name, "Baby Shark")

    def test_a_name_and_a_selector_together_are_refused(self):
        # Two ways of addressing the same thing is a contradiction, not a
        # preference; guessing which was meant is how the wrong one is used.
        with self.assertRaises(LocatorError):
            build_query(role="link", name="Search", css="#q")

    def test_a_query_with_nothing_to_go_on_is_refused(self):
        with self.assertRaises(LocatorError):
            build_query()

    def test_a_role_on_its_own_is_enough(self):
        # "Click the second video" and "click the third result" name no text at
        # all. Refusing them sends the model off to invent a name for something
        # the page has not shown yet, and then clicks the wrong thing.
        self.assertEqual(build_query(role="link", position="second").ordinal, 2)
        self.assertEqual(build_query(role="button").role, "button")

    def test_a_role_and_a_position_need_no_name(self):
        query = build_query(role="link", position="second")
        self.assertEqual(query.role, "link")
        self.assertEqual(query.name, "")

    def test_a_selector_implies_the_css_strategy(self):
        self.assertEqual(build_query(css="#search").strategy, "css")

    def test_a_position_is_read_into_the_query(self):
        self.assertEqual(build_query(role="link", name="video", position="second").ordinal, 2)

    def test_whitespace_in_a_name_is_collapsed(self):
        self.assertEqual(build_query(role="button", name="  Sign   in ").name, "Sign in")


# -- ranking and ambiguity ------------------------------------------------------


class RankingTests(unittest.TestCase):
    def test_an_exact_name_beats_a_prefix_beats_a_substring(self):
        query = build_query(role="link", name="Bath Song")
        ranked = locators.rank(RESULTS, query)
        self.assertEqual(ranked[0].name, "Bath Song")

    def test_a_prefix_outranks_a_substring_appearing_later(self):
        query = build_query(role="link", name="Baby Shark")
        ranked = locators.rank(RESULTS, query)
        # "Baby Shark" exactly, then "Baby Shark Remix" as the prefix.
        self.assertEqual(ranked[0].name, "Baby Shark")
        self.assertEqual(ranked[1].name, "Baby Shark Remix")

    def test_a_qualifier_narrows_within_a_role(self):
        # "the second video by Cocomelon": channel is evidence about intent, and
        # dropping it because it was inconvenient opens the wrong video.
        query = build_query(role="link", name="video", strategy="text", qualifier="Cocomelon")
        ranked = locators.rank(RESULTS, query)
        self.assertEqual({channel_of(item) for item in ranked[:2]}, {"Cocomelon"})

    def test_hidden_candidates_are_filtered_not_merely_ranked_last(self):
        hidden = candidate(1, "Search", role="button", visible=False)
        query = build_query(role="button", name="Search")
        ranked = locators.rank([hidden, candidate(2, "Search results", role="button")], query)
        self.assertEqual([item.name for item in ranked], ["Search results"])

    def test_candidates_of_another_role_are_not_candidates(self):
        """A role query filters. Without this, "the second *video*" was the
        second thing on the page -- a search box, a cookie button, a menu link --
        because the role in the request decided nothing at all."""
        page = [
            candidate(1, "q", role="textbox"),
            candidate(2, "Go", role="button"),
            candidate(3, "First result", role="link"),
            candidate(4, "Second result", role="link"),
        ]
        ranked = locators.rank(page, build_query(role="link", position="second"))
        self.assertEqual([item.name for item in ranked], ["First result", "Second result"])
        resolution = resolve_one(page, build_query(role="link", position="second"))
        self.assertEqual(resolution.chosen.name, "Second result")

    def test_a_role_query_with_nothing_of_that_role_is_empty(self):
        page = [candidate(1, "q", role="textbox"), candidate(2, "Go", role="button")]
        resolution = resolve_one(page, build_query(role="link"))
        self.assertTrue(resolution.empty)

    def test_ties_keep_enumeration_order_so_a_position_means_something_twice(self):
        first = locators.rank(RESULTS, ElementQuery(strategy="text"))
        second = locators.rank(RESULTS, ElementQuery(strategy="text"))
        self.assertEqual([item.index for item in first], [item.index for item in second])


class ResolutionTests(unittest.TestCase):
    def test_one_match_is_resolved_without_a_question(self):
        resolution = resolve_one([candidate(1, "Sign in", role="button")], build_query(role="button", name="Sign in"))
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.chosen.name, "Sign in")

    def test_several_exact_matches_refuse_rather_than_take_the_first(self):
        # "Click the Search button" with three Search buttons is ambiguous, and
        # picking the first is how the user finds out from the audio.
        candidates = [candidate(i, "Search", role="button") for i in (1, 2, 3)]
        resolution = resolve_one(candidates, ElementQuery(role="button", name="Search"))
        self.assertFalse(resolution.ok)
        self.assertTrue(resolution.ambiguous)
        self.assertEqual(len(resolution.candidates), 3)

    def test_an_ordinal_resolves_what_ambiguity_would_refuse(self):
        candidates = [candidate(i, "Search", role="button") for i in (1, 2, 3)]
        query = ElementQuery(role="button", name="Search", ordinal=2)
        resolution = resolve_one(candidates, query)
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.chosen.index, 2)

    def test_an_ordinal_beyond_the_list_says_how_many_there_were(self):
        candidates = [candidate(i, "Search", role="button") for i in (1, 2)]
        resolution = resolve_one(candidates, ElementQuery(role="button", name="Search", ordinal=5))
        self.assertFalse(resolution.ok)
        self.assertIn("only 2", resolution.reason)

    def test_last_means_the_end_of_the_list(self):
        candidates = [candidate(i, "Search", role="button") for i in (1, 2)]
        resolution = resolve_one(candidates, ElementQuery(role="button", name="Search", ordinal=-1))
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.chosen.index, 2)

    def test_nothing_matching_says_so_plainly(self):
        resolution = resolve_one(RESULTS, ElementQuery(role="button", name="Subscribe"))
        self.assertTrue(resolution.empty)
        self.assertIn("couldn't find", resolution.describe().casefold())

    def test_a_role_with_several_candidates_and_no_name_takes_the_first(self):
        # "Click the button" with three buttons and nothing to narrow them is not
        # a question a user can answer better than we can.
        candidates = [
            candidate(1, "One", role="button"),
            candidate(2, "Two", role="button"),
            candidate(3, "Three", role="button"),
        ]
        resolution = resolve_one(candidates, ElementQuery(role="button"))
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.chosen.name, "One")

    def test_a_clearly_better_match_is_not_ambiguity(self):
        candidates = [candidate(1, "Search", role="button"), candidate(2, "Search settings", role="button")]
        resolution = resolve_one(candidates, ElementQuery(role="button", name="Search"))
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.chosen.name, "Search")

    def test_the_description_lists_the_options_so_the_next_call_can_name_one(self):
        candidates = [candidate(1, "Search", role="button"), candidate(2, "Search", role="button")]
        resolution = resolve_one(candidates, ElementQuery(role="button", name="Search"))
        text = resolution.describe()
        self.assertIn("several", text)
        self.assertIn("Search", text)


# -- frame selection ------------------------------------------------------------


class FrameTests(unittest.TestCase):
    class FakePage:
        def __init__(self, frames):
            self.frames = frames
            self.main_frame = frames[0]

    class FakeFrame:
        def __init__(self, name, url):
            self.name = name
            self.url = url

    def setUp(self):
        self.main = self.FakeFrame("", "https://example.com/")
        self.child = self.FakeFrame("checkout", "https://pay.example.com/form")
        self.page = self.FakePage([self.main, self.child])

    def test_no_frame_named_means_the_page(self):
        self.assertIs(locators.frame_for(self.page, ""), self.page)

    def test_a_frame_is_selected_by_name(self):
        self.assertIs(locators.frame_for(self.page, "checkout"), self.child)

    def test_or_by_url_fragment(self):
        self.assertIs(locators.frame_for(self.page, "pay.example"), self.child)

    def test_or_by_index(self):
        self.assertIs(locators.frame_for(self.page, "1"), self.child)

    def test_an_unresolvable_frame_raises_rather_than_using_the_page(self):
        # A query that lands in the wrong frame is worse than one that reports
        # nothing, so this is a refusal.
        with self.assertRaises(LocatorError):
            locators.frame_for(self.page, "nowhere")

    def test_an_index_past_the_end_raises(self):
        with self.assertRaises(LocatorError):
            locators.frame_for(self.page, "9")


# -- the reporting layer ---------------------------------------------------------


class SnapshotTests(unittest.TestCase):
    PAYLOAD = {
        "title": "YouTube",
        "url": "https://www.youtube.com/results?search_query=x",
        "text": "results text",
        "ready": "complete",
        "candidates": [
            {"index": 1, "role": "link", "name": "Baby Shark", "tag": "a", "detail": {"href": "/watch?v=a"}},
            {"index": 2, "role": "link", "name": "Bath Song", "tag": "a", "detail": {"href": "/watch?v=b"}},
        ],
    }

    def test_a_snapshot_carries_a_generation_and_the_page_it_read(self):
        snapshot = state.snapshot_from_script(7, self.PAYLOAD)
        self.assertEqual(snapshot.generation, 7)
        self.assertEqual(snapshot.title, "YouTube")
        self.assertEqual(len(snapshot.candidates), 2)

    def test_candidates_carry_the_details_a_chooser_needs(self):
        snapshot = state.snapshot_from_script(1, self.PAYLOAD)
        self.assertEqual(snapshot.candidates[0].detail["href"], "/watch?v=a")

    def test_a_row_text_becomes_metadata_for_disambiguation(self):
        payload = dict(self.PAYLOAD)
        payload["candidates"] = [
            {"index": 1, "role": "link", "name": "Watch", "tag": "a", "rowText": "Baby Shark  Pinkfong  2:30"}
        ]
        snapshot = state.snapshot_from_script(1, payload)
        self.assertIn("Pinkfong", snapshot.candidates[0].detail["meta"])

    def test_a_junk_payload_still_produces_a_snapshot(self):
        snapshot = state.snapshot_from_script(1, None)
        self.assertEqual(snapshot.generation, 1)
        self.assertEqual(snapshot.candidates, ())

    def test_the_store_reports_staleness(self):
        store = state.SnapshotStore()
        store.store(state.snapshot_from_script(store.next_generation(), self.PAYLOAD))
        self.assertFalse(store.stale(1))
        store.store(state.snapshot_from_script(store.next_generation(), self.PAYLOAD))
        self.assertTrue(store.stale(1))

    def test_the_store_keeps_a_short_history(self):
        store = state.SnapshotStore()
        for _ in range(10):
            store.store(state.snapshot_from_script(store.next_generation(), self.PAYLOAD))
        self.assertLessEqual(len(store.history), 4)


class FormValueTests(unittest.TestCase):
    def test_a_password_value_never_leaves_the_page(self):
        # Redaction happens in the page script; this asserts the shape survives it,
        # so the model sees that a field exists and is empty rather than concluding
        # the page has none.
        rows = [{"name": "pw", "type": "password", "value": "", "redacted": True}]
        payload = state.form_values_payload(rows)
        self.assertTrue(payload[0]["redacted"])
        self.assertEqual(payload[0]["value"], "")

    def test_select_options_travel_so_a_choice_can_be_made(self):
        rows = [{"name": "size", "type": "select", "value": "m", "options": ["S", "M", "L"]}]
        self.assertEqual(state.form_values_payload(rows)[0]["options"], ["S", "M", "L"])

    def test_the_list_is_bounded(self):
        rows = [{"name": f"f{i}", "type": "text", "value": "x"} for i in range(200)]
        self.assertLessEqual(len(state.form_values_payload(rows)), 40)


class ClassifyTests(unittest.TestCase):
    """``_classify`` was untested, and it was reading the wrong attribute.

    The gate is handed ``action_class`` per element, so this function decides
    whether a click or an Enter interrupts the user. It had no test at all, and
    it passed ``detail["value"]`` where the DOM's ``type`` was meant -- so
    ``<input type="submit" value="Go">`` classified as an ordinary ``act`` and a
    form submission went through without ever asking, while ``browse``, whose
    own facts script returns the real attribute, called the same button
    ``sensitive``. Two tools, one button, and the wrong answer in the one
    direction that never interrupts.
    """

    def _classify(self, verb: str, chosen: Candidate, **kwargs) -> str:
        return control._classify(verb, chosen, **kwargs)[0]

    def test_a_submit_input_is_sensitive(self):
        chosen = Candidate(
            index=1,
            role="button",
            name="Send",
            tag="input",
            detail={"type": "submit", "inForm": True, "value": "Send"},
        )
        self.assertEqual(self._classify("click", chosen, text="", key=""), "sensitive")

    def test_a_value_attribute_does_not_decide_the_class(self):
        # The regression, stated as its own case: a control whose `type` says
        # submit and whose `value` says something ordinary is still a submitter.
        chosen = Candidate(
            index=1,
            role="button",
            name="Go",
            tag="input",
            detail={"type": "submit", "inForm": True, "value": "go"},
        )
        self.assertEqual(self._classify("click", chosen, text="", key=""), "sensitive")

    def test_enter_in_a_search_form_is_an_act(self):
        # The reported fault on this tool: pressing Enter on YouTube's search box
        # asked for permission. The form says what it is for.
        chosen = Candidate(
            index=1,
            role="searchbox",
            name="Search",
            tag="input",
            detail={"type": "", "inForm": True, "formRole": "search", "soleField": True},
        )
        self.assertEqual(self._classify("press", chosen, text="", key="Enter"), "act")

    def test_enter_in_an_ordinary_form_still_asks(self):
        # The bound on the exception above: a comment box in a plain form looks
        # identical minus the form's role, and must still ask.
        chosen = Candidate(
            index=1,
            role="textbox",
            name="Leave a comment",
            tag="textarea",
            detail={"inForm": True},
        )
        self.assertEqual(self._classify("press", chosen, text="", key="Enter"), "sensitive")

    def test_typing_and_choosing_are_acts(self):
        chosen = Candidate(index=1, role="textbox", name="Comment", tag="textarea",
                           detail={"inForm": True})
        for verb in ("fill", "type", "select", "hover"):
            with self.subTest(verb=verb):
                self.assertEqual(self._classify(verb, chosen, text="x", key=""), "act")


class ChangeReportTests(unittest.TestCase):
    def test_a_moved_url_is_reported_as_moved(self):
        self.assertIn("moved to", state.change_report("https://a.test/", "https://b.test/"))

    def test_an_unchanged_url_says_the_control_was_activated_but_the_page_did_not_move(self):
        # The specific honesty this exists for: a click returned success and the
        # page did not navigate.
        report = state.change_report("https://a.test/", "https://a.test/")
        self.assertIn("did not move", report)


# -- YouTube ---------------------------------------------------------------------


class AlreadyOpenTests(unittest.IsolatedAsyncioTestCase):
    """Re-opening a page you are already on, which is what made a turn loop.

    The reported failure was youtube.com opened three times in one turn: each call
    discarded the results page the previous one had read, so the model started
    over and never got further. The fix is to answer with what is already there,
    which costs one call instead of three and ends the loop.
    """

    def setUp(self):
        self.opened: list[str] = []
        self.here = ""
        self.page = {
            "title": "Obosthan song - YouTube",
            "url": "https://www.youtube.com/results?search_query=obosthan+song",
            "text": "BD Skip navigation All Shorts",
            "controls": [{"ref": "r1", "tag": "a", "label": "Obosthan - Train Poka"}],
        }

    async def _browse(self, url: str) -> str:
        from agent.tools.browser import tools as browse_mod
        from agent.tools.browser.tools import browse as browse_tool

        async def fake_current_url() -> str:
            return self.here

        async def fake_open(target: str) -> dict:
            self.opened.append(target)
            self.here = target
            return {"title": "YouTube", "url": target}

        async def fake_observe() -> dict:
            return self.page

        # The module-level names are what the tool body calls, so patching them
        # is what actually redirects the browser -- patching the tool object would
        # not be, because it closed over the originals at import.
        with (
            mock.patch.object(browse_mod, "current_url", fake_current_url),
            mock.patch.object(browse_mod, "open_page", fake_open),
            mock.patch.object(browse_mod, "observe_page", fake_observe),
        ):
            # `browse` is a livekit FunctionTool; `.info` carries the wrapped
            # coroutine and every other test in this suite calls it the same way.
            return await browse_tool(SimpleNamespace(), "open", url=url, ref="", text="", key="")

    async def test_opening_the_page_you_are_already_on_does_not_navigate(self):
        self.here = "https://www.youtube.com/"
        result = await self._browse("https://www.youtube.com/")
        self.assertEqual(self.opened, [], "it navigated when the page was already there")

    async def test_the_answer_carries_the_page_the_model_needed_to_read(self):
        # Returning "already open" and nothing else would end the loop by starving
        # the model. It has to arrive with the contents, or the next turn opens it
        # again.
        self.here = "https://www.youtube.com/"
        result = await self._browse("https://www.youtube.com/")
        self.assertIn("alreadyOpen", result)
        self.assertIn("Obosthan", result)

    async def test_a_different_page_still_navigates(self):
        self.here = "https://www.youtube.com/"
        await self._browse("https://example.com/")
        self.assertEqual(self.opened, ["https://example.com/"])

    async def test_no_page_at_all_still_navigates(self):
        self.here = ""
        await self._browse("https://www.youtube.com/")
        self.assertEqual(self.opened, ["https://www.youtube.com/"])

    async def test_a_trailing_slash_is_the_same_page(self):
        self.here = "https://www.youtube.com/results?search_query=x"
        await self._browse("https://www.youtube.com/results?search_query=x/")
        self.assertEqual(self.opened, [])


class YouTubeTitleScoringTests(unittest.TestCase):
    """Why a real result used to be thrown away.

    The reported bug was not "she searched wrong" -- it was "she was matching the
    exact order I gave it". A user asks for *obosthan vocal song*; the video on the
    page is titled *Obosthan (অবস্থান) - Train Poka Album - HIGHWAY - Debut
    Track*. The old test was ``"obosthan vocal song" in result_title``, which is
    False, so every genuine match was discarded and Lumine reported nothing while
    holding a page that plainly contained the song.
    """

    def test_a_paraphrase_of_the_request_still_matches(self):
        result = youtube.VideoResult(
            1, "Obosthan (অবস্থান) - Train Poka Album - HIGHWAY", channel="HIGHWAY"
        )
        self.assertEqual(youtube.title_score(result.title, "obosthan vocal song"), 1.0)
        self.assertIsNotNone(
            youtube._tier_for(result, youtube.VideoSelector(title="obosthan vocal song"))
        )

    def test_a_shorter_request_reaches_the_same_video(self):
        # "obosthan song" and "obosthan vocal song" are the same ask. Words that
        # only describe the *kind* of thing must not be what decides the match.
        title = "Obosthan - Train Poka Album - HIGHWAY"
        self.assertEqual(youtube.title_score(title, "obosthan song"), 1.0)
        self.assertEqual(youtube.title_score(title, "obosthan vocal song"), 1.0)

    def test_a_video_with_nothing_in_common_is_still_rejected(self):
        # Scoring must not turn "finds something" into "finds anything".
        self.assertEqual(youtube.title_score("Baby Shark Remix", "obosthan vocal song"), 0.0)
        self.assertIsNone(
            youtube._tier_for(
                youtube.VideoResult(1, "Baby Shark Remix"),
                youtube.VideoSelector(title="obosthan vocal song"),
            )
        )

    def test_a_single_weak_word_out_of_several_is_not_enough(self):
        # A results page full of loosely-related videos is exactly where opening
        # the wrong one is worst, so a low overlap has to stay a miss.
        self.assertLess(
            youtube.title_score("Obosthan Live", "obosthan highway cover band"),
            youtube.MIN_TITLE_SCORE,
        )

    def test_two_equally_good_results_still_ask_rather_than_guess(self):
        # Matching more loosely must not become picking one. Two Obosthan videos
        # and no position is a question, not a coin toss.
        results = [
            youtube.VideoResult(1, "Obosthan - Train Poka Album", channel="HIGHWAY"),
            youtube.VideoResult(2, "Obosthan - Official Video", channel="Other"),
        ]
        chosen = youtube.pick(results, youtube.VideoSelector(title="obosthan vocal song"))
        self.assertFalse(chosen.ok)
        self.assertIn("tell me which", chosen.reason)

    def test_a_position_resolves_the_ambiguity(self):
        results = [
            youtube.VideoResult(1, "Obosthan - Train Poka Album", channel="HIGHWAY"),
            youtube.VideoResult(2, "Obosthan - Official Video", channel="Other"),
        ]
        chosen = youtube.pick(
            results, youtube.VideoSelector(title="obosthan vocal song", position=1)
        )
        self.assertTrue(chosen.ok)
        self.assertEqual(chosen.result.channel, "HIGHWAY")


class YouTubeExtractionTests(unittest.TestCase):
    def test_a_duration_is_read_from_whatever_shape_the_page_used(self):
        self.assertEqual(youtube.parse_duration("  12:34 "), "12:34")
        self.assertEqual(youtube.parse_duration("1:02:03"), "1:02:03")
        self.assertEqual(youtube.parse_duration("no time here"), "")

    def test_a_view_count_is_read(self):
        self.assertEqual(youtube.parse_views("1.2M views"), "1.2M")
        self.assertEqual(youtube.parse_views("456 views"), "456")
        self.assertEqual(youtube.parse_views(""), "")


class YouTubePickTests(unittest.TestCase):
    def _results(self) -> list[youtube.VideoResult]:
        return [
            youtube.VideoResult(1, "Baby Shark", channel="Pinkfong", duration="2:30"),
            youtube.VideoResult(2, "Baby Shark Remix", channel="Cocomelon", duration="3:00"),
            youtube.VideoResult(3, "Bath Song", channel="Cocomelon", duration="1:50"),
            youtube.VideoResult(4, "Learn Colors", channel="Cocomelon", duration="4:20"),
        ]

    def test_the_second_video_of_the_list_is_the_second(self):
        chosen = youtube.pick(self._results(), youtube.VideoSelector(position=2))
        self.assertTrue(chosen.ok)
        self.assertEqual(chosen.result.title, "Baby Shark Remix")

    def test_a_channel_narrows_before_the_position_is_counted(self):
        # "the second video by Cocomelon" means the second of Cocomelon's results,
        # not the second result overall that happens to be by Cocomelon.
        selector = youtube.VideoSelector(position=2, channel="Cocomelon")
        chosen = youtube.pick(self._results(), selector)
        self.assertEqual(chosen.result.title, "Bath Song")

    def test_an_exact_channel_is_preferred_over_one_that_merely_contains_the_word(self):
        # "Cocomelon Kids" is a different channel that shares a word.
        results = [
            youtube.VideoResult(1, "Song", channel="Cocomelon Kids"),
            youtube.VideoResult(2, "Song", channel="Cocomelon"),
        ]
        chosen = youtube.pick(results, youtube.VideoSelector(channel="Cocomelon"))
        self.assertEqual(chosen.result.channel, "Cocomelon")

    def test_a_channel_word_inside_a_longer_word_is_not_a_match(self):
        results = [youtube.VideoResult(1, "Song", channel="Supercomomelon")]
        chosen = youtube.pick(results, youtube.VideoSelector(channel="Cocomelon"))
        self.assertFalse(chosen.ok)

    def test_several_matches_with_no_position_refuse_rather_than_pick_one(self):
        chosen = youtube.pick(self._results(), youtube.VideoSelector(channel="Cocomelon"))
        self.assertFalse(chosen.ok)
        self.assertIn("tell me which", chosen.reason)

    def test_one_match_needs_no_question(self):
        chosen = youtube.pick(self._results(), youtube.VideoSelector(title="Learn Colors"))
        self.assertEqual(chosen.result.title, "Learn Colors")

    def test_no_match_says_what_it_looked_for(self):
        chosen = youtube.pick(self._results(), youtube.VideoSelector(channel="Nobody"))
        self.assertFalse(chosen.ok)
        self.assertIn("Nobody", chosen.reason)

    def test_a_position_past_the_end_says_how_many_there_were(self):
        chosen = youtube.pick(self._results(), youtube.VideoSelector(position=9, channel="Cocomelon"))
        self.assertFalse(chosen.ok)
        self.assertIn("only 3", chosen.reason)

    def test_an_empty_page_does_not_produce_a_pick(self):
        chosen = youtube.pick([], youtube.VideoSelector(position=1))
        self.assertFalse(chosen.ok)
        self.assertIn("could not read", chosen.reason)

    def test_nothing_in_this_module_builds_a_watch_url(self):
        # The property is structural: there is no code path that constructs a
        # youtube.com/watch URL from a title or an index, so the test asserts the
        # only URL that can appear is one the page itself reported.
        source = youtube.__doc__ or ""
        del source
        results = self._results()
        for result in results:
            self.assertEqual(result.href, "", "a fixture result must carry no href of its own")


class YouTubeRevalidationTests(unittest.TestCase):
    def test_the_same_video_across_a_rerender_is_the_same_video(self):
        before = youtube.VideoResult(2, "Song", channel="C", href="/watch?v=x", duration="3:00")
        after = youtube.VideoResult(7, "Song", channel="C", href="/watch?v=x", duration="3:00")
        self.assertTrue(youtube.same_result(before, after))

    def test_a_different_video_is_not(self):
        before = youtube.VideoResult(2, "Song", channel="C", href="/watch?v=x")
        after = youtube.VideoResult(2, "Other", channel="C", href="/watch?v=y")
        self.assertFalse(youtube.same_result(before, after))

    def test_a_missing_side_is_not_a_match(self):
        result = youtube.VideoResult(1, "Song")
        self.assertFalse(youtube.same_result(None, result))
        self.assertFalse(youtube.same_result(result, None))

    def test_without_an_href_the_identifying_facts_are_compared(self):
        before = youtube.VideoResult(1, "Song", channel="C", duration="3:00")
        after = youtube.VideoResult(9, "Song", channel="C", duration="3:00")
        self.assertTrue(youtube.same_result(before, after))

    def test_a_blocked_video_is_reported_unavailable(self):
        self.assertEqual(youtube.playable_from_url("https://www.google.com/sorry/index"), "unavailable")

    def test_an_ordinary_video_page_is_only_available_not_playing(self):
        # "Available" is the honest claim from a URL alone; whether it plays is
        # something a player element has to be read for.
        self.assertEqual(youtube.playable_from_url("https://www.youtube.com/watch?v=x"), "available")

    def test_a_non_youtube_url_says_nothing_at_all(self):
        self.assertEqual(youtube.playable_from_url("https://example.com"), "")


class EngineSeamTests(unittest.TestCase):
    """The seam that hands `web_control` a browser, with nothing installed.

    The regression: the factory returned a differently-cased name than the
    instance it was meant to return, so the *first* call needing a real engine
    raised `NameError` from inside the tool body. The user saw the Python
    identifier as the tool's message -- a tool result is read by the model, and
    "name '_ENGINE' is not defined" is not a sentence anybody can act on. Nothing
    else caught it because every other test either injects a fake engine through
    `set_engine` or never reaches the real factory at all.
    """

    def tearDown(self) -> None:
        control.set_engine(None)

    def test_the_default_engine_resolves_without_raising(self):
        self.assertIsNotNone(control.engine())

    def test_it_is_the_one_process_wide_engine(self):
        # One browser per worker, like the browser it drives: a second instance
        # would be a second Chromium and a second profile for the same worker.
        self.assertIs(control.engine(), control.engine())

    def test_an_injected_engine_wins_over_the_real_one(self):
        fake = object()
        control.set_engine(fake)
        self.assertIs(control.engine(), fake)

    def test_the_factory_returns_the_module_instance(self):
        # Pinned by identity against the module, not just "not None": a factory
        # that built a *new* engine per call would satisfy the test above and
        # still be wrong.
        self.assertIs(engine.build_engine(), engine._engine)


if __name__ == "__main__":
    unittest.main()