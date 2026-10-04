"""Finding a track by what was *said*, the way a search engine finds a page.

## The problem this solves, stated as a real example

The library on the developer's machine holds nine files. Two of them are::

    Akhon Ami [Bengali] Vocal song.mp3
    Husn (Without Music Vocals Only) _ Anuv Jain.mp3

Say *"play the Akhon Ami vocal song"*. A substring test over the filename finds it
only if every word of the request is spelled the way the file spells it. But:

- "vocal song" describes a *kind* of file, not a title.
- "Without Music Vocals Only" is a **version tag** -- it is the same song with the
  instruments taken out, which is a different file and a different thing to play.
- `Bayaan` may be the artist and `Nahi Milta` the title, in either order.

So this ranks rather than matches, and it does so on three axes at once: how many
of the words you said appear in the file, how *exactly* they appear, and whether
the version you asked for is the version the file is.

## Why partial matching, and where its danger is

The brief was "if some words match, she should execute". That is right, and it is
also how a search engine behaves -- but a matcher that accepts one word out of six
will happily play the wrong song, and the user hears it immediately and stops
trusting the feature.

So **coverage is a number, not a boolean**, and two thresholds decide what happens:

- :data:`MIN_COVERAGE` -- below this, a track is not a result at all. "play a" and
  a stray vowel must not reach a library.
- :data:`AUTO_MARGIN` -- how far ahead the winner must be before she acts without
  asking. A clear winner is played; two close matches is a question, because one
  word of a spoken title is often wrong and *asking* costs one word back.

## What is deliberately borrowed

The spelling tolerance is **imported from `tools.messaging.whatsapp_flow`**, not
written again. That matcher was built for a measured failure -- "Jubayat" for a
contact named "Jubayed" -- and a second edit-distance implementation written for
filenames would be worse than the one already tuned against real speech output.

What is genuinely new here is filenames: noise words, version tags, and the
ranked comparison.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

try:
    from ..messaging.whatsapp_flow import (
        _edit_distance as _edit_distance,
        _same_word_spoken_two_ways as _same_word_spoken_two_ways,
        _tokens as _tokens,
    )
except ImportError:  # running as a top-level script with agent/ on sys.path
    from messaging.whatsapp_flow import (  # type: ignore[no-redef]
        _edit_distance as _edit_distance,
        _same_word_spoken_two_ways as _same_word_spoken_two_ways,
        _tokens as _tokens,
    )

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Words that describe a *kind* of media rather than the media itself.
#:
#: Stripped from the query before matching. "Play my vocal song" and "play love is
#: gone" must reach the same track, and the difference between them is one of these
#: words plus three real ones.
#:
#: ``vocal`` is **deliberately absent**. Every file in a vocal-only library carries
#: it, which is exactly what makes it a useful *tag* -- "play all my vocal songs"
#: is the request that selects the whole library, and stripping the word left that
#: query with nothing to match on and no results at all. It is a version tag now,
#: and the tag axis is where it is answered.
NOISE_WORDS = frozenset(
    {
        "song", "songs", "track", "tracks", "album", "play", "please", "start",
        "give", "put", "can", "you", "want", "would", "like", "hey", "ok",
        "okay", "now", "then", "me", "the", "my", "a", "an", "of", "in", "on",
        "to", "from", "and", "with", "by", "feat", "ft", "featuring", "some",
        "any", "all", "that", "this", "it", "there", "for", "at", "is", "are",
        "be", "i", "im", "music", "audio", "video", "only",
    }
)

#: Multi-word phrases that name a **version**, extracted as a unit.
#:
#: Order matters: the longer phrases are tried first, so "without music" is not
#: left as two loose words that some other file could match by accident.
VERSION_PHRASES: tuple[tuple[str, str], ...] = (
    ("without music", "no-music"),
    ("without the music", "no-music"),
    ("no music", "no-music"),
    ("vocals only", "vocal"),
    ("vocal only", "vocal"),
    ("only vocals", "vocal"),
    ("a cappella", "acapella"),
    ("acapella", "acapella"),
    ("instrumental version", "instrumental"),
    ("vocal version", "vocal"),
    ("vocal mix", "vocal"),
    ("bass boosted", "bass-boosted"),
    ("8d audio", "8d"),
)

#: Single words that name a version on their own.
#:
#: These are **kept in the name** rather than stripped, because in a library that
#: holds both a vocal and an instrumental of the same song they are the only thing
#: telling the two apart.
VERSION_WORDS: frozenset[str] = frozenset(
    {
        "vocal", "vocals", "instrumental", "acapella", "karaoke",
        "remix", "remixed", "acoustic", "unplugged", "live",
    }
)

#: Shortest a word may be before fuzzy matching is even attempted. Four characters
#: is where one edit stops being a different word and starts being a typo -- below
#: it, "me" is within one edit of most short names, and playing the wrong song is
#: the most expensive mistake this product can make.
_FUZZY_MIN_TOKEN = 4

#: Word tokens, split on everything that is not a letter or digit.
_TOKEN_SPLIT = re.compile(r"[^\w]+", re.UNICODE)

_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_TRAILING_NUMBER = re.compile(r"[\s._-]*\d+\s*$")
_SPACES = re.compile(r"\s+")

#: A run of digits at the start of a name -- "01 One.mp3", "02 - Two.mp3". Stripped
#: for comparison and **kept** in the display name, because "01" is what makes the
#: order the user arranged.
_LEADING_NUMBER = re.compile(r"^\s*\d+\s*[-_.)\]]?\s*")


# ---------------------------------------------------------------------------
# Thresholds -- the two numbers that decide whether she acts or asks
# ---------------------------------------------------------------------------

#: How much of what the user said has to be present before a track counts as a
#: partial result at all.
#:
#: Read as a floor and used **strictly**: more than half the words, not half of
#: them. Half-and-not-a-habit means 2 of 3 is fine and 3 of 4 is fine, but 1 of 2
#: is not -- and 1 of 2 is the case that actually bites. "play boro throat"
#: matches "Boro Theko" on "boro" and nothing else, and a matcher that accepts it
#: plays the wrong song to a user who only asked for half of it.
#:
#: A track whose *every* word matched is unaffected: that is a tier above this one,
#: and "play Bayaan" still reaches "Maand _ Bayaan" with no help from this number.
MIN_COVERAGE = 0.5

#: How far ahead the best match must be before she plays it without asking.
#:
#: Expressed in coverage points, so it means the same thing for a one-word query
#: and a four-word one. Two files both containing everything the user said are the
#: vocal and the instrumental of one song, and the only honest answer to that is to
#: ask which -- a question that costs the user one word and saves them from a song
#: they did not want.
AUTO_MARGIN = 0.25

#: Below this many query words, coverage is too coarse to be evidence of anything.
#: "play a", "play it" -- one short word is a shrug, not a search.
MIN_TERMS_FOR_PARTIAL = 2


# ---------------------------------------------------------------------------
# The vocabulary of a filename
# ---------------------------------------------------------------------------


def _extension_of(text: str) -> str:
    stem, dot, extension = text.rpartition(".")
    if dot and f".{extension.casefold()}" in _MEDIA_SUFFIXES:
        return stem
    return text


#: Filler that survives tag removal and means nothing on its own.
#:
#: "Hoor - Without Music, Only Vocal ,Acapella" has every version word removed and
#: leaves "Hoor Only" -- and "Only" is the tail of the phrase "Vocals Only", not a
#: word of the song. Left in, it is a token a query could match and a thing a
#: person would read as part of the title.
STRAY_WORDS: tuple[str, ...] = ("only", "version", "edit", "take")
STRAY_WORDS_SET: frozenset[str] = frozenset(STRAY_WORDS)


def _strip_tags(text: str) -> str:
    """Remove every recognised version phrase and word, plus the filler left over."""
    for phrase, _tag in VERSION_PHRASES:
        text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)
    for word in VERSION_WORDS:
        text = re.sub(rf"\b{re.escape(word)}\b", " ", text, flags=re.IGNORECASE)
    for word in STRAY_WORDS:
        text = re.sub(rf"\b{re.escape(word)}\b", " ", text, flags=re.IGNORECASE)
    return text


def display_name(filename: str) -> str:
    """The name a person would call the track, for saying out loud.

    Separators tidied, brackets and commas dropped, version words removed -- and
    the **leading track number kept**, because "02 Boro Theko" is how the user
    refers to it and how the folder is ordered, while "Boro Theko (Vocal)" is not
    what anyone calls it: the version is a field, and the name is the song.
    """
    text = _extension_of(str(filename or "").strip())
    text = _strip_tags(text)
    text = _BRACKETED.sub(" ", text)
    text = _SEPARATORS.sub(" ", text)
    return _STRANDED.sub("", _SPACES.sub(" ", text).strip())


def comparable_name(filename: str) -> str:
    """The name as it is *matched*: separators, brackets and numbers normalised.

    Separators collapse to single spaces because people do not say them: the file
    is `Nahi Milta -by Bayaan - (Vocals Only).mp3` and the request is "nahi milta
    by bayan". Bracketed and trailing numbers go because they are how the folder is
    *organised*, not part of what to say.
    """
    text = _extension_of(str(filename or ""))
    text = _BRACKETED.sub(" ", text)
    text = _SEPARATORS.sub(" ", text)
    text = _TRAILING_NUMBER.sub("", text)
    text = _LEADING_NUMBER.sub("", text)
    return _SPACES.sub(" ", text).strip().casefold()


#: Imported from the library module rather than repeated, so the two can never
#: disagree about what counts as media.
try:
    from .library import ALLOWED_SUFFIXES as _MEDIA_SUFFIXES
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools.media.library import ALLOWED_SUFFIXES as _MEDIA_SUFFIXES  # type: ignore[no-redef]

#: Everything that separates words in a filename people do not say.
#:
#: Commas are here and the original was wrong about them: `Hoor - Without Music,
#: Only Vocal ,Acapella` becomes `Hoor, Only,` once its version words are removed --
#: two words and a comma, which is not a title and matches nothing. A comma is
#: almost always punctuation in a filename rather than part of a name.
_SEPARATORS = re.compile(r"[\s._\-/\\,;:'\"()\[\]{}+]+")

#: Punctuation left stranded at either end after tags are removed.
_STRANDED = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)


def tags_of(filename: str) -> tuple[str, ...]:
    """The version tags in a filename, canonical and sorted.

    ``Husn (Without Music Vocals Only) _ Anuv Jain.mp3`` is ``("no-music", "vocal")``
    -- and those two words are *stripped* from the name it returns, because they
    are the same information twice: once as a tag and once as loose text that would
    match a file that is not a vocal.

    That stripping is why this is more than a label. A tag is a **filter**: a user
    who says "vocal" gets the vocal files and not the instrumental ones even when
    the instrumental has "vocal" somewhere else in its name in some other sense.
    """
    lowered = _SEPARATORS.sub(" ", _extension_of(str(filename or "")).casefold())
    found: set[str] = set()
    for phrase, tag in VERSION_PHRASES:
        if phrase in lowered:
            found.add(tag)
            # Remove the whole phrase so its individual words cannot also be read
            # as title words. "vocals only" must not leave "only" behind.
            lowered = lowered.replace(phrase, " ")
    for word in _tokens(lowered):
        if word in VERSION_WORDS:
            found.add("vocal" if word.startswith("vocal") else word)
            lowered = re.sub(rf"\b{re.escape(word)}\b", " ", lowered)
    return tuple(sorted(found))


def title_of(filename: str) -> str:
    """The name with its version tags removed -- what the song is actually called.

    "Akhon Ami vocal song" and "Akhon Ami without music vocal only" both come back
    as "Akhon Ami", which is the comparison that makes "play the akhon ami vocal
    song" reach either of them.

    The stranded-punctuation strip at the end is not cosmetic. ``Hoor - Without
    Music, Only Vocal ,Acapella`` has every version word removed and is left as
    ``Hoor , ,`` -- which tokenises to ``["hoor"]`` and compares as if it were the
    whole title, so a search for "hoor" scores it as a phrase match rather than the
    single word it is.
    """
    text = _extension_of(str(filename or ""))
    text = _strip_tags(text)
    text = _BRACKETED.sub(" ", text)
    text = _SEPARATORS.sub(" ", text)
    text = _TRAILING_NUMBER.sub("", text)
    text = _LEADING_NUMBER.sub("", text)
    return _STRANDED.sub("", _SPACES.sub(" ", text).strip())


def significant_words(text: str) -> list[str]:
    """The **title** words of ``text``, in order, de-duplicated.

    Version words are removed here, and that is the whole reason the tag axis
    exists. ``"akhon ami without music vocal only"`` is two terms plus two tags,
    not six terms -- and as six terms it could never reach anything, because no
    title contains the word "without".

    Removing ``vocal`` here does **not** lose the information: a track's tags are
    part of what a query word is matched against (:func:`_searchable`), so "play the
    husn vocal song" still requires the file to be a vocal. The term list is only
    ever about *names*.
    """
    lowered = _SEPARATORS.sub(" ", str(text or "").casefold())
    for phrase, _tag in VERSION_PHRASES:
        lowered = lowered.replace(phrase, " ")
    for word in VERSION_WORDS | STRAY_WORDS_SET:
        lowered = re.sub(rf"\b{re.escape(word)}\b", " ", lowered)
    seen: set[str] = set()
    out: list[str] = []
    for word in _tokens(lowered):
        if len(word) < 2 or word in NOISE_WORDS or word in seen:
            continue
        seen.add(word)
        out.append(word)
    return out


def _searchable(track: Track) -> str:
    """Everything a query word may match: the title, then the tags, then the folder.

    **This is what makes a version word resolvable.** The file is "Husn (Without
    Music Vocals Only) _ Anuv Jain"; its title is "Husn Anuv Jain" and its tag is
    ``vocal``. A user who says "play the husn vocal song" names a word that is not
    in the title *because it was deliberately removed from it* -- so a matcher that
    only reads the title reports `vocal` as missing on a file that is a vocal. That
    single omission is what made "play the akhon ami vocal song" ambiguous between
    nine files that are all vocals.
    """
    parts = [track.title or track.name, *track.tags]
    if track.folder:
        parts.append(track.folder)
    return " ".join(parts)


def requested_tags(text: str) -> tuple[str, ...]:
    """The version tags the user asked for, canonical and sorted.

    "the akhon ami vocal song" asks for ``("vocal",)``; "play something without
    music" asks for ``("no-music",)``. Reading the tag axis out of the *query* is
    what lets a version word rank results instead of being thrown away.
    """
    lowered = _SEPARATORS.sub(" ", str(text or "").casefold())
    found: set[str] = set()
    for phrase, tag in VERSION_PHRASES:
        if phrase in lowered:
            found.add(tag)
    for word in _tokens(lowered):
        if word in VERSION_WORDS:
            found.add("vocal" if word.startswith("vocal") else word)
    return tuple(sorted(found))


def _words_present(wanted: Sequence[str], text: str) -> bool:
    """Whether every one of ``wanted`` finds a near-partner in ``text``."""
    have = _tokens(text)
    if not wanted or not have:
        return False
    return all(any(_same_word_spoken_two_ways(word, other) for other in have) for word in wanted)


def _matched_words(wanted: Sequence[str], text: str) -> tuple[list[str], list[str]]:
    """Which of ``wanted`` appear in ``text``, and which do not.

    The split is the whole of partial matching, and it is returned rather than
    collapsed to a bool so the caller can *rank* on it.
    """
    have = _tokens(text)
    hit: list[str] = []
    miss: list[str] = []
    for word in wanted:
        if any(_same_word_spoken_two_ways(word, other) for other in have):
            hit.append(word)
        else:
            miss.append(word)
    return hit, miss


# ---------------------------------------------------------------------------
# A track, and a scored hit on one
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Track:
    """One playable file, with the facts a spoken line and a card need."""

    path: str
    name: str
    #: The immediate parent folder's name. Empty for a file at the library root.
    folder: str = ""
    #: The version tags in the filename -- "vocal", "no-music", "acapella".
    tags: tuple[str, ...] = ()
    #: The filename with its tags removed: the song's own name.
    title: str = ""

    @property
    def to_dict(self) -> dict[str, str]:
        out = {"name": self.name}
        if self.folder:
            out["folder"] = self.folder
        if self.tags:
            out["tags"] = ", ".join(self.tags)
        return out


#: How much a hit in each tier can be believed, before coverage is applied.
#:
#: **The tiers are not comparable to each other on coverage alone**, which was the
#: bug that made this ask far too often. A tag-only hit carries coverage 1.0 --
#: every word of "play all my vocal songs" was noise and the tag answered it -- and
#: a 67%-covered *title* match was losing to it on a straight subtraction. That is
#: backwards: knowing a file is a vocal is a filter, not a title match, and it must
#: never outrank a file whose name is what the user said.
TIER_TRUST: dict[int, float] = {
    0: 1.00,  # the whole phrase, exactly
    1: 1.00,  # the phrase at the start
    2: 0.98,  # the phrase somewhere in it
    3: 0.95,  # every word, but not together
    4: 0.85,  # some of the words
    5: 0.50,  # a version tag only
    6: 0.40,  # a folder name only
}


@dataclass(frozen=True)
class Hit:
    """One scored result: a track, and why it scored what it did.

    Every field is carried out to the model rather than collapsed into one number,
    because a result the model cannot explain is a result it will not trust: "I
    matched 'akhon' and 'ami'" is a claim the user can correct, and "the top result"
    is not.
    """

    track: Track
    #: 0 exact phrase, 1 prefix, 2 containment, 3 all words, 4 some words,
    #: 5 tags only, 6 folder only. Demoted results carry tier + 10.
    tier: int
    #: How much of what the user said this track accounts for, 0..1.
    coverage: float
    matched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    #: True when the user asked for a version and this track is it.
    wanted_tag_hit: bool = False
    #: True when the user asked for a version and this track is *not* it. The one
    #: thing that demotes a track hard: a vocal file must not answer a request for
    #: the instrumental.
    wanted_tag_miss: bool = False

    @property
    def confidence(self) -> float:
        """One comparable number, 0..1, for deciding whether to act or ask.

        **This exists so the comparison is like-for-like.** Deciding "is the winner
        clearly ahead?" by subtracting two coverages compares a title match against
        a tag match as though they were the same kind of evidence, and the tag
        always won -- which is how a search that could name the song with
        certainty asked the user to choose between nine files that all matched it
        equally well.

        A demoted result -- one that is the wrong *version* of what was asked for --
        keeps a share of its score rather than being erased: an instrumental really
        is a worse answer to "the vocal version" and not *no* answer, and offering
        it is information.
        """
        trust = TIER_TRUST.get(self.tier % 10, 0.30)
        score = trust * self.coverage
        if self.wanted_tag_miss and self.tier % 10 < 5:
            score *= 0.45
        return round(score, 4)

    def match_word(self) -> str:
        """The match as a short phrase, for a sentence a person can correct.

        "100% on 'akhon', 'ami', 'vocal'" is a claim the model can check against
        what the user said. "the top result" is not, and a model that cannot check
        its own reasoning will happily present it as confident.
        """
        if self.matched and self.missing:
            return f"{round(self.coverage * 100)}% -- matched {', '.join(self.matched)}, not {', '.join(self.missing)}"
        if self.matched:
            return f"all of {', '.join(self.matched)}"
        if self.wanted_tag_hit:
            return f"everything is tagged {', '.join(self.track.tags)}"
        if self.track.folder:
            return f"in the folder {self.track.folder}"
        return f"{round(self.coverage * 100)}%"

    @property
    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "name": self.track.name,
            # A percentage rather than a float at full precision: same information,
            # fewer tokens, and it reads as a proportion to a language model.
            "match": f"{round(self.coverage * 100)}%",
        }
        if self.track.folder:
            out["folder"] = self.track.folder
        if self.track.tags:
            out["tags"] = ", ".join(self.track.tags)
        if self.matched:
            out["matched"] = list(self.matched)
        if self.missing:
            out["missing"] = list(self.missing)
        return out


def track_for(path: object) -> Track:
    """A :class:`Track` from a path, with its tags and its real title."""
    from pathlib import Path

    p = Path(str(path))
    parent = p.parent.name if p.parent and p.parent.name else ""
    stem = p.stem
    return Track(
        path=str(p),
        name=display_name(stem),
        folder=parent,
        tags=tags_of(stem),
        title=title_of(stem) or display_name(stem),
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score(query: str, track: Track) -> Hit | None:
    """Score one track against one request, or ``None`` if it is not a result.

    Three axes, in the order they can veto each other:

    1. **Tag agreement.** A request for "vocal" is demoted against an instrumental
       even if every word of the title matched. This is the only hard-ish veto, and
       it comes first because it is the one that plays the wrong *version*.
    2. **Word coverage.** How many of the words the user said appear anywhere in
       the track -- title, tags or folder -- counting a word as present when it is
       spelled within one or two edits.
    3. **Exactness.** Whether the whole phrase, or all of it, appears together,
       which is what separates "borotheko" from a file that happens to contain
       "boro" and "theko" with other words between them.
    """
    raw = _SEPARATORS.sub(" ", str(query or "").strip().casefold())
    if not raw:
        return None

    wanted_tags = requested_tags(raw)
    tag_hit = bool(wanted_tags) and bool(set(wanted_tags) & set(track.tags))
    tag_miss = bool(wanted_tags) and not tag_hit

    terms = significant_words(raw)
    title = comparable_name(track.title or track.name)

    matched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    coverage = 0.0
    tier: int | None = None

    if terms and title:
        # Matched against title **and tags**, not the title alone. See
        # `_searchable` for why that is the whole difference between finding a
        # song by name and asking which of nine files the user meant.
        searchable = _searchable(track)
        hit, miss = _matched_words(terms, searchable)
        matched, missing = tuple(hit), tuple(miss)
        coverage = len(hit) / len(terms)

        # Exactness is judged on the **title**, because a tag is not part of a
        # phrase. "akhon ami vocal" is not the title of the file, and scoring it as
        # though it were would put a tag next to a song name.
        #
        # The phrase keeps the query's *connective* words -- "love is gone" is one
        # phrase, and dropping "is" made it "love gone", which matches nothing and
        # cost the strongest tier on every title with a small word in it.
        phrase = " ".join(
            word for word in _tokens(raw) if word not in VERSION_WORDS
        )
        if phrase and title == phrase:
            tier = 0
        elif phrase and title.startswith(phrase):
            tier = 1
        elif phrase and phrase in title:
            tier = 2
        elif not miss:
            tier = 3
        elif coverage > MIN_COVERAGE and len(terms) >= MIN_TERMS_FOR_PARTIAL:
            # The partial tier. **Strictly more than half the words, and only for
            # two or more of them.** One word of six is a guess and a guess is
            # what this module exists to avoid; one of two is the same guess with
            # a smaller denominator.
            tier = 4

    if tier is None and wanted_tags and tag_hit and track.tags:
        # "play all my vocal songs" -- every word was noise and every file carries
        # the tag. Without this the request had no terms, so no results, so the
        # library looked empty to the one query that describes it best.
        tier = 5
        coverage = 1.0

    if tier is None:
        # Last resort: the *folder* is the playlist and the user named it.
        #
        # Matched by **word**, not by substring, and that is not a detail. The
        # library on the developer's machine is called "Lumine Media Folder", so
        # `"a" in "lumine media folder"` is true -- and a raw-substring folder
        # check made the one-character query "play a" match all nine files, which
        # the tool then had to turn into an ambiguity question. A folder is named
        # with the words people say ("vocal", "live", "2019"), so a whole word from
        # the query appearing in the folder name is the test; the exact-name case
        # is kept because "play the media folder" has no other words to offer.
        folder = comparable_name(track.folder) if track.folder else ""
        if folder:
            exact = folder == raw or folder == _STRANDED.sub("", raw)
            folder_terms = significant_words(raw)
            landed, _not_landed = _matched_words(folder_terms, folder)
            if exact or landed:
                tier = 6
                coverage = 1.0 if exact else len(landed) / max(1, len(folder_terms))

    if tier is None:
        return None
    if tag_miss and tier < 5:
        # Demoted rather than dropped: a user who names a track exactly should get
        # it even while saying "vocal" about something they meant loosely. The tier
        # is pushed past every real match so it can only ever be the runner-up.
        tier += 10

    return Hit(
        track=track,
        tier=tier,
        coverage=coverage,
        matched=matched,
        missing=missing,
        wanted_tag_hit=tag_hit,
        wanted_tag_miss=tag_miss,
    )


def rank(query: object, tracks: Sequence[Track], *, limit: int = 6) -> list[Hit]:
    """The results for ``query``, best first.

    Ordered by :attr:`Hit.confidence`, then by tier, then by the order the scan
    produced -- which is natural sort within a folder and folders in name order, so
    "the second one" is the second one the user would see in Explorer.

    Confidence first because it is the only number that means the same thing across
    tiers; tier breaks ties because two hits at the same score should prefer the
    more exact kind of match.
    """
    text = _SEPARATORS.sub(" ", str(query or "").strip().casefold())
    if not text:
        return []
    hits: list[tuple[float, int, int, Hit]] = []
    for position, track in enumerate(tracks):
        hit = score(text, track)
        if hit is None:
            continue
        hits.append((-hit.confidence, hit.tier, position, hit))
    hits.sort(key=lambda row: (row[0], row[1], row[2]))
    return [hit for _c, _t, _p, hit in hits[: max(0, limit)]]


def find(query: object, tracks: Sequence[Track], *, limit: int = 6) -> list[Track]:
    """:func:`rank`, as bare tracks. For callers that do not care why."""
    return [hit.track for hit in rank(query, tracks, limit=limit)]


# ---------------------------------------------------------------------------
# Choosing, or asking
# ---------------------------------------------------------------------------


@dataclass
class Choice:
    """Which track was chosen, or why not.

    A refusal to guess is a *result*, not a failure: "two songs are called Love Is
    Gone -- which one?" costs the user one word and is better than playing the
    wrong song, which they would hear immediately and distrust afterwards.
    """

    chosen: Track | None
    candidates: tuple[Track, ...] = ()
    ambiguous: bool = False
    reason: str = ""
    #: The runner-up's coverage, so a caller can explain *how* close it was.
    margin: float = 0.0
    #: Why the winner won, carried through so the model can say it out loud.
    matched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.chosen is not None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"resolved": self.ok}
        if self.chosen is not None:
            out["chosen"] = self.chosen.to_dict
        if self.ambiguous:
            out["ambiguous"] = True
            out["candidates"] = [item.name for item in self.candidates[:5]]
        if self.reason:
            out["reason"] = self.reason
        if self.matched:
            out["matched"] = list(self.matched)
        return out


def choose(query: object, tracks: Sequence[Track], *, index: int = 0) -> Choice:
    """Resolve a spoken request to one track.

    **The decision rule, and it is two numbers.** She plays the top hit when it is
    clearly ahead -- :data:`AUTO_MARGIN` of coverage over the runner-up, or alone
    in the result set -- and asks when two candidates are close. Close is the
    dangerous case and the common one: a vocal and an instrumental of the same song
    both contain every word the user said, and only they know which they meant.

    ``index`` is 1-based and counts over the *results*, not over the library -- "play
    the second one" means the second result, which is what the user could see. Out
    of range is a sentence naming how many there were, because "there are only 2
    matches" is actionable and "invalid index" is not.
    """
    ranked = rank(query, tracks)
    if not ranked:
        return Choice(None, reason="nothing in your music folder matched that")

    if index and index > 0:
        if index > len(ranked):
            return Choice(
                None,
                candidates=tuple(hit.track for hit in ranked),
                reason=f"there are only {len(ranked)} matching track(s), not {index}",
            )
        picked = ranked[index - 1]
        return Choice(
            picked.track,
            candidates=tuple(hit.track for hit in ranked),
            matched=picked.matched,
            missing=picked.missing,
        )

    best = ranked[0]
    if len(ranked) == 1:
        return Choice(
            best.track,
            candidates=(best.track,),
            margin=1.0,
            matched=best.matched,
            missing=best.missing,
        )

    runner_up = ranked[1]
    # **Compared on confidence, not coverage.** See `Hit.confidence`: subtracting
    # two coverages compares a title match against a tag match as if they were the
    # same kind of evidence, and the tag won every time -- which made "play the
    # akhon ami vocal song" ask the user to choose between nine files that all
    # matched it exactly as well.
    margin = best.confidence - runner_up.confidence
    # A tag mismatch is a demotion, not a defeat: an instrumental *is* an answer
    # when the user asked for a vocal, just a worse one, and it should be offered
    # rather than hidden -- "I have the instrumental too" is useful information.
    clear = margin >= AUTO_MARGIN or (
        best.tier < runner_up.tier and not runner_up.wanted_tag_hit
    )
    if clear:
        return Choice(
            best.track,
            candidates=tuple(hit.track for hit in ranked),
            margin=margin,
            matched=best.matched,
            missing=best.missing,
        )
    return Choice(
        None,
        candidates=tuple(hit.track for hit in ranked),
        ambiguous=True,
        reason="more than one track matches that",
        margin=margin,
        matched=best.matched,
        missing=best.missing,
    )


def ambiguous_message(names: Sequence[str], *, limit: int = 4) -> str:
    """What to say when the request was ambiguous, naming the candidates.

    A refusal nobody can act on is a dead end, so the options are named -- and the
    count is capped, because a results page is not something to read out over
    voice.
    """
    shown = [str(name)[:40] for name in names[:limit]]
    return "More than one track matches that: " + ", ".join(shown) + ". Which one?"


#: Kept because the edit-distance primitive is imported above and a reader arriving
#: at this module should see that the tolerance is shared, not local.
__all__ = [
    "AUTO_MARGIN",
    "Choice",
    "Hit",
    "MIN_COVERAGE",
    "Track",
    "ambiguous_message",
    "best_folder",
    "choose",
    "comparable_name",
    "display_name",
    "find",
    "rank",
    "requested_tags",
    "score",
    "significant_words",
    "tags_of",
    "title_of",
    "track_for",
    "_edit_distance",
    "_same_word_spoken_two_ways",
]


def best_folder(query: object, folder_names: Sequence[str], *, limit: int = 6) -> list[str]:
    """The folders that answer ``query``, best first.

    Same rule as the folder branch of :func:`score`, and the same reason the query
    keeps its noise words: a folder is *named* "vocal", "live", "2019", and
    stripping those would leave nothing to compare.

    Separate from :func:`rank` on purpose. "Play my vocal songs" names a folder, not
    forty files, and reaching the folder tier by fuzzy-matching 4,000 filenames is
    how a request for a playlist turns into one arbitrary track.
    """
    raw = _SEPARATORS.sub(" ", str(query or "").strip().casefold())
    if not raw:
        return []
    # **Tags first, then words -- and the order is the whole point.** A folder is
    # named with the very words the title matcher discards: `vocal`, `live`,
    # `instrumental`, `2019`. Using `significant_words` here made "play my vocal
    # songs" reach nothing, because every word of it was either noise or a version
    # word, and the folder called `vocal` was the answer. So the request's *tags*
    # are matched against the folder name first, and the word comparison is only
    # the fallback for a folder named after something else.
    tags = requested_tags(raw)
    terms = significant_words(raw)
    hits: list[tuple[int, int, str]] = []
    for position, name in enumerate(folder_names):
        haystack = comparable_name(name)
        if not haystack:
            continue
        # Word-based, never substring: "play a" used to match a folder called
        # "vocal", because the letter "a" is in "vocal", and the tool then offered
        # to play a playlist the user never named.
        landed, _not_landed = _matched_words(terms, haystack)
        tag_landed, _missed = _matched_words(tags, haystack)
        if haystack == raw or haystack == _STRANDED.sub("", raw):
            tier = 0
        elif tag_landed:
            tier = 1
        elif landed:
            tier = 2
        else:
            continue
        hits.append((tier, position, name))
    hits.sort(key=lambda row: (row[0], row[1]))
    return [name for _tier, _position, name in hits[: max(0, limit)]]