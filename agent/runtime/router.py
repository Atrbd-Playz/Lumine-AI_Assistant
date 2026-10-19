"""Which environment a request belongs in: background, browser, or desktop.

The problem this solves is not "how do I call a tool" -- the registry already
lists them. It is that three different tools can each plausibly answer "open
Instagram", and picking the wrong one is *visible*: the user asked for their own
browser and got a second browser window nobody asked for.

So routing is decided once, in one pure function, before any tool runs, and the
decision is recorded (:class:`Route` carries ``reason`` and ``alternatives``) so
a wrong route is diagnosable rather than mysterious.

## Why it is not a keyword table

The obvious implementation -- "if the text contains 'search', route to
``search_web``" -- fails in both directions, and the table in the requirements is
a set of *examples*, not rules:

* "Search for white T-shirt prices" wants the API. "Search for a song on
  YouTube" wants a page, because YouTube's search results are the answer and a
  list of links is not. The verb is identical.
* "Open Chrome" is a desktop request. "Open YouTube" is *also* a desktop request
  when an installed app or PWA exists for it, and a browser request when there is
  not. Only the machine knows which.

So the policy is a small ordered set of *evidence* rules, strongest first, with
explicit intent beating every default:

1. **An explicit environment claim wins.** "in your browser", "in chrome",
   "on my desktop" -- the user has already answered the question we would
   otherwise have to ask, so no amount of app-index state overrides it.
2. **An explicitly named known program or site wins.** Naming Krita means
   Krita; naming Instagram means Instagram, and whether that is a PWA or a
   browser tab is decided by what is installed, not by preference.
3. **The verb family decides** once nothing is named: interact verbs
   (click/type/choose/scroll/fill) require a page and get the browser; look-up
   verbs get the background tools.
4. **Ambiguity is a question, not a coin flip.** Two equally-supported
   environments produce a route with ``ambiguous`` set and a short ``question``,
   and the caller asks rather than opening two windows.

Everything here is pure. Installation state, running windows and open browser
pages are passed in as facts by the caller, so every branch is testable with a
fixture and no filesystem, no registry, and no window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

# -- environments -------------------------------------------------------------

BACKGROUND = "background"
BROWSER = "browser"
DESKTOP = "desktop"
MESSAGING = "messaging"
#: The user's own music folder. Not the browser and not "the internet" -- a
#: folder they chose, on a disk they own.
MEDIA = "media"

ENVIRONMENTS: tuple[str, ...] = (BACKGROUND, BROWSER, DESKTOP, MESSAGING, MEDIA)

# -- verb families ------------------------------------------------------------

#: Verbs that need a *page* to exist. None of them can be answered by a search
#: API, which is the whole reason they route to the browser rather than being
#: treated as a lookup.
INTERACT_VERBS: frozenset[str] = frozenset(
    {
        "click", "clicking", "clicks", "type", "typing", "types", "fill",
        "filling", "choose", "choosing", "select", "selecting", "scroll",
        "pressing", "press", "tap", "double-click", "right-click", "hover",
        "log", "sign", "signin", "login", "book", "order", "buy", "checkout",
        "add", "remove", "play", "pause", "watch", "listen", "post",
    }
)

#: Verbs that ask for a *fact*. These are the ones the background tools answer
#: cheaply, and routing one through the browser costs a window and a page load to
#: return the same information.
LOOKUP_VERBS: frozenset[str] = frozenset(
    {
        "find", "search", "look", "searching", "looking", "research",
        "compare", "price", "prices", "review", "reviews", "news",
        "weather", "forecast", "tell", "what", "whats", "latest", "current",
        "check", "summarize", "summarise", "list",
    }
)

#: Verbs that mean "put something on the screen".
OPEN_VERBS: frozenset[str] = frozenset({"open", "opening", "launch", "launching", "start", "run", "show"})
CLOSE_VERBS: frozenset[str] = frozenset({"close", "closing", "quit", "exit", "minimize", "maximize"})
WINDOW_VERBS: frozenset[str] = frozenset(
    {"focus", "switch", "bring", "restore", "foreground", "window", "windows"}
)

#: Words that name a *browser tab* rather than an application window.
#:
#: Added because "switch to the other tab" contains `switch`, and `switch` is in
#: `WINDOW_VERBS` -- so the request fell into the rule below and was routed to
#: `manage_window` on the **desktop**, a tool that arranges application windows
#: and has no notion of a tab at all. The user's report was that Lumine had no
#: control over tabs, and this is where the control went: not to the browser,
#: to a tool that cannot use it.
#:
#: The words are deliberately common. "tab" is the noun, and the browser is the
#: only place it means anything.
TAB_WORDS: frozenset[str] = frozenset(
    {"tab", "tabs", "tabbed"}
)

#: A request that talks about a tab, whatever else it says. Checked *before* the
#: window rule, so "close that tab" is a tab and "switch to the other tab" is a
#: tab -- and neither becomes a window request.
def _talks_about_tabs(text: object) -> bool:
    return bool(TAB_WORDS & set(words(text)))
MESSAGE_VERBS: frozenset[str] = frozenset(
    # The inflected forms are here because this branch is the *only* way a request
    # about somebody's messages reaches the messaging tools, and speech-to-text
    # produces "has Afia replied" far more often than "reply". Without them that
    # sentence fell through to a background search.
    #
    # `answered` is deliberately absent: it is too widely used outside messaging
    # ("answered the door"), and a missed route is a wasted turn while a wrong one
    # is a conversation about something else entirely.
    {
        "message", "text", "whatsapp", "chat",
        "reply", "replied", "respond", "responded",
        "send", "sent",
    }
)

#: "What did she say", "did he reply", "have I read that" -- a question about
#: words *already written*. Checked before the messaging branch because that
#: branch catches anything containing "message", and "any new messages from Afia?"
#: is a question about her inbox rather than an instruction to message her.
_ASKING_ABOUT = re.compile(
    r"\b(did|does|do|has|have|had|is|are|was|were|any|what|who|which|when|"
    r"tell me|show me|read|check|see|last|latest|unread)\b",
    re.IGNORECASE,
)
# The interrogatives above carry the read side of "reply" on their own:
# "did Afia reply?", "has Afia replied?" and "any reply?" each contain one. What
# separates them from the imperative "reply to Afia" is exactly that -- a question
# word in front. Adding `reply` to *this* pattern was tried and made it worse:
# it matched the imperative too, so "reply to Afia saying hi" became a read.

#: The counterweight: an explicit instruction to write. When both are present the
#: write wins, because "did she get my message? no -- send it again" is two
#: requests and the second one is the one being acted on.
#:
#: **`reply` and `respond` are deliberately absent.** They are the one genuinely
#: ambiguous word in this pair: "reply to Afia" is an instruction to write, while
#: "did Afia reply?" is a question about what was written. Listing `reply` here
#: made the question route to `send_whatsapp`, which is the exact failure the
#: comment above this function says was already fixed once -- a read reaching for
#: the tool that sends. The interrogatives in `_ASKING_ABOUT` carry the read
#: side, and the direction of the error matters: a mis-routed read costs a wasted
#: turn, a mis-routed send messages somebody.
_WRITING = re.compile(
    r"\b(send|write|text|forward|tell|ask|let her know|let him know|"
    r"inform|update|confirm)\b",
    re.IGNORECASE,
)

# -- explicit environment claims ---------------------------------------------

#: Phrases where the user names *our* browser. Checked before anything else,
#: because "open YouTube in your browser" is not ambiguous under any reading and
#: must never also start the user's Chrome.
_OWN_BROWSER = re.compile(
    r"\b(?:in|on|with|using)\s+(?:your|your own|lumine'?s?|my)\s+(?:own\s+)?browser\b"
    r"|\bin\s+lumine\s+browser\b|\bin\s+your\s+playwright\b",
    re.IGNORECASE,
)

#: Phrases that name the user's own browser explicitly. Kept as a *claim* rather
#: than a default so that "open Chrome" and "open a browser" differ: the first
#: names a program, the second genuinely does not.
_NAMED_BROWSERS: dict[str, str] = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "brave": "brave",
    "brave browser": "brave",
    "edge": "edge",
    "microsoft edge": "edge",
    "firefox": "firefox",
    "opera": "opera",
    "vivaldi": "vivaldi",
    "chromium": "chromium",
}

_BROWSER_CLAIM = re.compile(
    r"\b(?:in|on|with|using|open)\s+(?:my\s+|the\s+|your\s+)?(" + "|".join(sorted(_NAMED_BROWSERS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_DESKTOP_CLAIM = re.compile(
    r"\b(?:on|in)\s+(?:my|the|our)\s+(?:desktop|computer|laptop|pc|mac|machine)\b",
    re.IGNORECASE,
)

#: Words that mean the *installed program* rather than the website of the same
#: name. Needed because step 3 now steps aside for a site name, and a user who has
#: the app on purpose needs a way to say so. Deliberately narrow: "app" is in it
#: and "open" is not, because "open youtube" is overwhelmingly the website and
#: guessing wrong there launches a program nobody asked for.
_APP_CLAIM = re.compile(
    r"\b(?:the\s+)?(?:desktop\s+|native\s+|real\s+)?"
    r"(?:app|application|program|client)\b"
    r"|\binstalled\b",
    re.IGNORECASE,
)

#: A name the user paired with **"desktop"**, and nothing else.
#:
#: This exists because "open whatsapp" was launching *both* WhatsApp Web and the
#: installed WhatsApp Desktop app. Two separate mechanisms each did half of it, and
#: neither was wrong on its own: the site rule reached for the installed program,
#: and then the messaging rule at step 1 -- which correctly reads any request naming
#: "whatsapp" as a messaging request -- reached for Web. The user asked for one
#: thing and got two windows.
#:
#: The product decision this encodes is now the general one rather than a
#: WhatsApp exception: **the website is the default, and the application is reached
#: by naming it.** "open YouTube" opens the site; "open desktop YouTube" opens the
#: program.
#:
#: Narrow on purpose. ``_APP_CLAIM`` stays general, because "open the Spotify app"
#: is unambiguous. This one is the single word the product asks for, so it only has
#: to recognise "desktop X" and "the native X".
_DESKTOP_APP_CLAIM = re.compile(
    # "desktop YouTube", "the native app for Spotify"
    r"\b(?:desktop|native)\s+(?:app\s+|version\s+)?\w+"
    r"|\bdesktop\s+version\b"
    # "WhatsApp Desktop", "YouTube desktop" -- the *product's own* name puts the
    # word second, and speech-to-text writes it exactly that way. Without this,
    # "open whatsapp desktop" was read as a messaging request.
    #
    # The lookahead keeps "on my desktop" out of it. That phrase is handled by
    # `_DESKTOP_CLAIM` and means the machine rather than a program, and letting it
    # match here would make every sentence containing "my desktop" name an app.
    r"|\b(?!(?:my|our|your|the)\s)\w+\s+desktop\b",
    re.IGNORECASE,
)

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_FILLER = frozenset(
    {
        "a", "an", "the", "my", "me", "i", "you", "your", "please", "could",
        "would", "can", "will", "just", "now", "then", "and", "of", "for", "to",
        "in", "on", "with", "at", "it", "that", "this", "some", "any", "is",
        "are", "do", "does", "go", "going", "want", "wanna", "lets", "let",
        "up", "out", "about", "from", "into", "over", "again", "one",
    }
)

# -- known sites --------------------------------------------------------------
#
# A small, explicit table rather than a guessed URL. Every entry carries the
# canonical address *and* the fact that it is a site with a search box, because
# "search on YouTube" needs the second field to route at all.

SITES: dict[str, dict[str, str]] = {
    "youtube": {"url": "https://www.youtube.com", "search": "youtube_search", "host": "youtube.com"},
    "instagram": {"url": "https://www.instagram.com", "search": "instagram", "host": "instagram.com"},
    "google": {"url": "https://www.google.com", "search": "google_search", "host": "google.com"},
    "gmail": {"url": "https://mail.google.com", "search": "gmail", "host": "mail.google.com"},
    "amazon": {"url": "https://www.amazon.com", "search": "amazon_search", "host": "amazon.com"},
    "netflix": {"url": "https://www.netflix.com", "search": "", "host": "netflix.com"},
    "x": {"url": "https://x.com", "search": "x_search", "host": "x.com"},
    "twitter": {"url": "https://x.com", "search": "x_search", "host": "x.com"},
    "facebook": {"url": "https://www.facebook.com", "search": "facebook", "host": "facebook.com"},
    "reddit": {"url": "https://www.reddit.com", "search": "reddit_search", "host": "reddit.com"},
    "linkedin": {"url": "https://www.linkedin.com", "search": "linkedin", "host": "linkedin.com"},
    "github": {"url": "https://github.com", "search": "github_search", "host": "github.com"},
    "wikipedia": {"url": "https://en.wikipedia.org", "search": "wikipedia_search", "host": "wikipedia.org"},
    "whatsapp": {"url": "https://web.whatsapp.com", "search": "", "host": "web.whatsapp.com"},
    "maps": {"url": "https://www.google.com/maps", "search": "maps_search", "host": "google.com"},
    "chatgpt": {"url": "https://chatgpt.com", "search": "", "host": "chatgpt.com"},
    "spotify": {"url": "https://open.spotify.com", "search": "spotify_search", "host": "open.spotify.com"},
    # The default search engine, so "search duckduckgo for X" routes like any
    # other site. It is also the page `browse open` with no address opens.
    "duckduckgo": {"url": "https://duckduckgo.com", "search": "duckduckgo_search", "host": "duckduckgo.com"},
}

#: A bare address in the request. Captured so "open example.com/x" is a browser
#: task regardless of any other word in the sentence.
_URL = re.compile(r"\b(?:https?://|www\.)[^\s,]+", re.IGNORECASE)
_DOMAINISH = re.compile(r"\b[a-z0-9][a-z0-9-]{1,30}\.(?:com|net|org|io|dev|app|co|uk|de|fr|es|jp|in|bd|ca|au|tv|me)\b", re.IGNORECASE)

# -- misc ---------------------------------------------------------------------

_STOPWORDS = frozenset({"the", "a", "an", "and", "or", "of", "in", "on", "with", "to", "for"})

#: Verbs that only make sense against a music library. `pause` and `skip` are in
#: here even though the system player cannot do either, because a refusal belongs
#: to the *tool* and a wrong route does not: routing "pause the song" to the
#: desktop would send Lumine to click a player window with the mouse, which is
#: both worse and more authority than the request needs.
_MEDIA_VERB = re.compile(
    r"\b(play|pause|resume|skip|stop|unpause)\b", re.IGNORECASE
)

#: Nouns that mean "a file on this computer", as opposed to a page.
_MEDIA_NOUN = re.compile(
    r"\b(song|songs|music|track|tracks|album|albums|playlist|playlists|"
    r"vocal|vocals|audio|discography)\b",
    re.IGNORECASE,
)

#: Nouns that mean "on a page or a screen", and which therefore must not be
#: routed to a folder. Excluded explicitly rather than by the absence of a media
#: noun: "play the third video" and "play that clip" are both real requests, and
#: the ordinal case is the one the browser answers.
_SCREEN_NOUN = re.compile(
    r"\b(video|videos|clip|clips|episode|episodes|movie|movies|film|game|"
    r"stream|channel|screen)\b",
    re.IGNORECASE,
)


def words(text: object) -> list[str]:
    """Lowercased content words, in order, duplicates kept."""
    return [match.group(0) for match in _WORD.finditer(str(text or "").casefold())]


def content_words(text: object) -> list[str]:
    """Content words only -- what a name is likely to be spelled out of."""
    return [word for word in words(text) if word not in _FILLER]


def find_site(text: object) -> tuple[str, dict[str, str]] | None:
    """The site this request names, if it names one.

    Whole-token matching only, and against a closed list: a request that merely
    contains the letters "ig" does not mean Instagram, and a heuristic that
    occasionally decides to open the wrong account is worse than one that asks.
    """
    tokens = set(words(text))
    for name in sorted(SITES, key=len, reverse=True):
        if name in tokens:
            return name, SITES[name]
    return None


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slug(text: object) -> list[str]:
    """Lowercased word tokens, punctuation removed.

    Needed because installed names are not words: "Instagram
    (www.instagram.com)" is one entry in the index, and a user says "Instagram".
    Everything from the bracket onward is dropped first, because the part in
    brackets is the *host the site lives on*, not part of the name anybody uses.
    """
    cleaned = re.sub(r"\(.*", "", str(text or ""))
    return [token for token in _SLUG_STRIP.split(cleaned.casefold()) if token]


def find_named_browser(text: object) -> str | None:
    """Which of the user's own browsers the request names, if any."""
    match = _BROWSER_CLAIM.search(str(text or ""))
    if match:
        return _NAMED_BROWSERS[match.group(1).casefold()]
    # "open chrome" with no preposition is still naming a program. The word has
    # to be the whole token, because "chromedriver" is not a browser request.
    tokens = set(words(text))
    for name, key in sorted(_NAMED_BROWSERS.items(), key=lambda row: len(row[0]), reverse=True):
        if name in tokens:
            return key
    return None


