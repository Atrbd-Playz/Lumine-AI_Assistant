"""YouTube's search results, as facts, and the rules for picking one of them.

## The problem this exists for

"Click the second video" fails in three different ways, and all three are the
same mistake from different directions:

1. **Constructing a URL.** ``youtube.com/watch?v=<something>`` assembled from a
   title, a channel, or a result index is *fabricated*. It may 404, it may play a
   different video, and the model will report it as the requested one. Nothing in
   this module produces a watch URL -- it produces a *position in a list the page
   actually rendered*, and the click goes through the page's own UI.
2. **Taking the first match.** YouTube's result list mixes channels, playlists,
   Shorts shelves, ads, and "people also watched" into the same visual column.
   Index 2 of *anything* is not index 2 of the videos.
3. **Not re-checking.** A single-page app can replace the results between the
   observation and the click. So the target is described by *facts read from the
   DOM* (title, channel, duration, href), the click is preceded by a re-read that
   must still agree, and the result is verified afterwards.

## What is extracted

Per result: position (1-based, over video results only), title, channel,
duration, view count, and href. The channel and duration are what make
disambiguation possible; without them the only disambiguator is "second", and
"second" is not enough when the list contains three kinds of thing.

## Where the facts come from

Everything is read from the rendered DOM through selectors listed in
:data:`RESULT_SELECTORS`, and every selector has a documented fallback because
YouTube's markup is not a contract. When *no* selector matches, the extraction
reports ``available: False`` with a reason rather than returning an empty list --
"there are no results" and "I could not read the results" are different
situations and the tool must not say the first when it means the second.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

#: Containers for a watchable result, most specific first. ``#video-title`` is the
#: link; its closest list item is the row, which is where the channel and the
#: duration live.
RESULT_SELECTORS: tuple[str, ...] = (
    "ytd-video-renderer",
    "ytd-rich-item-renderer",
    "ytd-grid-video-renderer",
    "ytd-compact-video-renderer",
    "a#video-title[href]",
)

#: Selectors that are *not* video results even though they sit in the same
#: column. Filtered by container, not by index, because the filter has to survive
#: a layout change.
EXCLUDED_CONTAINERS: tuple[str, ...] = (
    "ytd-ad-slot-renderer",
    "ytd-shelf-renderer",
    "ytd-message-renderer",
    "ytd-background-promotion-renderer",
    "ytd-promoted-sparkles-web-renderer",
)

_DURATION = re.compile(r"(\d{1,2}:\d{2}(?::\d{2})?)")
_VIEWS = re.compile(r"([\d.,]+[KMB]?)\s*(?:views|watching)", re.IGNORECASE)


def parse_duration(raw: object) -> str:
    """``"12:34"`` from whatever shape the page used. ``""`` when absent."""
    match = _DURATION.search(str(raw or ""))
    return match.group(1) if match else ""


def parse_views(raw: object) -> str:
    match = _VIEWS.search(str(raw or ""))
    return match.group(1) if match else ""


@dataclass
class VideoResult:
    """One watchable result, as the page rendered it."""

    position: int
    title: str
    channel: str = ""
    duration: str = ""
    views: str = ""
    href: str = ""
    ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"position": self.position, "title": self.title}
        if self.channel:
            out["channel"] = self.channel
        if self.duration:
            out["duration"] = self.duration
        if self.views:
            out["views"] = self.views
        if self.ref:
            out["ref"] = self.ref
        return out

    def facts(self) -> str:
        """The identifying facts as one comparable blob, for re-validation."""
        return "|".join(
            [self.title.casefold(), self.channel.casefold(), self.href, self.duration]
        )


@dataclass
class ResultSet:
    """What was read off the results container."""

    results: list[VideoResult] = field(default_factory=list)
    available: bool = False
    reason: str = ""

    def __len__(self) -> int:
        return len(self.results)

    def __iter__(self):
        return iter(self.results)


@dataclass(frozen=True)
class VideoSelector:
    """What the user asked for, beyond a position."""

    position: int = 0
    title: str = ""
    channel: str = ""
    views: str = ""
    duration: str = ""

    @property
    def unconstrained(self) -> bool:
        return not (self.position or self.title or self.channel or self.views or self.duration)


def selector_from_text(text: object, *, position: int = 0) -> VideoSelector:
    """Parse "the second video by Cocomelon" into a selector.

    Best-effort and deliberately narrow: a channel is anything after "by" or
    "from", a title is a quoted string or the words before a "video(s)" token.
    Anything it cannot read is left empty rather than guessed, and an empty
    constraint is *better* than a wrong one -- a wrong channel filter would
    resolve to the wrong video and report it as right.
    """
    raw = " ".join(str(text or "").split())
    lowered = raw.casefold()
    channel = ""
    for marker in (" by ", " from ", " on "):
        position_of = lowered.rfind(marker)
        if position_of > 0:
            candidate = raw[position_of + len(marker):].strip(" .!?,")
            if candidate and "video" not in candidate.casefold():
                channel = candidate
                raw = raw[:position_of]
                lowered = raw.casefold()
                break
    title = ""
    quoted = re.search(r"[\"“”']([^\"“”']{2,80})[\"“”']", raw)
    if quoted:
        title = quoted.group(1).strip()
    else:
        stripped = re.sub(r"^\s*(?:the\s+)?", "", raw, flags=re.IGNORECASE)
        stripped = re.sub(
            r"\b(?:video|videos|result|results|one|please|click|open|play)\b.*$", "", stripped, flags=re.IGNORECASE
        ).strip(" .!?,")
        if stripped and stripped.casefold() not in ("", "the", "a", "an"):
            title = stripped
    return VideoSelector(
        position=position,
        title=title,
        channel=channel,
    )


def channel_matches(result_channel: str, wanted: str) -> bool:
    """Whether ``wanted`` names this channel, exactly or as a whole word.

    Two tiers on purpose. An exact match is the user's channel; a whole-word match
    is a channel that *contains* the word -- "Cocomelon" naming "Cocomelon Kids"
    is a real possibility and not the same as the channel the user asked for. The
    caller ranks exact above partial for that reason; what this function refuses
    is a substring inside a longer word, which is how "com" would otherwise match
    every channel that has it anywhere.
    """
    name = result_channel.casefold().strip()
    target = wanted.casefold().strip()
    if not target:
        return True
    if name == target:
        return True
    return bool(re.search(rf"\b{re.escape(target)}\b", name))


#: Words that carry no intent in a search phrase. Dropping them is what lets
#: "obosthan vocal song" and "obosthan song" reach the same video -- "vocal" and
#: "song" describe a *kind* of thing, and a title that says "Debut Track" has not
#: failed to mention them.
TITLE_STOPWORDS = frozenset(
    {
        "song", "songs", "vocal", "vocals", "video", "videos", "audio",
        "track", "tracks", "music", "live", "official", "lyric", "lyrics",
        "hd", "hq", "the", "a", "an", "of", "in", "on", "to", "for", "and",
        "full", "new", "best", "mix", "version", "clip",
    }
)


def title_score(result_title: str, wanted: str) -> float:
    """How well a result's title answers what the user asked for.

    **This used to be a substring test**, and it was the whole of "she cannot find
    the song I can see". A user says "obosthan vocal song"; the video on the page
    is titled "Obosthan (অবস্থান) - Train Poka Album - HIGHWAY - Debut Track".
    ``"obosthan vocal song" in that title`` is False, so every real result was
    discarded and Lumine reported that nothing matched while reading a page that
    plainly contained it.

    So it is a word-overlap score instead. Every significant word the user used
    that appears in the title counts; words that only describe the *kind* of
    thing are ignored, because a title is not obliged to contain the word "vocal"
    to be a vocal. A full substring still scores highest, so an exact hit is still
    chosen first.

    Returns 0.0 for no overlap at all, which the caller treats as a miss.
    """
    haystack = str(result_title or "").casefold()
    needle = str(wanted or "").casefold().strip()
    if not haystack or not needle:
        return 0.0
    if needle in haystack:
        return 1.0

    wanted_words = {
        word
        for word in re.findall(r"[^\W_]+", needle, re.UNICODE)
        if word not in TITLE_STOPWORDS and len(word) > 1
    }
    if not wanted_words:
        # Every word was a stopword, so the user's phrase carries no intent we can
        # weigh. Score on the raw phrase rather than rejecting everything.
        wanted_words = set(re.findall(r"[^\W_]+", needle, re.UNICODE))

    found = sum(1 for word in wanted_words if word in haystack)
    if not found:
        return 0.0
    return found / len(wanted_words)


#: Below this, a result is not what was asked for. One shared word out of four is
#: "Obosthan", and a page full of Obosthan-adjacent videos is exactly the case
#: where picking the wrong one is worse than admitting the search was unclear.
MIN_TITLE_SCORE = 0.5


def _tier_for(result: VideoResult, selector: VideoSelector) -> int | None:
    """``0`` for an exact channel, ``1`` for a partial one, ``None`` for a miss.

    ``None`` is a miss rather than a low score, so the caller can drop it without
    also dropping a legitimate weak match.
    """
    if selector.title:
        if title_score(result.title, selector.title) < MIN_TITLE_SCORE:
            return None
    if selector.channel:
        if result.channel.casefold().strip() == selector.channel.casefold().strip():
            channel_tier = 0
        elif channel_matches(result.channel, selector.channel):
            channel_tier = 1
        else:
            return None
    else:
        channel_tier = 0
    if selector.duration and result.duration and selector.duration not in result.duration:
        return None
    if selector.views and result.views and selector.views.casefold() not in result.views.casefold():
        return None
    return channel_tier


def _matches(result: VideoResult, selector: VideoSelector) -> bool:
    return _tier_for(result, selector) is not None


@dataclass
class Pick:
    """The chosen result, or the reason there is not one."""

    result: VideoResult | None
    reason: str = ""
    considered: int = 0

    @property
    def ok(self) -> bool:
        return self.result is not None


def pick(results: Sequence[VideoResult], selector: VideoSelector) -> Pick:
    """Choose one result, or say why not.

    Order of resolution, and why:

    1. **Filter** by every constraint the user gave. A constraint the user
       volunteered is *evidence about intent*; dropping it because it was
       inconvenient is how "the second video by Cocomelon" opens Cocomelon Kids.
    2. **Prefer an exact channel** over a partial one among the survivors. "By
       Cocomelon" means that channel; "Cocomelon Kids" merely contains the word.
    3. **Then apply the position**, counted over the survivors. This is the
       important subtlety: "the second video by Cocomelon" means the second of
       Cocomelon's results, not the second result overall that happens to be by
       Cocomelon.
    4. **Then refuse** rather than pick the first, if more than one survives with
       no position.

    No URL is ever constructed here. The caller clicks the element the page
    rendered, through its own UI.
    """
    if not results:
        return Pick(None, "I could not read any results on this page", 0)

    scored = [(_tier_for(item, selector), item) for item in results]
    survivors = [(item, tier) for tier, item in scored if tier is not None]
    if not survivors:
        named = selector.channel or selector.title or f"position {selector.position}"
        return Pick(None, f"no result matches {named}", len(results))

    # An exact channel match *is* the channel the user named, so partial matches
    # are dropped rather than ranked below it. "By Cocomelon" must not end up
    # ambiguous because a second channel's name happens to contain the word --
    # otherwise the one channel that is definitely right produces a question about
    # a channel that is only possibly right.
    exact = [item for item, tier in survivors if tier == 0]
    kept = exact or [item for item, _tier in survivors]

    if selector.position > 0:
        if selector.position > len(kept):
            return Pick(
                None,
                f"there are only {len(kept)} matching result(s), not {selector.position}",
                len(results),
            )
        return Pick(kept[selector.position - 1], "", len(results))

    if len(kept) == 1:
        return Pick(kept[0], "", len(results))
    return Pick(
        None,
        f"{len(kept)} results match; tell me which one (position, channel, or title)",
        len(results),
    )


def same_result(before: VideoResult | None, after: VideoResult | None) -> bool:
    """Whether two readings describe the same element.

    Used to re-validate a target across a re-render. Compared on the identifying
    facts -- title, channel, href -- and *not* on position, because position is
    the one thing a re-render changes while the video stays the same video.
    """
    if before is None or after is None:
        return False
    if before.href and after.href:
        return before.href == after.href
    return before.facts() == after.facts()


def playable_from_url(url: object) -> str:
    """What can be honestly said about a YouTube page.

    Returns one of ``"playing"``, ``"available"``, ``"unavailable"``, or ``""``.

    The distinction that matters is ``unavailable``: a removed, private or
    region-restricted video shows a real page with a real title, and reporting
    "opened the video" for it is the specific dishonesty this function exists to
    prevent. Two signals, in order:

    * a Google ``/sorry/`` interstitial -- the bot-check page, which means the
      click never reached a video at all;
    * a *query parameter* YouTube itself sets for an unplayable video. Checked on
      the query, not the path, because a private video's URL still contains
      ``/watch`` and only carries ``&reason=`` to say why it cannot play.

    Detection reads what the page reported. Nothing here infers playability from a
    title, an index or a constructed address.
    """
    text = str(url or "")
    if not text:
        return ""
    lowered = text.casefold()
    if "/sorry" in lowered:
        return "unavailable"
    if "/watch" not in lowered and "/shorts/" not in lowered and "/live/" not in lowered:
        return ""
    if re.search(r"[?&](?:reason|error)=", lowered):
        return "unavailable"
    return "available"