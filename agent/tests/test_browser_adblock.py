"""Ad blocking: what is blocked, what must never be, and the switch.

The claim this file holds is narrow and it is the whole reason the module
exists: a block list that cannot be reasoned about is a list that will one day
stop a video from playing, and nobody will know why. So each rule is asserted
against the shape that made it necessary, and the two hosts whose absence is a
user-visible break -- YouTube's media and its thumbnails -- are asserted
*positively*, by name, rather than merely left out of the list.
"""

from __future__ import annotations

import unittest
from unittest import mock

from agent.tools.browser import adblock


class BlockedHostsTests(unittest.TestCase):
    """The list itself, and the boundary it matches on."""

    def test_the_usual_ad_and_tracker_hosts_are_blocked(self):
        for url in (
            "https://securepubads.g.doubleclick.net/tag/js/gpt.js",
            "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js",
            "https://static.doubleclick.net/instream/ad_status.js",
            "https://ad.doubleclick.net/demoad/view?x=1",
            "https://www.googletagmanager.com/gtm.js?id=GTM-XXXX",
            "https://analytics.google.com/g/collect?v=2",
            "https://connect.facebook.net/en_US/fbevents.js",
            "https://sb.scorecardresearch.com/beacon.js",
            "https://adservice.google.com/adsid/integrator.js",
            "https://cdn.taboola.com/libtrc/loader.js",
        ):
            with self.subTest(url=url):
                self.assertTrue(adblock.should_block(url), url)

    def test_a_host_that_merely_contains_a_listed_name_is_not_blocked(self):
        # The boundary is the host, not the string. A substring test would
        # block a real site whose name happens to contain "doubleclick", and
        # the failure mode of that is a page that silently does not work.
        for url in (
            "https://notdoubleclick.example.com/",
            "https://doubleclick.example.com/",
            "https://media.netflix.com/",  # "media.net" is listed; this is not it
        ):
            with self.subTest(url=url):
                self.assertFalse(adblock.should_block(url), url)

    def test_a_subdomain_of_a_listed_host_is_blocked(self):
        # The other half of the boundary: `adnxs.com` is on the list, and the
        # requests actually come from `secure.adnxs.com`.
        self.assertTrue(adblock.should_block("https://secure.adnxs.com/xhr"))

    def test_a_first_party_analytics_path_is_blocked_without_the_host(self):
        # A site's own domain serving an analytics script is caught by the
        # path, not the host -- so `https://example.com/gtm.js` is blocked
        # while `https://example.com/` is not.
        self.assertTrue(adblock.should_block("https://news.example.com/gtag/js?id=X"))
        self.assertFalse(adblock.should_block("https://news.example.com/article"))


class NeverBlockedTests(unittest.TestCase):
    """The hosts whose absence is a user-visible break, named positively.

    This is the assertion the ~49-second playback stop makes necessary. A list
    that blocked `*.googlevideo.com` would pass every test that does not play a
    video and fail the one case a person notices, so these are pinned by name
    rather than merely being absent from the block list.
    """

    def test_youtube_video_segments_are_never_blocked(self):
        for url in (
            "https://rr5---sn-abc123.googlevideo.com/videoplayback?mime=video%2Fmp4",
            "https://rr2---sn-def456.googlevideo.com/videoplayback",
        ):
            with self.subTest(url=url):
                self.assertFalse(adblock.should_block(url), url)

    def test_youtube_thumbnails_and_captions_are_never_blocked(self):
        for url in (
            "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
            "https://www.youtube.com/watch?v=abcdefghijk",
            "https://i1.ytimg.com/an_webp/abcdefghijk/mqdefault_60.webp",
        ):
            with self.subTest(url=url):
                self.assertFalse(adblock.should_block(url), url)

    def test_the_never_list_beats_a_broad_block(self):
        # Written as an ordering rule rather than a coincidence: if someone
        # adds `google.com` to the block list tomorrow, `youtube.com` still
        # works, because the never-list is consulted first.
        with mock.patch.object(adblock, "HOST_SUFFIXES", ("googlevideo.com",)):
            self.assertFalse(
                adblock.should_block("https://rr5---sn-x.googlevideo.com/videoplayback"),
                "the never-list must win over any block-list entry",
            )


class AdBlockSwitchTests(unittest.TestCase):
    """The escape hatch, and which way an unknown value falls."""

    def test_blocking_is_on_by_default(self):
        with mock.patch.dict("os.environ", {adblock.ADBLOCK_VAR: ""}, clear=False):
            self.assertTrue(adblock.enabled())

    def test_off_spellings_disable_it(self):
        for value in ("0", "false", "off", "no", " FALSE ", "Off"):
            with self.subTest(value=value):
                with mock.patch.dict("os.environ", {adblock.ADBLOCK_VAR: value}, clear=False):
                    self.assertFalse(adblock.enabled())
                    self.assertFalse(
                        adblock.should_block("https://doubleclick.net/x"),
                        f"setting {value!r} did not actually stop the blocking",
                    )

    def test_an_unrecognised_value_stays_on(self):
        # This key only ever *removes* something nobody asked for, so a typo
        # must not turn the blocker off -- that would read as "the blocker does
        # not work", which is the same class of mistake as the
        # `desktop.input` pair in the other direction.
        with mock.patch.dict("os.environ", {adblock.ADBLOCK_VAR: "maybe"}, clear=False):
            self.assertTrue(adblock.enabled())
            self.assertTrue(adblock.should_block("https://doubleclick.net/x"))


class PurityTests(unittest.TestCase):
    """`should_block` is pure over the URL, so it is testable without a browser."""

    def test_an_unparsable_or_empty_url_is_not_blocked(self):
        for url in ("", None, "not a url at all", "   "):
            with self.subTest(url=url):
                self.assertFalse(adblock.should_block(url))

    def test_a_url_with_no_host_is_not_blocked(self):
        self.assertFalse(adblock.should_block("https:///just-a-path"))


if __name__ == "__main__":
    unittest.main()