#: Words that mean "the Nth one", wherever one is in a list. Whole tokens only:
#: "third" in "thirteen" would otherwise make an ordinary sentence an ordinal
#: request.
_ORDINAL_WORDS = ("first", "second", "third", "fourth", "fifth", "last", "1st", "2nd", "3rd")


def _ordinal_phrase(text: object) -> bool:
    """Whether the request picks something out by position."""
    tokens = set(words(text))
    return any(word in tokens for word in _ORDINAL_WORDS)


def verb_families(text: object) -> set[str]:
    """Which verb families the request uses."""
    tokens = set(words(text))
    found: set[str] = set()
    if tokens & INTERACT_VERBS:
        found.add("interact")
    if tokens & LOOKUP_VERBS:
        found.add("lookup")
    if tokens & OPEN_VERBS:
        found.add("open")
    if tokens & CLOSE_VERBS:
        found.add("close")
    if tokens & WINDOW_VERBS:
        found.add("window")
    if tokens & TAB_WORDS:
        found.add("tab")
    if tokens & MESSAGE_VERBS:
        found.add("message")
    return found


# -- the route ---------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """One routing decision, and the evidence for it.

    ``alternatives`` is kept because "she opened a browser when I asked for the
    app" is answerable afterwards only if the alternatives were recorded at the
    time. It is never shown to the user in full: the model gets a one-line
    reason at most.
    """

    environment: str
    target: str = ""
    url: str = ""
    tool: str = ""
    #: The *verb* to use on that tool, when the environment alone does not say
    #: which one. A tab request is the reason this exists: "switch to the other
    #: tab" and "open example.com" both route to `browse`, and handing the model
    #: only the tool leaves it to guess between `open` and `tab` -- and it guesses
    #: `open`, which navigates the tab in use and throws the page away.
    action: str = ""
    reason: str = ""
    alternatives: tuple[str, ...] = ()
    ambiguous: bool = False
    question: str = ""
    sensitive: bool = False
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.environment in ENVIRONMENTS and not self.ambiguous

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"environment": self.environment}
        for key in ("target", "url", "tool", "action", "reason", "question"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.alternatives:
            out["alternatives"] = list(self.alternatives)
        if self.ambiguous:
            out["ambiguous"] = True
        if self.sensitive:
            out["sensitive"] = True
        if self.notes:
            out["notes"] = self.notes
        return out


@dataclass(frozen=True)
class MachineFacts:
    """What the router is allowed to know about the machine.

    Passed in rather than discovered, for one reason: the router has to be a pure
    function of the request plus these facts, or none of its branches can be
    tested without a real desktop. ``installed`` is a set of normalized app
    names; ``running`` maps a normalized name to how many processes carry it;
    ``windows`` is the normalized titles currently open; ``browser_open`` says
    whether Lumine's isolated browser has a page.
    """

    installed: frozenset[str] = frozenset()
    #: The same names, longest first. Computed once in :meth:`build` because the
    #: router would otherwise re-sort ~950 entries on every request, which
    #: measured as more time than the whole rest of the classification.
    ordered: tuple[str, ...] = ()
    running: Mapping[str, int] = field(default_factory=dict)
    windows: frozenset[str] = frozenset()
    browser_open: bool = False
    default_browser: str = ""
    own_browser_default: bool = False
    whatsapp_ready: bool = False

    @classmethod
    def build(
        cls,
        *,
        installed: Iterable[str] = (),
        running: Mapping[str, int] | None = None,
        windows: Iterable[str] = (),
        browser_open: bool = False,
        default_browser: str = "",
        own_browser_default: bool = False,
        whatsapp_ready: bool = False,
    ) -> "MachineFacts":
        return cls(
            installed=frozenset(str(name).strip().casefold() for name in installed if str(name).strip()),
            ordered=_Ordered(sorted((str(name).strip().casefold() for name in installed if str(name).strip()), key=len, reverse=True)),
            running=_normalize_processes(running),
            windows=frozenset(str(title).strip().casefold() for title in windows if str(title).strip()),
            browser_open=bool(browser_open),
            default_browser=str(default_browser or "").strip().casefold(),
            own_browser_default=bool(own_browser_default),
            whatsapp_ready=bool(whatsapp_ready),
        )

    def processes_for(self, name: object) -> int:
        """How many processes carry this program, however it was spelled.

        A caller reports processes by executable (``krita.exe``) while an index
        entry is a display name (``Krita``), so the lookup strips the extension and
        falls back to a prefix match for the multi-part names an app index carries.
        """
        key = " ".join(str(name or "").split()).casefold()
        if not key:
            return 0
        if key in self.running:
            return self.running[key]
        stem = key[:-4] if key.endswith(".exe") else key
        if stem in self.running:
            return self.running[stem]
        for name_key, count in self.running.items():
            if name_key.startswith(stem):
                return count
        return 0


def _normalize_processes(running: Mapping[str, int] | None) -> dict[str, int]:
    """Process names as keys, extension stripped, so lookups agree."""
    out: dict[str, int] = {}
    for name, count in (running or {}).items():
        key = str(name or "").strip().casefold()
        if key.endswith(".exe"):
            key = key[:-4]
        if key:
            out[key] = max(out.get(key, 0), int(count))
    return out


#: Whether an installed program for a named site should be preferred over the
#: website. **There is no table, and that is the design.**
#:
#: There were two: one list whose names preferred the app (Instagram, Netflix,
#: Spotify) and one whose names preferred the web (WhatsApp). A table like that
#: has to be right about every site, and each entry was a separate opportunity to
#: launch a program nobody asked for -- which is the bug this replaced, twice.
#:
#: The rule now: **the website is the default for every named site, and the
#: application is reached by naming it.** "open YouTube" opens YouTube; "open
#: desktop YouTube" opens the program. One word, no table to age, and no site
#: that behaves differently from every other one.
#:
#: It is also the better default on the merits. The browser is the only path that
#: carries search, reads and history; the app is faster to launch but can do less.
#: A user who wants the app says so once and Lumine learns it from the words,
#: not from which entries somebody remembered to update.


def _same_site(installed_name: object, site_name: object) -> bool:
    """Whether an installed program is the desktop app *for* a known site.

    Token-based, like :func:`_installed_hits`, because the installed name is
    whatever the machine called it: "YouTube", "youtube.exe", "YouTube Desktop",
    "YouTube (web)". Requiring every token of the installed name to appear in the
    canonical site name would miss "YouTube Desktop"; requiring the site name's
    token to appear in the installed name is the direction that holds, and it
    still requires the whole word -- "tube" does not reach "youtube".
    """
    wanted = set(slug(site_name))
    if not wanted:
        return False
    parts = _slug_parts(installed_name)
    if not parts:
        return False
    # Every *word* of the site name has to be somewhere in the program name.
    # The trailing "desktop"/"app" is allowed to be extra because that is exactly
    # what distinguishes the program from the site.
    return wanted.issubset(set(parts))


def _installed_hits(request: str, installed: Iterable[str]) -> list[str]:
    """Installed names this request names, longest first.

    Token-based on purpose, and punctuation-insensitive via :func:`slug`, because
    an index entry is written by whoever installed the program: "Google Chrome",
    "chrome.exe", "WhatsApp (web.whatsapp.com)". Substring matching would make
    "note" match "Notepad"; token matching requires the whole word.

    The one performance note: a real machine indexes ~950 programs, and the
    obvious ``sorted(installed, key=len, reverse=True)`` costs more than the rest
    of the router put together. Sorting is kept because "longest wins" is the
    rule, but it is computed over a *list built once* by the caller
    (:meth:`MachineFacts.ordered_installed`) rather than over a set on every call.
    """
    ordered = installed if isinstance(installed, _Ordered) else sorted(installed, key=len, reverse=True)
    tokens = set(slug(request))
    if not tokens:
        return []
    hits: list[str] = []
    for name in ordered:
        parts = _slug_parts(name)
        if not parts:
            continue
        if all(part in tokens for part in parts):
            hits.append(name)
    return hits


class _Ordered(list):
    """A pre-sorted list.

    Named rather than a plain list because the only thing it carries is the
    guarantee that it is already ordered longest-first; a caller handing a plain
    sorted list gets the same answer, and one handing an unsorted set gets it
    sorted.
    """


_SLUG_CACHE: dict[str, tuple[str, ...]] = {}


def _slug_parts(name: object) -> tuple[str, ...]:
    """:func:`slug` as a tuple, memoised.

    The same ~950 names are slugged on every request otherwise, and slugging is
    two regex passes each. A plain dict rather than an LRU because the key space
    is bounded by what is installed, and a stale entry costs nothing: the string
    is the key.
    """
    key = str(name or "")
    cached = _SLUG_CACHE.get(key)
    if cached is None:
        cached = tuple(slug(key))
        _SLUG_CACHE[key] = cached
    return cached


def classify(
    request: object,
    facts: MachineFacts | None = None,
    *,
    tools: Sequence[str] | None = None,
) -> Route:
    """Decide which environment answers ``request``.

    ``tools`` is the set of tool ids actually enabled for this run. A route whose
    preferred tool is disabled falls through to its alternative rather than
    producing a plan the executor cannot run -- which is the same reason a
    missing browser has to be a *fact* rather than an assumption.
    """
    text = str(request or "").strip()
    facts = facts or MachineFacts()
    available = {str(name).strip().casefold() for name in (tools or ())}

    def usable(tool: str) -> bool:
        return not available or tool in available

    if not text:
        return Route(BACKGROUND, reason="there is nothing to route", ambiguous=False, question="What would you like me to do?")

    families = verb_families(text)
    site = find_site(text)
    url_in_text = _URL.search(text)
    domainish = _DOMAINISH.search(text)
    named_browser = find_named_browser(text)

    # A desktop claim is the user's answer to "where?", so it is consulted with
    # the browser claims rather than after the site table. "Open Instagram on my
    # desktop" means the installed application even though Instagram is a site.
    wants_desktop = _DESKTOP_CLAIM.search(text) is not None

    # -- 0. cancellation and conversational non-tasks ------------------------
    #
    # A *tab* is checked first and returns, because "switch to the other tab"
    # carries `switch` and would otherwise land here -- routed to
    # `manage_window`, which arranges application windows and cannot see a tab.
    # A tab request is a browser request whatever else it says, including
    # "close that tab", which is a tab rather than the browser itself.
    #
    # Messaging is the one thing that outranks it, because "switch to the WhatsApp
    # tab" names a *conversation*, and the tab behind it is not what the user is
    # asking about. Everything else in this module obeys the same order: an
    # explicit claim beats a generic noun.
    if "tab" in families and "message" not in families:
        return Route(
            BROWSER,
            target=first_word(text),
            tool="browse",
            action="tab",
            reason="asked about a browser tab",
            alternatives=(DESKTOP,),
        )

    if families & {"close", "window"} and not site and not named_browser and not domainish:
        return Route(
            DESKTOP,
            target=first_word(text),
            tool="manage_window",
            reason="asked to arrange or close a window",
            alternatives=(BROWSER,),
        )

    # -- 1. messaging --------------------------------------------------------
    # Read and write are separated *here*, not just in the tools, because this is
    # where a question about somebody's messages used to become a send. Any
    # request containing "message" or "whatsapp" was routed to `send_whatsapp`,
    # so "did Afia reply?" reached for the tool that sends. Verbs decide first,
    # because a verb is an unambiguous statement of what the user wants; a name
    # with no verb beside it is not, and falls through to whichever tool is
    # enabled.
    # "desktop WhatsApp" names the installed program, and this rule is about
    # messaging. It used to swallow that phrase, so asking for the desktop app
    # reached WhatsApp Web anyway -- and the user got two windows for one sentence.
    #
    # Both spellings are honoured, because both are how people say it: "desktop
    # WhatsApp" and "the WhatsApp app". The word only wins when WhatsApp itself is
    # named, so "message her on my desktop" still reaches the messaging tools --
    # the right answer there, since that phrase names the machine, not the app.
    names_the_app = "whatsapp" in set(words(text)) and (
        wants_desktop
        or _DESKTOP_APP_CLAIM.search(text)
        or _APP_CLAIM.search(text)
    )
    if ("message" in families or "whatsapp" in set(words(text))) and not names_the_app:
        asks_about = bool(_ASKING_ABOUT.search(text))
        wants_read = asks_about and not _WRITING.search(text)
        wanted_tool = "whatsapp_read" if wants_read else "send_whatsapp"
        if usable(wanted_tool):
            return Route(
                MESSAGING,
                target=first_word(text),
                tool=wanted_tool,
                reason=(
                    "the request is about what somebody wrote"
                    if wants_read
                    else "the request is about messaging someone"
                ),
                alternatives=(BROWSER, DESKTOP),
                sensitive=not wants_read,
            )
        if wants_read and usable("send_whatsapp"):
            # Reads were switched off and sends were not. Saying "messaging is
            # switched off" would send the user looking at a switch that is on.
            return Route(
                MESSAGING,
                tool="whatsapp_read",
                reason="the request is about what somebody wrote",
                question="I can't read your WhatsApp just now. Shall I send a message instead?",
                ambiguous=True,
            )
        return Route(
            MESSAGING,
            tool="send_whatsapp",
            reason="messaging was asked for but the tool is switched off",
            question="Messaging is switched off. Do you want me to open WhatsApp instead?",
            ambiguous=True,
        )

    # -- 2. explicit browser claims -----------------------------------------
    # Strongest rule in the policy. Everything below is a default; this is the
    # user answering the question in advance.
    if _OWN_BROWSER.search(text):
        if not usable("browse"):
            return Route(
                BROWSER,
                url=site[1]["url"] if site else "",
                tool="browse",
                reason="asked for Lumine's own browser, which is switched off",
                question="My browser tool is switched off. Open it in Chrome instead?",
                ambiguous=True,
                alternatives=(DESKTOP,),
            )
        return Route(
            BROWSER,
            target=site[0] if site else "",
            url=site[1]["url"] if site else "",
            tool="browse",
            reason="asked for Lumine's own browser explicitly",
            alternatives=(DESKTOP,),
        )

    if named_browser:
        # Naming Chrome is naming a program. The one honest thing this route can
        # promise is the launch and focus; navigating *their* browser is a
        # different capability and pretending otherwise is how "open Chrome and
        # go to YouTube" ends up opening a second browser.
        if not usable("open_app"):
            return Route(
                BROWSER,
                target="",
                tool="browse",
                reason=f"asked for {named_browser}, but launching programs is switched off",
                question=f"I can't open {named_browser} right now. Use my own browser instead?",
                ambiguous=True,
            )
        return Route(
            DESKTOP,
            target=_browser_app_name(named_browser),
            tool="open_app",
            reason="named a specific installed browser",
            alternatives=(BROWSER,),
            notes={"browser": named_browser, "navigation": "unavailable"},
        )

    # -- 3. an explicitly named program --------------------------------------
    #
    # **Deferred for a name that is also a website.** This step used to run first
    # and win unconditionally, so "open youtube" on a machine with YouTube
    # installed launched the *desktop app* -- and so did "play something on
    # youtube", which no one means as a program launch.
    #
    # The site-vs-app decision belongs to step 4, which has the rule in one place:
    # the website unless the request names the application. A program that is not
    # a website is unaffected, which is why Notepad and Brave still land here.
    hits = _installed_hits(text, facts.ordered or facts.installed)
    if site and hits and _same_site(hits[0], site[0]):
        # Step aside for the website unless the user actually named the app. Two
        # separate words can mean that, and they are not interchangeable:
        # "desktop whatsapp" (specific) and "the whatsapp app" (generic).
        named_the_app = bool(_APP_CLAIM.search(text)) or bool(
            _DESKTOP_APP_CLAIM.search(text)
        )
        if not wants_desktop and not named_the_app:
            hits = []
    if hits:
        name = hits[0]
        if not usable("open_app"):
            return Route(
                BACKGROUND,
                target=name,
                tool="search_web",
                reason="named an installed program, but launching is switched off",
                question=f"I can't open {name} right now. Look something up instead?",
                ambiguous=True,
                alternatives=(BROWSER,),
            )
        already = bool(facts.processes_for(name)) or any(name in title for title in facts.windows)
        return Route(
            DESKTOP,
            target=name,
            tool="open_app",
            reason="named an installed program that is already running" if already else "named an installed program",
            alternatives=(BROWSER,) if site else (),
            notes={"alreadyRunning": already},
        )

    # -- 3b. an address in the request --------------------------------------
    # Ahead of the site table, because a spelled-out address is a stronger signal
    # than a name: "go to github.com" names a place, while "github" alone might be
    # part of a sentence about something else entirely.
    if url_in_text or domainish:
        address = url_in_text.group(0) if url_in_text else f"https://{domainish.group(0)}"
        if "interact" in families:
            if usable("browse"):
                return Route(
                    BROWSER,
                    url=address,
                    tool="browse",
                    reason="an address was given and the request is to operate it",
                    alternatives=(BACKGROUND,),
                )
            return Route(
                BACKGROUND,
                url=address,
                tool="search_web",
                reason="an address was given but the browser tool is switched off",
                ambiguous=True,
                question="My browser tool is switched off. Look it up instead?",
            )
        return Route(
            BROWSER,
            url=address,
            tool="browse",
            reason="an address was named",
            alternatives=(BACKGROUND,),
        )

    # -- 4. an explicitly named site ----------------------------------------
    if site and not wants_desktop:
        name, meta = site
        wants_interaction = bool(families & {"interact"})
        on_site = _ON_SITE.search(text) is not None

        # The website is the default for a named site, always. The application is
        # reached by *naming* it -- "open desktop YouTube", "open the Spotify app".
        #
        # There used to be a table here deciding which sites default to the app
        # (Instagram, Netflix, Spotify) and which to the web (WhatsApp). One rule is
        # better than a table for three reasons, and the third is the one that
        # matters: a table has to be *right* about every site, and each entry was a
        # separate chance to launch a program nobody asked for. A user who wants the
        # app now says one word, and a user who says nothing gets the thing that can
        # actually do the work -- only the web carries searches, reads and chat.
        installed_for_site = _installed_hits(text, facts.ordered or facts.installed)
        named_the_app = bool(
            _DESKTOP_APP_CLAIM.search(text)
            or _APP_CLAIM.search(text)
            or _DESKTOP_CLAIM.search(text)
        )
        if named_the_app and installed_for_site and usable("open_app") and not wants_interaction:
            return Route(
                DESKTOP,
                target=installed_for_site[0],
                tool="open_app",
                reason="asked for the installed application by name",
                alternatives=(BROWSER,),
                notes={"site": name, "isWebApp": True},
            )
        if named_the_app and not installed_for_site:
            # They said "desktop" and there is no such program. Silently opening
            # the website instead would look like it worked, which is the whole
            # failure this rule exists to remove.
            return Route(
                BROWSER,
                target=name,
                url=meta["url"],
                tool="browse",
                reason="asked for an application that is not installed",
                question=(
                    f"I don't have a desktop app for {name}. "
                    f"Open {name} on the web instead?"
                ),
                ambiguous=True,
                alternatives=(DESKTOP,),
            )
        # "find a video on youtube" is a lookup *of the site*, and only the site
        # can answer it. "what's the price of X" is a lookup about the world,
        # and the API answers it for a tenth of the cost.
        if wants_interaction or on_site:
            if usable("browse"):
                return Route(
                    BROWSER,
                    target=name,
                    url=meta["url"],
                    tool="browse",
                    reason="interacting with a named website",
                    alternatives=(BACKGROUND,),
                )
            return Route(
                BACKGROUND,
                target=name,
                tool="search_web",
                reason="named a website but the browser tool is switched off",
                question="My browser tool is switched off. Search the web for it instead?",
                ambiguous=True,
                alternatives=(DESKTOP,),
            )
        # The site is named and the request is *about using it* -- "open YouTube
        # and search for lofi", "search YouTube for a song". The search box only
        # exists there, so a background lookup cannot answer this at all: it would
        # return links about the song rather than the site's own results. This is
        # the difference between a verb table and the real rule, and it was the
        # cause of "she cannot search on YouTube".
        if families & {"open"} or re.search(r"\b(search|find|look\s+up)\b", text, re.IGNORECASE):
            if usable("browse"):
                return Route(
                    BROWSER,
                    target=name,
                    url=meta["url"],
                    tool="browse",
                    reason="the request is to use that site",
                    alternatives=(BACKGROUND,),
                )
        # The site is mentioned but the ask is a fact. Background search.
        return Route(
            BACKGROUND,
            target=name,
            tool="search_web",
            reason="the ask is a fact, not a page",
            alternatives=(BROWSER,),
        )

    # -- 4b. the user's own music folder --------------------------------------
    #
    # Ahead of the verb-family fallback and *behind* every named target, which is
    # the whole of the ordering. `play` is in INTERACT_VERBS, so without this
    # "play love is gone" reached step 5 and became `desktop_control` -- Lumine
    # reaching for the mouse to click a player, with more authority than the
    # request needs and no folder involved at all.
    #
    # Behind the site table on purpose: "play that on YouTube" is a page, and
    # "play it in VLC" is a program. A named target is better evidence than a
    # verb.
    #
    # Two ways in, because the common request carries no media noun at all --
    # "play love is gone" is a verb and a name. The second form therefore
    # requires that *nothing else* claimed it, which is what the `_SCREEN_NOUN`
    # and ordinal guards below are for.
    media_verb = _MEDIA_VERB.search(text) is not None
    if media_verb and not site and not named_browser and not domainish and not url_in_text:
        screen_noun = _SCREEN_NOUN.search(text) is not None
        ordinal = _ordinal_phrase(text)
        explicit_folder = _MEDIA_NOUN.search(text) is not None
        # No installed program either: "play VLC" is naming an application, and
        # the step above already answered it.
        unnamed_target = not _installed_hits(text, facts.ordered or facts.installed)
        if not screen_noun and not ordinal and (explicit_folder or unnamed_target):
            if usable("play_media"):
                return Route(
                    MEDIA,
                    target=first_word(text),
                    tool="play_media",
                    reason="asked for something in their own music folder",
                    alternatives=(BROWSER, DESKTOP),
                    notes={"surface": "library"},
                )
            # Switched off. An honest question rather than a silent fall-through
            # to the desktop, which would answer "play the song" by clicking
            # something the user never named.
            return Route(
                MEDIA,
                tool="play_media",
                reason="asked for their own music, but the library tool is switched off",
                question=(
                    "I can't play your music just now. "
                    "Turn on Music in Settings, or play it on the web instead?"
                ),
                ambiguous=True,
                alternatives=(BROWSER, DESKTOP),
            )

    # -- 5. no target named: the verb family decides -------------------------
    if "interact" in families or _ordinal_phrase(text):
        # An interaction with nothing to interact with is the one case where
        # guessing a site is worse than asking. It happens when a previous turn
        # supplied the page and the context was trimmed, so the alternative list
        # records that possibility rather than assuming it.
        #
        # "Open the third result" counts as an interaction, not as `open`: there
        # is nothing to open, and the only thing it can mean is the page in front
        # of the user. Without this it fell through to a background web search,
        # which is how "she cannot click anything" reached the point of asking the
        # web for "the third result".
        if facts.browser_open:
            return Route(
                BROWSER,
                tool="browse",
                reason="asked to act on a page and one is already open",
                alternatives=(BACKGROUND,),
            )
        if _ordinal_phrase(text):
            return Route(
                BROWSER,
                tool="browse",
                reason="asked for something by position, which needs a page",
                question="Which page should I use? Open it first, or tell me the site.",
                ambiguous=True,
                alternatives=(BACKGROUND,),
            )
        # Not a page, and not a website: "click the save button", "type into the
        # search box", "press enter" name the window the user is already looking
        # at. Asking "which website?" about one of those is a question with an
        # answer the user has already given by looking at the screen.
        #
        # Checked *before* the ambiguous question rather than as an alternative,
        # because the alternative was never offered: a user who says "click save"
        # in Notepad is not choosing between a website and their own desktop.
        # A browser is open was handled above, so reaching here means there is
        # no page to mean -- which is exactly when the desktop is the target.
        if usable("desktop_control"):
            return Route(
                DESKTOP,
                tool="desktop_control",
                reason="asked to act on something on this computer's screen",
                notes={"surface": "desktop"},
            )
        return Route(
            BROWSER,
            tool="browse",
            reason="asked to operate something on a page",
            question="Which website should I do that on?",
            ambiguous=True,
            alternatives=(BACKGROUND, DESKTOP),
        )

    if "lookup" in families or "open" in families:
        # A bare "open X" with no known X and no address is the genuinely
        # ambiguous case the requirements ask about. Asking is the honest move:
        # opening two applications is a worse failure than one short question.
        if "open" in families and not _installed_hits(text, facts.ordered or facts.installed):
            preferred = _preferred_open(facts, usable)
            if preferred is not None:
                return preferred
            return Route(
                BACKGROUND,
                tool="search_web",
                reason="asked to open something I cannot resolve",
                question="What should I open? Give me the app or website name.",
                ambiguous=True,
                alternatives=(DESKTOP, BROWSER),
            )
        tool = "search_web"
        return Route(
            BACKGROUND,
            tool=tool,
            reason="a lookup, answered by the background tools",
            alternatives=(BROWSER,),
        )

    # -- 7. nothing actionable ----------------------------------------------
    return Route(
        BACKGROUND,
        reason="no tool call is implied",
        question="",
    )


#: "on youtube", "in instagram", "from netflix" -- the site is the *object* of
#: the verb rather than the subject of the sentence.
_ON_SITE = re.compile(r"\b(?:on|in|at|via|through|from)\s+(?:the\s+)?\w+", re.IGNORECASE)


def _browser_app_name(key: str) -> str:
    return {"chrome": "Google Chrome", "edge": "Microsoft Edge", "brave": "Brave"}.get(key, key.title())


def _preferred_open(facts: MachineFacts, usable: Callable[[str], bool]) -> "Route | None":
    """The route for an "open a website" that named no website.

    Only reached when the request is genuinely unresolved -- no address, no known
    app, no site. Both answers here are *preferences the user wrote down*, never
    guesses:

    * ``preferOwnBrowser`` means the isolated browser, which is the only one this
      product can actually drive;
    * ``default`` means the user's own desktop browser, which this product can
      launch and focus but cannot navigate.

    With neither set, this returns ``None`` and the caller asks. That is the right
    default: opening the wrong browser is visible to the user, and a question costs
    one short exchange.
    """
    if facts.own_browser_default and usable("browse"):
        return Route(
            BROWSER,
            tool="browse",
            reason="no site named, and the settings say to use Lumine's own browser",
            notes={"preferred": "own_browser"},
        )
    if facts.default_browser and usable("open_app"):
        return Route(
            DESKTOP,
            target=facts.default_browser,
            tool="open_app",
            reason="no site named, and the settings name a default browser",
            alternatives=(BROWSER,),
            notes={"preferred": "default_browser", "navigation": "unavailable"},
        )
    return None


def first_word(text: object) -> str:
    """A short handle for the request, for logs and ids."""
    parts = content_words(text)
    return " ".join(parts[:3])
