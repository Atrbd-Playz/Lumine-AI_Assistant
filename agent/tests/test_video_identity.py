"""A made-up video id must not look like a working one.

This is the failure behind "she clicks the wrong URL and YouTube says the video
is not available". Three things have to be true at once, and each is a bug that
was invisible on its own:

* **YouTube's watch URL still works.** `/watch?v=<id>` is current, so the fix is
  not a different URL shape. That premise is asserted here so a future reader who
  suspects it has gone stale finds the question answered rather than re-litigated.
* **An invented id is indistinguishable from a real one *by shape*.** It is
  eleven URL-safe characters either way, and the page returns HTTP 200 with a
  full document. So nothing about the request says "I guessed this", and a
  session reports a working page while having navigated to nothing.
* **The only honest evidence is the player's own verdict.** The document
  loading proves nothing; `playabilityStatus` proves something.

The tests below pin the two paths apart, and pin that they are told apart *after*
the navigation rather than before it -- because a pre-flight refusal cannot
distinguish an invented id from one the user read out loud, and refusing the
second is worse than the original bug.
"""

from __future__ import annotations

import unittest
from unittest import mock

from agent.tools.browser import session as S

REAL_ID = "dQw4w9WgXcQ"  # a well-known, long-lived video
INVENTED_ID = "Xh1tKj0y_mQ"  # the id in the reported failure


def watch(video_id: str, host: str = "www.youtube.com") -> str:
    return f"https://{host}/watch?v={video_id}&list=RDAMVM{REAL_ID[:5]}"


class IdShapeTests(unittest.TestCase):
    def test_a_watch_url_yields_its_id(self):
        self.assertEqual(S.video_id_of(watch(REAL_ID)), REAL_ID)

    def test_a_search_url_yields_nothing(self):
        # The whole point of matching on the path: `/results?search_query=` has a
        # `v`-shaped query too, and treating a search page as a video would make
        # the verdict nonsense.
        self.assertEqual(S.video_id_of("https://www.youtube.com/results?search_query=x"), "")

    def test_another_host_is_not_a_video_page(self):
        # A lookalike must not be read as YouTube, or a 404 page on some other
        # domain could be reported as "this video is unavailable".
        self.assertEqual(S.video_id_of("https://notyoutube.com/watch?v=abcdefghijk"), "")

    def test_every_host_youtube_redirects_to_is_accepted(self):
        # youtube.com -> www.youtube.com and m.youtube.com are ordinary
        # redirects, and the page you land on is the page that answers.
        for host in S.YOUTUBE_HOSTS:
            self.assertEqual(S.video_id_of(watch(REAL_ID, host)), REAL_ID)

    def test_junk_does_not_raise(self):
        for value in ("", None, "not a url", "://", "watch?v="):
            self.assertIsInstance(S.video_id_of(value), str)

    def test_an_invented_id_is_the_right_shape_and_still_wrong(self):
        # The reason no input validation could ever have caught this: both are
        # eleven URL-safe characters. If this test ever fails, the *platform* has
        # changed and the diagnosis in this module needs revisiting.
        for video_id in (REAL_ID, INVENTED_ID):
            self.assertRegex(video_id, S.VIDEO_ID_RE)


class _FakePage:
    """A page that records navigation instead of doing it."""

    def __init__(self, url: str, playability: dict | None = None):
        self.url = url
        self._playability = playability or {}
        self.visited: list[str] = []

    async def goto(self, url: str, **_kwargs):
        self.visited.append(url)
        self.url = url

    async def evaluate(self, script, arg=None):
        return dict(self._playability)

    async def title(self):
        return self._playability.get("title") or "YouTube"


def _browser(page):
    """A `_Browser` whose page is ours and whose Chromium will never start.

    `ensure` is the entry point every operation goes through, so that is what is
    replaced. Patching the private `_ensure` underneath it looks equivalent and
    is not: the real `ensure` then launches a real Playwright, which in a unit
    test means a subprocess, a profile lock, and a suite that hangs.
    """
    browser = S._Browser()
    browser.ensure = mock.AsyncMock(return_value=page)
    return browser


class VerdictTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_real_video_is_playable_and_remembered(self):
        page = _FakePage(watch(REAL_ID), {"status": "OK", "videoId": REAL_ID, "title": "Never Gonna Give You Up"})
        browser = _browser(page)
        verdict = await browser._watch_verdict(page.url)
        self.assertEqual(verdict["verdict"], "playable")
        self.assertEqual(verdict["videoId"], REAL_ID)
        self.assertIn(REAL_ID, browser._seen_video_ids)

    async def test_an_invented_id_is_unavailable_not_merely_empty(self):
        page = _FakePage(watch(INVENTED_ID), {"status": "ERROR", "videoId": INVENTED_ID, "reason": "Video unavailable"})
        browser = _browser(page)
        verdict = await browser._watch_verdict(page.url)
        self.assertEqual(verdict["verdict"], "unavailable")
        # Never seen on a page -- so it was assembled, not read. This is the bit
        # that turns "YouTube is broken" into "I guessed the address".
        self.assertFalse(verdict["seen"])
        self.assertNotIn(INVENTED_ID, browser._seen_video_ids)

    async def test_a_removed_video_the_user_named_is_still_not_a_guess(self):
        # The case a pre-flight refusal gets wrong. The id came from the user, so
        # it was never seen on a page -- but the wording says the video is
        # private, which means it exists, so it is not a fabricated address and
        # the advice must not tell the model to go and search for it.
        page = _FakePage(watch(INVENTED_ID), {"status": "ERROR", "videoId": INVENTED_ID, "reason": "Private video"})
        browser = _browser(page)
        browser._seen_video_ids.add("someOtherId")
        verdict = await browser._watch_verdict(page.url)
        self.assertEqual(verdict["verdict"], "unavailable")
        self.assertFalse(verdict["seen"])
        self.assertFalse(verdict["invented"], "a private video was called a fabrication")
        self.assertEqual(verdict["reason"], "Private video")

    async def test_a_video_deleted_after_we_saw_it_is_not_a_fabrication(self):
        # The other half: an id read off a results page that has since been
        # removed. The id was real once, so the cause is the owner, not Lumine.
        page = _FakePage(watch(REAL_ID), {"status": "ERROR", "videoId": REAL_ID, "reason": "Video unavailable"})
        browser = _browser(page)
        browser._seen_video_ids.add(REAL_ID)
        verdict = await browser._watch_verdict(page.url)
        self.assertTrue(verdict["seen"])
        self.assertFalse(verdict["invented"])

    async def test_a_bare_unavailable_on_an_unseen_id_is_an_invention(self):
        # The exact reported symptom: eleven valid characters, a page that loads,
        # and a video that was never there.
        page = _FakePage(watch(INVENTED_ID), {"status": "ERROR", "videoId": INVENTED_ID, "reason": "Video unavailable"})
        browser = _browser(page)
        browser._seen_video_ids.add("aaaaaaaaaaa")
        verdict = await browser._watch_verdict(page.url)
        self.assertTrue(verdict["invented"])

    async def test_a_page_with_no_player_response_claims_nothing(self):
        # The unavailable page is localised and its text changes; the status does
        # not. When there is no status at all the honest answer is "I do not
        # know", because the document may simply still be booting.
        browser = _browser(_FakePage(watch(REAL_ID), {}))
        verdict = await browser._watch_verdict(watch(REAL_ID))
        self.assertEqual(verdict["verdict"], "unknown")

    async def test_a_page_we_cannot_read_claims_nothing(self):
        class Exploding:
            url = watch(REAL_ID)

            async def evaluate(self, *a, **k):
                raise RuntimeError("detached")

        browser = _browser(Exploding())
        self.assertEqual((await browser._watch_verdict(watch(REAL_ID)))["verdict"], "unknown")

    async def test_a_non_youtube_page_is_not_a_video_verdict(self):
        browser = _browser(_FakePage("https://example.com/"))
        self.assertEqual((await browser._watch_verdict("https://example.com/"))["verdict"], "other")


class OpenPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_opening_a_real_video_reports_it_as_playable(self):
        page = _FakePage(watch(REAL_ID), {"status": "OK", "videoId": REAL_ID, "title": "A Song"})
        browser = _browser(page)
        browser.allowed = mock.AsyncMock(return_value=(True, ""))
        result = await browser.open_page(watch(REAL_ID))
        self.assertEqual(result["video"]["verdict"], "playable")
        self.assertNotIn("notice", result)

    async def test_opening_an_invented_id_says_so_instead_of_looking_fine(self):
        # The regression this whole file exists for. Before, this returned a
        # normal `{title, url}` and the model reported a working page.
        page = _FakePage(watch(INVENTED_ID), {"status": "ERROR", "videoId": INVENTED_ID, "reason": "Video unavailable"})
        browser = _browser(page)
        browser.allowed = mock.AsyncMock(return_value=(True, ""))
        result = await browser.open_page(watch(INVENTED_ID))
        self.assertEqual(result["video"]["verdict"], "unavailable")
        self.assertIn("not a playable video", result["notice"])
        # The actionable half: this is the part that tells the model it invented
        # the address, instead of sending the user to debug YouTube.
        self.assertIn("assembled rather than read", result["notice"])
        self.assertIn("click the video", result["notice"])

    async def test_a_user_supplied_dead_link_is_reported_without_the_lecture(self):
        # Same verdict, different advice. Telling someone to search for a video
        # they just handed us would be worse than useless.
        page = _FakePage(watch(INVENTED_ID), {"status": "ERROR", "videoId": INVENTED_ID, "reason": "Private video"})
        browser = _browser(page)
        browser.allowed = mock.AsyncMock(return_value=(True, ""))
        browser._seen_video_ids.add("aaaaaaaaaaa")
        result = await browser.open_page(watch(INVENTED_ID))
        self.assertEqual(result["video"]["verdict"], "unavailable")
        self.assertNotIn("assembled rather than read", result["notice"])
        # ...and it says the true thing instead: the video exists, it is just not
        # viewable here. Advice that sends the user off to search for a link they
        # just handed over would be worse than no advice.
        self.assertIn("viewing restriction", result["notice"])

    async def test_an_ordinary_page_carries_no_video_fields(self):
        # Every site on the internet must still open normally and quietly.
        page = _FakePage("https://example.com/")
        browser = _browser(page)
        browser.allowed = mock.AsyncMock(return_value=(True, ""))
        result = await browser.open_page("https://example.com/")
        self.assertEqual(set(result), {"title", "url"})

    async def test_a_refused_address_is_still_refused_before_navigating(self):
        # The route guard runs first and is unchanged: no browser, no navigation.
        browser = _browser(_FakePage("about:blank"))
        browser.allowed = mock.AsyncMock(return_value=(False, "loopback is blocked"))
        with self.assertRaises(Exception) as caught:
            await browser.open_page("http://127.0.0.1/")
        self.assertIn("loopback is blocked", str(caught.exception))
        browser.ensure.assert_not_called()


class RememberTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_observe_gathers_the_ids_it_showed(self):
        # The evidence set, built from the only call that reads a whole document.
        # Without it, "assembled rather than read" can never be distinguished
        # from "the user gave me that link", because both ids are unseen.
        page = mock.Mock()

        async def _evaluate(_script, _gen):
            return {
                "title": "a song - YouTube",
                "url": "https://www.youtube.com/results?search_query=a+song",
                "text": "",
                "candidates": [
                    {"ref": "r1", "tag": "a", "detail": {"href": watch(REAL_ID)}},
                    {"ref": "r2", "tag": "a", "detail": {"href": watch(INVENTED_ID)}},
                    {"ref": "r3", "tag": "button", "detail": {"href": ""}},
                    {"ref": "r4", "tag": "a", "detail": {"href": "https://example.com/other"}},
                ],
                "items": [],
            }

        page.evaluate = _evaluate
        browser = _browser(page)
        await browser.observe_page()
        self.assertEqual(browser._seen_video_ids, {REAL_ID, INVENTED_ID})


if __name__ == "__main__":
    unittest.main()