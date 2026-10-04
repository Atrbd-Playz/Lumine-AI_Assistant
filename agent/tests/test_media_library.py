"""Tests for the music library: matching, the path boundary, and playback.

Split across four classes because they fail for four different reasons, and a
reader who has a red test wants to know which one:

- :class:`MatchingTests` -- "what did she hear?" Pure. No disk, no player.
- :class:`AmbiguityTests` -- "should she ask?" The rule that a refusal to guess
  is a *result*, not a failure.
- :class:`BoundaryTests` -- "which files?" The security surface. Traversal,
  absolute paths, symlinks, extensions, disabled configuration.
- :class:`PlaybackTests` -- "did it actually play?" With a fake player, because
  a test that opens a real one is a test that makes noise.

Every test here runs against a temporary directory. Nothing reaches the developer's
own music folder, which is the whole point of the allowlist and would be a poor
thing for a test suite to violate.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools.media import library as media_library  # noqa: E402
from agent.tools.media import match as media_match  # noqa: E402
from agent.tools.media import player as media_player  # noqa: E402
from agent.tools.media import queue as media_queue  # noqa: E402
from agent.tools.media import scanner as media_scanner  # noqa: E402
from agent.tools.media import state as media_state  # noqa: E402
from agent.tools.media.library import ALLOWED_SUFFIXES  # noqa: E402
from agent.tools.media.match import Track  # noqa: E402
from agent.tools.media.scanner import natural_key  # noqa: E402
from agent.tools.tools_compat import ToolError  # noqa: E402


def _library(root: Path, files: dict[str, str] | None = None) -> Path:
    """Build a library on disk. Keys are relative paths; ``""`` makes a folder."""
    for relative in files or {}:
        target = root / relative
        if relative.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(files[relative] or "audio", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Matching -- what was said versus what the file is called
# ---------------------------------------------------------------------------


class MatchingTests(unittest.TestCase):
    def test_noise_words_do_not_have_to_be_in_the_filename(self):
        """"play my vocal song" and "play love is gone" are the same request."""
        track = media_match.track_for("Love Is Gone (Vocal).mp3")
        self.assertIsNotNone(media_match.score("love is gone vocal", track))
        self.assertIsNotNone(media_match.score("play the song love is gone", track))
        self.assertIsNotNone(media_match.score("love is gone", track))
        # Ranked rather than merely found: both reach the track, and neither is
        # demoted for carrying or omitting a version word, because the tag carries
        # the version rather than the title.
        plain = media_match.rank("love is gone", [track])[0]
        qualified = media_match.rank("love is gone vocal", [track])[0]
        # Both are a full phrase match: the connective "is" is kept for the phrase
        # comparison even though it is not a coverage term, so "love is gone" is
        # the title rather than "love gone".
        self.assertEqual(plain.tier, 0, plain.match_word())
        self.assertEqual(qualified.tier, 0, qualified.match_word())
        self.assertEqual(plain.confidence, 1.0, plain.match_word())
        self.assertEqual(qualified.confidence, 1.0, qualified.match_word())

    def test_a_partial_match_needs_more_than_half_the_words(self):
        """One of two is a guess. One of three is a search."""
        two = Track(path="a", name="Boro Theko", folder="")
        self.assertIsNone(media_match.score("boro throat", two))
        three = Track(path="b", name="Boro Theko Live", folder="")
        self.assertIsNotNone(media_match.score("boro theko thor", three))

    def test_a_version_word_resolves_against_the_tag_not_the_title(self):
        """The bug that made "play the akhon ami vocal song" ambiguous.

        The file is "Husn (Without Music Vocals Only) _ Anuv Jain". Its *title* is
        "Husn Anuv Jain" -- "vocal" was deliberately removed from it -- so a matcher
        reading only the title reports the word as missing on a file that is a
        vocal. Against nine such files that is nine 67% matches and a question the
        user never needed to be asked.
        """
        track = media_match.track_for("Husn (Without Music Vocals Only) _ Anuv Jain.mp3")
        self.assertEqual(track.title, "Husn Anuv Jain")
        self.assertIn("vocal", track.tags)
        hit = media_match.score("play the husn vocal song", track)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.missing, ())
        self.assertEqual(hit.matched, ("husn",))
        # 0.95 rather than 1.0: every word was found, but not together as a phrase,
        # and "all the words separately" is a shade less than "the whole title".
        self.assertGreaterEqual(hit.confidence, 0.95, hit.match_word())
        self.assertTrue(hit.wanted_tag_hit)

    def test_a_tag_phrase_is_not_re_read_as_title_words(self):
        """"vocals only" must not leave "only" behind to match something else."""
        self.assertEqual(media_match.title_of("A (Vocals Only) B"), "A B")
        # And the query side: the phrase is answered by the tag, so it is not four
        # title terms that no file contains.
        self.assertEqual(media_match.significant_words("akhon ami without music vocal only"), ["akhon", "ami"])
        self.assertEqual(media_match.requested_tags("without music vocal only"), ("no-music", "vocal"))

    def test_tags_are_extracted_from_real_filenames(self):
        """Measured against a real library, not invented."""
        self.assertEqual(
            set(media_match.tags_of("Hoor - Without Music, Only Vocal ,Acapella _.mp3")),
            {"no-music", "vocal", "acapella"},
        )
        self.assertEqual(
            set(media_match.tags_of("Maand _ Bayaan _ Without Music _ Vocals Only.mp3")),
            {"no-music", "vocal"},
        )
        self.assertEqual(media_match.tags_of("Nahi Milta -by Bayaan - (Vocals Only).mp3"), ("vocal",))

    def test_asking_for_the_wrong_version_demotes_the_right_file(self):
        """A vocal file is not the answer to "the instrumental version".

        Demoted rather than dropped: it is a worse answer, not no answer, and
        offering it is information the user can act on.
        """
        vocal = media_match.track_for("Maand (Vocals Only).mp3")
        instrumental = media_match.track_for("Maand (Instrumental).mp3")
        ranked = media_match.rank("maand instrumental", [vocal, instrumental])
        self.assertEqual(ranked[0].track, instrumental)
        self.assertTrue(ranked[1].wanted_tag_miss)
        self.assertGreater(ranked[0].confidence, ranked[1].confidence)

    def test_a_single_folder_word_does_not_match_every_track(self):
        """A real bug, caught by the library being called "Lumine Media Folder".

        `"a" in "lumine media folder"` is true, so a substring folder check made
        "play a" match all nine files and the tool turned it into an ambiguity
        question about a letter.
        """
        library = Track(path="x", name="Akhon Ami", folder="Lumine Media Folder")
        self.assertIsNone(media_match.score("a", library))
        self.assertIsNotNone(media_match.score("media folder", library))

    def test_the_extension_is_never_a_matchable_word(self):
        """A bug found while building this: `"mp3"` became a token.

        `comparable_name` compared `rsplit(".")[-1]` -- which yields `"mp3"` --
        against an allowlist holding `".mp3"`, so it never stripped. The extension
        stayed in the name, and a query containing "mp3" then matched every file
        in the library.
        """
        self.assertEqual(media_match.comparable_name("Song.mp3"), "song")
        self.assertNotIn("mp", media_match._tokens(media_match.comparable_name("Song.mp3")))
        album = [Track(path="x", name="Song", folder="")]
        self.assertEqual(media_match.find("mp3", album), [])

    def test_a_misheard_word_still_finds_the_song(self):
        """The whole reason the fuzzy tier exists.

        Speech recognition does not spell. This is the same failure the WhatsApp
        contact matcher was built for -- "Jubayat" for "Jubayed" -- and the
        primitive is imported rather than rewritten, so the tolerance cannot drift.
        """
        track = Track(path="x", name="Jubayed Reza", folder="")
        self.assertIsNotNone(media_match.score("jubayat reza", track))

    def test_a_short_word_is_never_matched_approximately(self):
        """Below four characters, one edit is a different word, not a typo."""
        track = Track(path="x", name="Me2", folder="")
        self.assertIsNone(media_match.score("me", track))

    def test_a_literal_match_is_never_displaced_by_a_fuzzy_one(self):
        """Ordering is the point of the tier numbers."""
        exact = Track(path="a", name="Boro Theko", folder="")
        near = Track(path="b", name="Boro Theku", folder="")
        found = media_match.find("boro theko", [near, exact])
        self.assertEqual(found[0].path, "a")

    def test_bracket_qualifiers_become_tags_and_leave_the_display_name(self):
        """Two files differing only by `(Vocal)` are told apart by the *tag*, not by
        a bracket left in a name.

        Keeping the brackets was the earlier behaviour and it was worse in both
        directions: the name read "Love Is Gone (Vocal)" when the tag list already
        said "vocal", and a comma left stranded -- `Hoor, Only,` -- became part of a
        title. The name is now the song, and the version is a field.
        """
        track = media_match.track_for("Love Is Gone (Vocal).mp3")
        self.assertEqual(track.title, "Love Is Gone")
        self.assertEqual(track.tags, ("vocal",))
        self.assertEqual(track.name, "Love Is Gone")
        self.assertEqual(media_match.comparable_name("Love Is Gone (Vocal)"), "love is gone")

    def test_a_stranded_comma_does_not_become_part_of_a_title(self):
        """Measured: `Hoor - Without Music, Only Vocal ,Acapella` cleaned up to
        `Hoor , ,` and then compared as though the comma were the title."""
        self.assertEqual(media_match.title_of("Hoor - Without Music, Only Vocal ,Acapella _"), "Hoor")
        # `Hoor , ,` is one token, not three: the commas were punctuation.
        self.assertEqual(media_match.comparable_name("Hoor , ,"), "hoor")

    def test_a_trailing_number_is_not_part_of_the_phrase(self):
        """"Track 01" and "Track 1" are the same words written twice."""
        self.assertEqual(media_match.comparable_name("Track 01"), "track")
        self.assertEqual(media_match.comparable_name("Track 1"), "track")

    def test_a_pure_noise_query_matches_nothing_by_name_but_reaches_its_folder(self):
        """`vocal` is not noise any more -- it is a version tag, and a tag is an
        answer. What it must not do is count as a *title* word."""
        track = media_match.track_for("Boro Theko (Vocal).mp3")
        track = Track(path="x", name=track.name, folder="vocal", tags=("vocal",), title="Boro Theko")
        hit = media_match.score("vocal", track)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.wanted_tag_hit)

    def test_a_name_match_outranks_a_folder_match(self):
        """The ordering that stops a folder named `Love` hijacking `love is gone`."""
        in_folder = media_match.track_for("Something Else.mp3")
        in_folder = Track(**{**in_folder.__dict__, "folder": "Love"})
        by_name = media_match.track_for("Love Is Gone.mp3")
        by_name = Track(**{**by_name.__dict__, "folder": "Rock"})
        ranked = media_match.rank("love is gone", [in_folder, by_name])
        self.assertEqual(ranked[0].track.path, by_name.path)
        self.assertGreater(ranked[0].confidence, ranked[1].confidence)

    def test_folder_matching_does_not_fuzzy_match_every_file(self):
        """"play my vocal songs" names a folder, not forty files."""
        names = ["vocal", "live", "Rock"]
        self.assertEqual(media_match.best_folder("vocal", names), ["vocal"])
        self.assertEqual(media_match.best_folder("my vocal songs", names), ["vocal"])
        self.assertEqual(media_match.best_folder("bollywood", names), [])

    def test_ties_keep_the_order_a_person_would_see(self):
        """So "the second one" is the second one in Explorer."""
        tracks = [
            Track(path="1", name="Love Is Gone (Live)", folder=""),
            Track(path="2", name="Love Is Gone (Vocal)", folder=""),
        ]
        found = media_match.find("love is gone", tracks)
        self.assertEqual([item.path for item in found], ["1", "2"])

    def test_natural_sort_puts_track_two_before_track_ten(self):
        """`sorted()` plays an album in the wrong order and looks like a bug."""
        names = ["Track 10.mp3", "Track 2.mp3", "Track 1.mp3", "01 intro.mp3"]
        self.assertEqual(
            sorted(names, key=natural_key),
            ["01 intro.mp3", "Track 1.mp3", "Track 2.mp3", "Track 10.mp3"],
        )

    def test_natural_sort_is_case_insensitive(self):
        """Otherwise every capital letter sorts before every lower-case one."""
        self.assertEqual(sorted(["BANANA", "apple", "Cherry"], key=natural_key), ["apple", "BANANA", "Cherry"])

    def test_zero_padded_and_unpadded_numbers_sort_together(self):
        """`7` and `07` are the same position.

        Asserted on the keys rather than on a sorted list: Python's sort is
        stable, so equal keys keep the input order and the list would compare
        equal to its input no matter what the key actually was.
        """
        self.assertEqual(natural_key("7.mp3"), natural_key("07.mp3"))
        self.assertLess(natural_key("7.mp3"), natural_key("8.mp3"))
        # And therefore `01`, `02` come before `10` across the padding boundary.
        self.assertEqual(
            sorted(["10.mp3", "01.mp3", "2.mp3"], key=natural_key),
            ["01.mp3", "2.mp3", "10.mp3"],
        )


# ---------------------------------------------------------------------------
# Ambiguity -- a refusal to guess is a result
# ---------------------------------------------------------------------------


class AmbiguityTests(unittest.TestCase):
    def test_two_equal_matches_are_a_question_and_not_a_coin_toss(self):
        tracks = [
            Track(path="a", name="Love Is Gone (Live)", folder=""),
            Track(path="b", name="Love Is Gone (Vocal)", folder=""),
        ]
        choice = media_match.choose("love is gone", tracks)
        self.assertFalse(choice.ok)
        self.assertTrue(choice.ambiguous)
        self.assertEqual(len(choice.candidates), 2)

    def test_the_question_names_the_candidates(self):
        """A refusal nobody can act on is a dead end."""
        tracks = [
            Track(path="a", name="Love Is Gone (Live)", folder=""),
            Track(path="b", name="Love Is Gone (Vocal)", folder=""),
        ]
        message = media_match.ambiguous_message([item.name for item in tracks])
        self.assertIn("Love Is Gone (Live)", message)
        self.assertIn("more than one", message.casefold())

    def test_an_index_resolves_the_ambiguity_without_asking_again(self):
        tracks = [
            Track(path="a", name="Love Is Gone (Live)", folder=""),
            Track(path="b", name="Love Is Gone (Vocal)", folder=""),
        ]
        self.assertEqual(media_match.choose("love is gone", tracks, index=2).chosen.path, "b")

    def test_an_index_past_the_end_names_how_many_there_were(self):
        """"there are only 2 matches" is actionable; "invalid index" is not."""
        tracks = [
            Track(path="a", name="Love Is Gone (Live)", folder=""),
            Track(path="b", name="Love Is Gone (Vocal)", folder=""),
        ]
        choice = media_match.choose("love is gone", tracks, index=9)
        self.assertFalse(choice.ok)
        self.assertIn("2", choice.reason)

    def test_one_match_is_chosen_without_asking(self):
        choice = media_match.choose("love is gone", [Track(path="a", name="Love Is Gone", folder="")])
        self.assertTrue(choice.ok)
        self.assertFalse(choice.ambiguous)

    def test_the_ambiguous_list_is_capped(self):
        """Forty filenames is not something to read out over voice."""
        tracks = [Track(path=str(i), name=f"Love Is Gone {i}", folder="") for i in range(40)]
        self.assertLessEqual(len(media_match.ambiguous_message([t.name for t in tracks]).split(", ")), 5)


# ---------------------------------------------------------------------------
# The boundary -- which files may be played
# ---------------------------------------------------------------------------


class BoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.root = Path(self._temp.name) / "library"
        _library(
            self.root,
            {
                "Boro Theko.mp3": "audio",
                "album/": "",
                "album/02 Two.mp3": "audio",
                "album/01 One.mp3": "audio",
                "album/10 Ten.mp3": "audio",
                "notes.txt": "text",
                "cover.jpg": "image",
            },
        )
        self.outside = Path(self._temp.name) / "outside.mp3"
        self.outside.write_text("secret", encoding="utf-8")
        # **Both sources are isolated, and the document one matters.** `library_root`
        # reads the saved document first now, so a developer machine that has
        # genuinely chosen a music folder would otherwise hand its real path to
        # every test here -- which is how the first run of these failed, asserting
        # that a temporary library was the only one that existed.
        document = mock.patch.object(media_library, "_document_library", return_value="")
        document.start()
        self.addCleanup(document.stop)
        saved = mock.patch.dict(os.environ, {media_library.LIBRARY_VAR: str(self.root)})
        saved.start()
        self.addCleanup(saved.stop)

    def tearDown(self) -> None:
        self._temp.cleanup()

    # -- traversal --------------------------------------------------------

    def test_a_path_outside_the_library_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            media_library.resolve_track(self.outside)
        self.assertIn("inside your music folder", str(caught.exception))

    def test_dot_dot_cannot_climb_out(self):
        """The load-bearing case: resolved *first*, then compared."""
        escape = str(self.root / ".." / "outside.mp3")
        with self.assertRaises(ToolError):
            media_library.resolve_track(escape)

    def test_a_relative_path_is_judged_by_where_it_lands_not_by_its_text(self):
        """A relative path resolves against the working directory.

        So one that happens to sit inside the library is accepted, and one that
        does not is refused -- which is the rule. What this pins is that the
        judgement is *positional*: the same spelling is fine or not depending
        only on where it resolves. The working directory is moved rather than
        computed with `relpath`, because the temporary library and the repository
        can be on different drives and `relpath` raises rather than returning
        anything.
        """
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            self.assertEqual(media_library.resolve_track("Boro Theko.mp3"), (self.root / "Boro Theko.mp3").resolve())
            os.chdir(self.outside.parent)
            with self.assertRaises(ToolError):
                media_library.resolve_track(self.outside.name)
        finally:
            os.chdir(previous)

    def test_a_root_that_is_not_resolved_still_compares_correctly(self):
        """A bug found while writing these tests.

        `scan(root=...)` and `resolve_track(..., root=...)` took the caller's root
        as given while resolving the *child*, so `root in child.parents` was
        False for a path whose resolved form differed -- which on Windows is any
        path under a short `8.3` name. Every file in a good library was reported
        as outside it and the scan returned nothing, silently, reading exactly
        like an empty folder.
        """
        self.assertEqual(len(media_scanner.scan(root=self.root)), 4)
        self.assertEqual([name for name in media_scanner.folders(root=self.root)], ["album"])

    def test_a_url_is_refused_outright(self):
        with self.assertRaises(ToolError) as caught:
            media_library.resolve_track("https://example.com/song.mp3")
        self.assertIn("already on this computer", str(caught.exception))

    def test_a_symlink_pointing_out_of_the_library_is_refused(self):
        """Skipped where the OS cannot make one -- Windows without a developer
        mode, and any machine where the account may not create links."""
        link = self.root / "escape.mp3"
        try:
            link.symlink_to(self.outside)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks are not available here")
        with self.assertRaises(ToolError) as caught:
            media_library.resolve_track(link)
        self.assertIn("inside your music folder", str(caught.exception))

    def test_the_scan_does_not_follow_a_symlink_out_of_the_library(self):
        """A scan that trusted its own directory iterator would return this."""
        link = self.root / "escape.mp3"
        try:
            link.symlink_to(self.outside)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks are not available here")
        names = [Path(track.path).name for track in media_scanner.scan(root=self.root)]
        self.assertNotIn("escape.mp3", names)

    # -- file type -------------------------------------------------------

    def test_an_unsupported_extension_is_refused(self):
        """An extension not on the list is refused, not guessed at."""
        with self.assertRaises(ToolError) as caught:
            media_library.resolve_track(self.root / "cover.jpg")
        self.assertIn("kind of media file", str(caught.exception))

    def test_a_script_is_refused(self):
        """The allowlist is the only thing between a `.bat` and the OS handler."""
        evil = self.root / "run.bat"
        evil.write_text("echo no", encoding="utf-8")
        with self.assertRaises(ToolError):
            media_library.resolve_track(evil)

    def test_a_playlist_file_is_refused(self):
        """A playlist is a file of paths, so honouring one would hand playback a
        directory this module never checked."""
        playlist = self.root / "all.m3u"
        playlist.write_text("C:/anywhere/else.mp3\n", encoding="utf-8")
        self.assertNotIn(".m3u", ALLOWED_SUFFIXES)
        with self.assertRaises(ToolError):
            media_library.resolve_track(playlist)

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(ToolError):
            media_library.resolve_track(self.root / "nope.mp3")

    def test_a_zero_byte_file_is_refused(self):
        """A failed copy. Playing it opens a player showing an error."""
        empty = self.root / "empty.mp3"
        empty.write_bytes(b"")
        with self.assertRaises(ToolError):
            media_library.resolve_track(empty)

    def test_every_briefed_format_is_accepted(self):
        for suffix in (".mp3", ".m4a", ".flac", ".wav", ".opus", ".mp4", ".mkv"):
            with self.subTest(suffix=suffix):
                self.assertIn(suffix, ALLOWED_SUFFIXES)

    def test_the_allowlist_can_only_be_narrowed_never_widened(self):
        """A settings file must not be able to add `.exe` to it."""
        os.environ["LUMINE_MEDIA_EXTENSIONS"] = ".mp3,.exe,.bat"
        try:
            allowed = media_library.allowed_suffixes()
            self.assertEqual(allowed, frozenset({".mp3"}))
        finally:
            os.environ.pop("LUMINE_MEDIA_EXTENSIONS", None)

    def test_naming_a_narrower_allowlist_shrinks_the_scan(self):
        os.environ["LUMINE_MEDIA_EXTENSIONS"] = ".flac"
        try:
            names = [Path(t.path).name for t in media_scanner.scan(root=self.root)]
            self.assertEqual(names, [])
        finally:
            os.environ.pop("LUMINE_MEDIA_EXTENSIONS", None)

    # -- configuration ----------------------------------------------------

    def test_no_library_configured_is_not_the_same_as_an_empty_one(self):
        """One is a settings step nobody has done; the other is a search that found
        nothing. They need different sentences, so they are different values."""
        os.environ.pop(media_library.LIBRARY_VAR, None)
        self.assertIsNone(media_library.library_root())
        with self.assertRaises(ToolError) as caught:
            media_library.resolve_track(self.outside)
        self.assertIn("music folder", str(caught.exception))

    def test_a_library_is_available_by_default_but_the_document_can_switch_it_off(self):
        """On by default; the document still revokes it.

        It shipped off originally because a song keeps playing after the call
        ends. That is true and it is not a boundary problem: the capability can
        only open one file inside one allowlisted folder the user chose, and it
        cannot move, rename or delete anything. Requiring a second switch before an
        assistant will play a song was the wrong trade.
        """
        from agent.tools import tools_permission

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(
                tools_permission, "effective_document", return_value=({}, "test", [])
            ):
                self.assertEqual(tools_permission.media_disabled_reason(), "")
            with mock.patch.object(
                tools_permission,
                "effective_document",
                return_value=({"media": {"enabled": "off"}}, "test", []),
            ):
                self.assertIn("media.enabled", tools_permission.media_disabled_reason())

    def test_the_document_folder_reaches_the_worker(self):
        """The bug this whole read order exists for.

        The Music page writes `media.library` into the saved document. The worker
        read only the environment variable, so choosing a folder in the app did
        nothing for it -- five consecutive voice requests came back "You haven't
        picked a music folder yet" *with the tool visibly enabled*, which is the
        worst shape it could have taken.
        """
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(
                media_library,
                "_document_library",
                return_value=str(self.root),
            ):
                self.assertEqual(media_library.library_root(), self.root.resolve())

    def test_the_environment_is_the_fallback_not_the_only_source(self):
        # A machine configured by file still works, and a stale document value
        # falls through to it rather than silently disabling a configured library.
        with mock.patch.dict(
            os.environ, {media_library.LIBRARY_VAR: str(self.root)}, clear=True
        ):
            with mock.patch.object(media_library, "_document_library", return_value=""):
                self.assertEqual(media_library.library_root(), self.root.resolve())

    def test_a_folder_that_does_not_exist_reads_as_no_library(self):
        os.environ[media_library.LIBRARY_VAR] = str(self.root / "gone")
        self.assertIsNone(media_library.library_root())

    # -- what a result carries --------------------------------------------

    def test_a_track_never_carries_the_full_path_to_the_model_or_a_card(self):
        r"""`describe` is the only spelling used for both, and it is name + folder.

        A card carrying `C:\Users\me\Music\...` puts the user's directory layout
        in front of whoever is standing behind them, and nothing about deciding
        whether to play a song needs it.
        """
        target = self.root / "album" / "01 One.mp3"
        self.assertEqual(media_library.describe(target), "01 One (album)")
        self.assertNotIn(str(self.root), media_library.describe(target))

    def test_a_track_dict_is_name_and_folder_only(self):
        self.assertEqual(
            Track(path="C:/Music/album/x.mp3", name="x", folder="album").to_dict,
            {"name": "x", "folder": "album"},
        )


# ---------------------------------------------------------------------------
# Playback -- with a fake player, so nothing opens
# ---------------------------------------------------------------------------


class FakePlayer:
    """Records what it was asked to play, and can be told to fail."""

    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.played: list[str] = []
        self.fail = fail

    def play(self, path: Path) -> None:
        if self.fail:
            raise OSError("nothing on this computer opens that")
        self.played.append(str(path))


class PlaybackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.root = Path(self._temp.name) / "library"
        _library(
            self.root,
            {
                "album/01 One.mp3": "audio",
                "album/02 Two.mp3": "audio",
                "album/03 Three.mp3": "audio",
                "solo.mp3": "audio",
            },
        )
        media_state.forget()

    def tearDown(self) -> None:
        media_state.forget()
        self._temp.cleanup()

    def test_play_hands_the_file_to_the_backend_and_returns_its_name(self):
        player = FakePlayer()
        started = media_player.play(self.root / "solo.mp3", player=player)
        self.assertEqual(started, "fake")
        self.assertEqual(player.played, [str(self.root / "solo.mp3")])

    def test_a_failure_raises_rather_than_reporting_success(self):
        """Returning `""` on failure is the shape that produces "I played it" for a
        track that never opened -- the one claim the user notices immediately."""
        with self.assertRaises(OSError):
            media_player.play(self.root / "solo.mp3", player=FakePlayer(fail=True))

    def test_one_bad_file_does_not_lose_the_rest_of_a_folder(self):
        """A symlink deleted between the scan and here should not take the other
        nineteen with it.

        The tolerance moved rather than went away. `play_many` no longer launches
        per file -- there is one hand-over, so there is nothing to be tolerant
        *about* -- and the filtering happens one layer up, where a path is resolved
        and dropped before it ever reaches the queue. That is the better place for
        it: an unresolvable file cannot get into a playlist at all.
        """
        good = self.root / "album" / "01 One.mp3"
        doomed = self.root / "album" / "missing.mp3"
        resolved = []
        for candidate in (good, doomed):
            try:
                resolved.append(media_library.resolve_track(str(candidate), root=self.root))
            except Exception:  # noqa: BLE001 - the drop is the behaviour
                pass
        self.assertEqual(resolved, [good.resolve()])

    def test_a_queue_is_one_hand_over_and_not_one_launch_per_track(self):
        """The incident, pinned.

        "Play my vocal songs" launched all nine files at once: nine
        `os.startfile` calls, nine players on top of each other, nothing audible.
        `startfile` takes **one** file, so the fix cannot be to launch them in
        turn -- it has to be to write one ordered playlist and hand *that* over.
        Nine tracks now produce exactly one call to the player.
        """
        player = FakePlayer()
        tracks = sorted(self.root.glob("album/*.mp3"))
        started = media_player.play_many(tracks, player=player)

        self.assertEqual(len(started), 3)
        self.assertEqual(len(player.played), 1, "one queue, not one launch per track")
        queue = Path(player.played[0])
        self.assertEqual(queue.suffix, ".m3u8")

    def test_the_queue_holds_the_tracks_in_the_order_it_was_given(self):
        """Order is the entire point of a playlist, so it is asserted on the file's
        contents rather than on the return value -- the return value is the same
        three paths either way and would not catch a reversed queue."""
        player = FakePlayer()
        tracks = sorted(self.root.glob("album/*.mp3"))
        media_player.play_many(tracks, titles=[item.stem for item in tracks], player=player)

        lines = Path(player.played[0]).read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        entries = [line for line in lines if not line.startswith("#")]
        self.assertEqual(entries, [str(item.resolve()) for item in tracks])
        # A name per row, so the player's own list reads as songs and not as paths.
        self.assertIn("#EXTINF:-1,01 One", lines)

    def test_a_single_track_is_opened_directly_and_not_through_a_playlist(self):
        """One file in a queue is one more indirection for no benefit, and the tool
        reports `queued: false` for it -- so the queue path has to stay the
        multi-track path only or that report becomes a lie."""
        player = FakePlayer()
        media_player.play_many([self.root / "solo.mp3"], player=player)
        self.assertEqual(player.played, [str(self.root / "solo.mp3")])
        self.assertFalse(player.played[0].endswith(".m3u8"))

    def test_a_queue_that_cannot_be_opened_raises_rather_than_reporting_success(self):
        """`play_many` reporting `[]` is what produced "I played it" for a queue that
        never opened. There is one hand-over now, so there is one answer, and it is
        raised."""
        tracks = sorted(self.root.glob("album/*.mp3"))
        with self.assertRaises(OSError):
            media_player.play_many(tracks, player=FakePlayer(fail=True))

    def test_an_over_long_queue_is_refused_rather_than_launched(self):
        """A request with 200 items in it is a request Lumine misunderstood."""
        too_many = [Path(f"{i}.mp3") for i in range(media_player.MAX_QUEUE + 1)]
        with self.assertRaises(OSError):
            media_player.play_many(too_many, player=FakePlayer())

    def test_the_default_player_is_a_system_handler_and_nothing_bundled(self):
        """The brief: use what the machine has, ship no player of our own."""
        player = media_player.default_player()
        self.assertIsInstance(player, media_player.SystemPlayer)
        self.assertEqual(player.name, "system")

    def test_next_steps_to_the_track_after_the_last_one_played(self):
        """`startfile` cannot skip inside a running player -- and playing the next
        *file* is a real answer to what the user asked, not a refusal."""
        tracks = media_scanner.scan(root=self.root)
        in_album = [item for item in tracks if item.folder == "album"]
        self.assertEqual(
            [item.name for item in in_album],
            ["01 One", "02 Two", "03 Three"],
        )
        media_state.remember(in_album[0].path)
        self.assertEqual(media_state.last_folder(), "album")
        following = media_state.step(in_album, media_state.last_played(), forward=True)
        self.assertIsNotNone(following)
        assert following is not None
        self.assertEqual(following.name, "02 Two")

    def test_next_wraps_around_at_the_end(self):
        """A playlist that stops at its last track cannot be followed, and "next"
        from the end nearly always means back to the start."""
        tracks = media_scanner.scan(root=self.root)
        in_album = [item for item in tracks if item.folder == "album"]
        wrapped = media_state.step(in_album, in_album[-1].path, forward=True)
        assert wrapped is not None
        self.assertEqual(wrapped.name, "01 One")

    def test_previous_steps_backwards(self):
        tracks = media_scanner.scan(root=self.root)
        in_album = [item for item in tracks if item.folder == "album"]
        back = media_state.step(in_album, in_album[2].path, forward=False)
        assert back is not None
        self.assertEqual(back.name, "02 Two")

    def test_next_on_an_unknown_track_refuses_rather_than_restarting(self):
        """Returning the first track would silently restart a playlist the user
        thought they had finished."""
        tracks = media_scanner.scan(root=self.root)
        self.assertIsNone(media_state.step(tracks, "C:/elsewhere/gone.mp3"))

    def test_next_on_an_empty_folder_refuses(self):
        self.assertIsNone(media_state.step([], "anything"))


class QueueTests(unittest.TestCase):
    """The queue builder, on its own.

    A folder is handed over as **one playlist** rather than N launches, and this is
    the module both the voice path and the Music widget depend on. Every property
    here is one a player reads and Lumine does not control.
    """

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.root = Path(self._temp.name) / "library"
        _library(
            self.root,
            {
                "01 First.mp3": "audio",
                "02 Second.mp3": "audio",
                "03 Third.mp3": "audio",
            },
        )
        # A queue directory inside the temp tree, so a test never writes into the
        # real temp directory and cannot see another run's queues.
        self.queue_home = Path(self._temp.name) / "queues"
        self.addCleanup(self._temp.cleanup)
        patcher = mock.patch.dict(
            os.environ, {media_queue.QUEUE_DIR_VAR: str(self.queue_home)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_queue_is_a_real_m3u8_named_for_its_contents(self):
        """`.m3u8` is UTF-8, and `.m3u` is read as the local codepage by several
        players -- which turns a Bengali title into mojibake in the player's own
        list. The hash of the body makes the write idempotent, so asking for the
        same queue twice leaves one file rather than two."""
        paths = [self.root / name for name in ("01 First.mp3", "02 Second.mp3")]
        first = media_queue.build(paths, ["First", "Second"])
        second = media_queue.build(paths, ["First", "Second"])

        self.assertEqual(first, second)
        self.assertEqual(first.suffix, ".m3u8")
        self.assertTrue(first.read_text(encoding="utf-8").startswith("#EXTM3U"))

    def test_a_non_latin_title_survives_the_queue(self):
        """The user's own library, measured: `Akhon Ami এখন আমি Vocal song.mp3`. A
        title mangled here is a title mangled in the player's playlist."""
        title = "Akhon Ami এখন আমি Vocal song"
        queue = media_queue.build([self.root / "01 First.mp3"], [title])
        self.assertIn(title, queue.read_text(encoding="utf-8"))

    def test_a_newline_in_a_title_cannot_inject_a_second_directive(self):
        """There is no escape in this format, so a title with a newline in it would
        end its own `#EXTINF` and start another -- and a filename that a user
        actually has on disk could carry one."""
        queue = media_queue.build(
            [self.root / "01 First.mp3"], ["Two\n#EXTINF:-1,Injected.mp3"]
        )
        # Directives are line-based, so the property is that no *second line* begins
        # one -- not that the substring appears once. The injected text legitimately
        # survives as characters; what it must not do is stand up as its own entry.
        lines = queue.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(1 for line in lines if line.startswith("#EXTINF")), 1)
        self.assertNotIn("Injected.mp3", [line for line in lines if not line.startswith("#")])

    def test_the_order_given_is_the_order_written(self):
        """Natural-sorted by the caller, and a queue that reordered would be worse
        than nine separate launches: it would look right and be wrong."""
        forwards = [self.root / "01 First.mp3", self.root / "03 Third.mp3"]
        backwards = list(reversed(forwards))
        self.assertNotEqual(
            media_queue.read_titles(media_queue.build(forwards, ["A", "C"])),
            media_queue.read_titles(media_queue.build(backwards, ["C", "A"])),
        )

    def test_an_empty_queue_refuses_rather_than_writing_a_playlist_of_nothing(self):
        with self.assertRaises(Exception):
            media_queue.build([])

    def test_an_over_long_queue_is_refused_rather_than_written(self):
        too_many = [Path(f"{index}.mp3") for index in range(media_queue.MAX_TRACKS + 1)]
        with self.assertRaises(Exception):
            media_queue.build(too_many)

    def test_old_queues_are_pruned_and_the_new_one_survives(self):
        """A queue is a transient hand-off, so the app's directory should not grow
        by one file per song a person asks for over a week."""
        for index in range(media_queue.KEEP + 4):
            media_queue.build([self.root / "01 First.mp3"], [f"queue {index}"])

        left = list(media_queue.queue_dir().glob(f"lumine-*{media_queue.QUEUE_SUFFIX}"))
        self.assertLessEqual(len(left), media_queue.KEEP)

    def test_a_queue_is_never_written_inside_the_library(self):
        """It would be picked up by the next scan and offered to the user as a
        track, and a queue is not part of anybody's collection."""
        queue = media_queue.build([self.root / "01 First.mp3"], ["First"])
        self.assertFalse(
            media_queue.queue_dir().resolve().is_relative_to(self.root.resolve())
            if hasattr(Path, "is_relative_to")
            else not str(queue.resolve()).startswith(str(self.root.resolve())),
            "the queue lives outside the library",
        )


if __name__ == "__main__":
    unittest.main()
