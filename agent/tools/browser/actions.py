"""Pure decisions for browsing: what a URL is, what a click means, what never gets typed.

No I/O lives here. URLs are parsed, hosts are judged against addresses
somebody already resolved, and an element's DOM *facts* become a permission
class -- so every rule is testable without a browser, and the page never
touches the decision.

Two directions are worth spelling out, because they are the security shape of
the whole tool:

- **The page can present facts; facts only ever push toward more asking.**
  :func:`classify_click` reads ``tag``/``type``/``in_form`` -- structure, not
  text. A page that lies about its structure can at worst make us ask a
  question we did not need; it cannot make a submission look like a harmless
  click, because the things that mark a submission are the ones it would have
  to hide, and hiding them changes what the browser actually does.
- **Refusals are not permissions.** A password field is refused outright by
  :func:`fill_deny_reason` -- no card, no timeout, no "allow once". A
  permission card is a question a person answers in seconds, and "type your
  password" is a question this product never asks.

## The one fact that pushes the other way, and why it is bounded

:func:`is_search_control` is the single exception to the rule above, and it is
deliberate rather than a leak. **A search form submits a query, not the user's
data**, so it is an ordinary ``act`` rather than a ``sensitive`` submission.
Without it "search YouTube for X" asked for permission, on the grounds that
Enter inside a ``<form>`` is HTML's implicit submission -- which is true of the
mechanism and false of the intent, and it made the most routine thing this
browser does the most interrupted.

The bound is written down rather than assumed, because this is the only place a
page-presented fact *lowers* a class:

- It requires a **positive** signal that the form is a search form --
  ``<form role="search">``, a ``searchbox`` control, or the form's only text
  field typed ``search``. A comment box, a checkout, a sign-in form and a
  contact form all still ask, because none of them carries one.
- What it moves is one class on one form. A hostile page gains what an ordinary
  click already gives it (``act``); it does not gain anybody's keystrokes,
  because Lumine only types what the user dictated, a password field is refused
  before the gate is reached (:func:`fill_deny_reason`), and every other
  submission is still ``sensitive``.
- It never reaches ``read``. A search does put the query on the wire, so it is
  an ``act`` -- the class :mod:`.search` has always reported for a tabulated
  site -- and never a pretence that nothing left the machine.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Sequence
from urllib.parse import urlsplit, urlunsplit

try:
    from ..tools_text import clip
    from ..tool_results import as_payload
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools_text import clip
    from tool_results import as_payload

#: The only schemes this tool opens. Everything else -- ``file:``,
#: ``javascript:``, ``data:``, ``chrome:`` -- reaches something that is not a
#: web page, and a URL is not a place to smuggle one past.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: References are minted by our own script in :mod:`.session` as ``r1``,
#: ``r2``... and interpolated into a ``querySelector``. Validating the shape
#: before it reaches a selector is what keeps ``ref`` from becoming selector
#: syntax: the page chooses its DOM, never our query language.
REF_RE = re.compile(r"^r\d{1,3}$")

#: How much of the page's own text and control list :func:`observe_payload`
#: keeps. A tool result joins the conversation permanently, so the ceiling is
#: the same one every other tool answers under -- `tool_results` -- and these
#: are the pieces that share it.
OBSERVE_TEXT_CHARS = 220
MAX_REPORTED_CONTROLS = 8
LABEL_CHARS = 40
TITLE_CHARS = 70
URL_CHARS = 90
#: A link's own address. Clipped hard because it is carried on every control of
#: every observe -- and a full query string is rarely the part that identifies
#: where a link goes. The host and path survive; a tracking tail does not.
HREF_CHARS = 80


def normalize_url(raw: object) -> str:
    """A clean ``http(s)`` URL, or ``ValueError`` with a reason code.

    Schemes are added (``example.com`` becomes ``https://example.com``) and
    the host is lowercased, because a model that half-remembers an address
    should get a page rather than an error; everything that could *reach
    something other than a page* is refused instead of repaired -- embedded
    credentials (a URL carrying a secret is not a URL we repeat back), other
    schemes, whitespace, and hosts that are not there.

    Raises ``ValueError``; callers translate it to a sentence the model can
    relay. The reason codes are short words so a test can pin which rule
    fired without matching prose.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty")
    if any(ch.isspace() or ord(ch) < 32 for ch in text):
        # A URL is never spelled with a space by anything that means a URL;
        # refusing is cheaper than guessing what the other bytes were for.
        raise ValueError("whitespace")
    if "://" not in text:
        # An authority with no scheme -- `user:pass@example.com` -- is a URL
        # carrying a secret. Refused before anything else, because the repair
        # below would otherwise fold it into nonsense and fail for an
        # unrelated reason. `@` inside a *path* (after the first `/`) is
        # ordinary and untouched.
        if "@" in text.split("/", 1)[0]:
            raise ValueError("credentials")
        # An explicit scheme without an authority -- `file:`, `data:`,
        # `javascript:`, `C:\...` -- is refused *before* any repair, because
        # prepending https:// to it produces nonsense that fails later for
        # the wrong reason. A host:port form (`example.com:8080`) is not a
        # scheme: its after-the-colon part starts with a digit, which is the
        # one thing a scheme can never do in this shape.
        declared = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):(.*)$", text)
        if declared and declared.group(1).lower() not in ALLOWED_SCHEMES:
            if not re.match(r"^\d", declared.group(2)):
                raise ValueError("scheme")
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        raise ValueError("unparsable") from None
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError("scheme")
    if "@" in parts.netloc:
        # user:pass@host -- credentials in a URL would be echoed into the
        # approval card, the audit digest, and the transcript. Not ours.
        raise ValueError("credentials")
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise ValueError("unparsable") from None
    if not host:
        raise ValueError("no host")
    host = host.casefold()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


def hostname_of(url: object) -> str:
    """The lowercased host of a URL, for messages a person reads back."""
    try:
        return (urlsplit(str(url or "")).hostname or "").casefold()
    except ValueError:
        return ""


def host_verdict(
    hostname: object, resolved: Sequence[str], *, allow_local: bool = False
) -> tuple[bool, str]:
    """``(allowed, why-not)`` for one host, given addresses already resolved.

    The default posture is **public internet only**. ``localhost``, private
    ranges, link-local, and mDNS names are refused -- not because a router's
    admin page is secret, but because a page the assistant opened must not be
    able to aim the browser at the user's own network. That is the classic
    pivot: a hostile (or merely careless) site scripting ``http://192.168.1.1``
    through a browser the user was told to trust.

    ``allow_local`` is the documented escape hatch for someone who *wants*
    their assistant to reach the NAS. Default off, checked per call so the
    setting is a setting and not a recompile.

    Resolution failure refuses: a host we cannot stand behind is a host we do
    not open.
    """
    if allow_local:
        return True, ""
    host = str(hostname or "").strip().strip(".").casefold()
    if not host:
        return False, "it has no host"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False, "it is a local name"
    addresses = [str(item) for item in resolved if str(item)]
    if not addresses:
        # A literal IP needs no resolver; anything else that arrives here
        # simply did not resolve.
        try:
            addresses = [str(ipaddress.ip_address(host.strip("[]")))]
        except ValueError:
            return False, "it could not be resolved"
    for raw in addresses:
        candidate = raw.split("%", 1)[0]  # strip an IPv6 zone id
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            return False, "one of its addresses could not be read"
        if not ip.is_global:
            return False, f"{candidate} is not a public address"
    return True, ""


# -- clicks -----------------------------------------------------------------


#: Input types that submit their form. ``image`` is here because the spec
#: makes it a submitter, not because the name looks dangerous.
SUBMIT_TYPES = frozenset({"submit", "image"})


#: The ``role`` a control carries when it is a search box, and the ``type`` of a
#: search input. Both are HTML's own vocabulary rather than words a page chose
#: for a label, which is what makes them usable here at all: a page cannot
#: rename a checkbox into a search box without changing what the element is.
SEARCH_ROLE = "searchbox"
SEARCH_INPUT_TYPE = "search"


def is_search_control(
    *,
    tag: object = "",
    type_: object = "",
    role: object = "",
    in_form: object = False,
    form_role: object = "",
    sole_field: object = False,
) -> bool:
    """Whether this control is the search box of a search form.

    The narrowest question that answers "is submitting this a *search*?", asked
    from structure only. Three ways to say yes, and a plain text input in a
    plain ``<form>`` is none of them:

    - the ``<form>`` itself declares ``role="search"`` -- YouTube, Google and
      Amazon all do, and it is the strongest signal because it describes the
      form rather than the field;
    - the control reports the ``searchbox`` role and is **not** a lone
      ``type=search`` field in a larger form.

    The sole-field requirement on ``type=search`` is the sharp edge, and it is
    needed because ``state``'s ``roleOf`` *derives* ``searchbox`` from
    ``type=search``. Without it that derivation would quietly satisfy the
    ``searchbox`` branch for every filter box on every site, and the whole
    function would collapse into "any ``type=search`` field is a search form".
    A filter box is not a search form: it narrows what is already on screen
    rather than asking another server for something.

    ``sole_field`` is the fact ``state`` already collects for exactly this
    shape -- Google's field is ``name="q"`` with nothing in its label that says
    "search", so a search box on the most-used search engine has no label to
    read and this is the only signal left.
    """
    if not in_form:
        # Nothing to submit, so the question is moot and the caller falls
        # through to the ordinary rules.
        return False
    if str(form_role or "").strip().casefold() == "search":
        return True
    typed = str(type_ or "").strip().casefold()
    if typed == SEARCH_INPUT_TYPE:
        # Only when it is the form's sole field -- see the docstring.
        return bool(sole_field)
    return str(role or "").strip().casefold() == SEARCH_ROLE


def classify_click(
    *,
    tag: object,
    type_: object,
    in_form: bool,
    search: bool = False,
) -> str:
    """The permission class for clicking an element described by DOM facts.

    ``sensitive`` when the control submits a form -- the moment something the
    user typed moves from their screen toward someone else's server -- and
    ``act`` otherwise. Facts only: this function is never shown a label, a
    title, or any other word the page chose.

    ``search`` is the one caller-supplied fact that lowers the class, and only
    from ``sensitive`` to ``act``: a search form submits a query rather than the
    user's data, so submitting it is an ordinary act. See
    :func:`is_search_control` for why that is bounded, and note it never
    reaches ``read`` -- the query did go to somebody's server.

    The honest limit, written down so nobody mistakes this for more than it
    is: a page's own JavaScript can send data on any click it likes, and no
    classification of button shapes can see that. What this controls is our
    side of the line -- **every form submission asks**, ordinary clicks are
    acts (automatic by default, and the whole tool can be tightened to
    ``ask`` per profile), and the flows that *know* they are sending
    something -- WhatsApp's send in the next phase -- gate themselves with a
    read-back rather than by guessing at a button's shape.
    """
    name = str(tag or "").casefold()
    kind = str(type_ or "").casefold()
    if search:
        return "act"
    if name == "button":
        # HTML's own rule: a <button> inside a form submits unless it says
        # otherwise; outside a form it is an ordinary control.
        if kind in ("", "submit") and in_form:
            return "sensitive"
        return "act"
    if name == "input" and kind in SUBMIT_TYPES:
        return "sensitive"
    if kind in SUBMIT_TYPES:
        # Not a form control we recognise, but claiming to be one: the
        # dangerous direction is being wrong toward asking, never away.
        return "sensitive"
    return "act"


def fill_deny_reason(*, tag: object, type_: object) -> str | None:
    """Why this field must not be filled, or ``None``.

    A refusal, not a permission class: see the module docstring. The same
    check is repeated inside the page at the moment of typing (in `session`),
    so a page that swaps elements under a reference between two calls still
    cannot receive a keystroke in a password field.
    """
    if str(tag or "").casefold() == "input" and str(type_ or "").casefold() == "password":
        return "that is a password field, and Lumine never types passwords"
    return None


# -- keys -------------------------------------------------------------------


#: The keys this tool will press, spelled as the browser spells them.
#:
#: A closed set rather than a pass-through, because a key is a chord with
#: the operating system: ``Ctrl+W`` closes the window, ``Alt+F4`` closes the
#: app, ``F12`` opens a debugger, and no classification of DOM facts can see
#: any of them coming -- they never reach the page. What is left after
#: refusing those is navigation and caret movement, all of it aimed at a
#: control the model named by reference.
KEYS: frozenset[str] = frozenset(
    {
        "Enter",
        "Escape",
        "Tab",
        "Backspace",
        "Delete",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        " ",  # space: typed as a character, and a press on a button
    }
)

#: Spellings a model uses that the browser spells differently. Aliases are
#: resolved *into* the closed set above, never added to it.
KEY_ALIASES: dict[str, str] = {
    "space": " ",
    "spacebar": " ",
    "esc": "Escape",
    "escapekey": "Escape",
    "return": "Enter",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "pageup": "PageUp",
    "pagedown": "PageDown",
}

_KEY_LOOKUP: dict[str, str] = {name.casefold(): name for name in KEYS}
_KEY_LOOKUP.update({alias.casefold(): name for alias, name in KEY_ALIASES.items() if alias.casefold() not in _KEY_LOOKUP})


def normalize_key(raw: object) -> str:
    """One of :data:`KEYS`, or ``ValueError`` with a reason code.

    ``ValueError`` rather than a silent substitution: a key that was refused
    must be reported as refused, because pressing a *different* key than the
    one asked for is the one outcome nobody can undo.
    """
    original = str(raw if raw is not None else "")
    text = original.strip() or (" " if original == " " else "")
    if not text:
        raise ValueError("empty")
    found = _KEY_LOOKUP.get(text.casefold())
    if found is None:
        raise ValueError("unknown")
    return found


#: Keys that can *activate* the focused control rather than only edit it.
#: A press of one of these on a button or link is a click by another route,
#: so it is classified by the same DOM facts a click is.
ACTIVATING_KEYS = frozenset({"Enter", " "})


def classify_key(
    key: object, *, tag: object, type_: object, in_form: bool, search: bool = False
) -> str:
    """The permission class for pressing a key on a control described by DOM facts.

    Three rules, in order:

    - **A search box is a search, not a submission.** ``search`` comes from
      :func:`is_search_control`, and it is checked *before* the Enter-in-a-form
      rule below because that rule is the one being excepted. "Search YouTube
      for X" typed a query and pressed Enter inside a ``<form>``, and asking for
      permission to do it made the most ordinary thing this browser does the
      thing a user interrupts it for. It lands on ``act``, which never asks.
    - **Enter inside a form submits it** -- HTML's implicit submission -- so it
      is ``sensitive`` on any *other* form, whatever the control looks like.
      That is data moving toward someone else's server, which is the line this
      whole module holds.
    - **A key that can activate the focused control is a click**, and clicks
      are classified by the DOM (:func:`classify_click`), not by the key.

    Everything else -- arrows, Escape, PageDown -- only moves focus or the
    caret, and is an ``act``. As with :func:`classify_click`, a page's own
    handlers can do anything on any keypress; what this decides is our side
    of the line.
    """
    name = str(key or "")
    if search:
        return "act"
    if name == "Enter" and in_form:
        return "sensitive"
    if name in ACTIVATING_KEYS:
        return classify_click(tag=tag, type_=type_, in_form=in_form)
    return "act"


# -- scrolling --------------------------------------------------------------


#: Where :func:`normalize_scroll` may send the page. ``down`` is what an
#: empty argument means, because "scroll" on its own is a request to see
#: what is below.
SCROLL_DIRECTIONS = ("down", "up", "top", "bottom")


def normalize_scroll(raw: object) -> str:
    """One of :data:`SCROLL_DIRECTIONS`; empty means ``down``.

    Refused rather than repaired for the same reason as keys: a scroll that
    goes the wrong way is a page the user watches move in a direction they
    did not ask for.
    """
    text = str(raw or "").strip().lower()
    if not text:
        return "down"
    if text not in SCROLL_DIRECTIONS:
        raise ValueError("unknown")
    return text


# -- tabs --------------------------------------------------------------------
#
# One verb, a handful of operations, chosen by the argument. That is the shape
# `scroll` already has here -- a verb plus a `text` that says which way -- and the
# reason is the same: each of these is one decision about the *browser* rather
# than about a control on a page, so none of them needs its own `ref`, its own
# gate sentence, or a place in the model's memory of a tool's verbs.
#
# Parsed here rather than in the tool because it is pure: every accepted spelling
# and every refusal is a unit test, with no browser and no network.
#
# **This parses a sentence, it does not match a shape.** The first version wanted
# an exact word and refused everything else, and the measured cost was that almost
# nothing anyone actually says worked: "delete the 1st tab", "close the first
# tab", "tab 2", "switch to tab 3" and "what tabs" were all refused. A refusal
# here is not a dead end -- the model retries, and a retry is where a *second
# approval card* comes from, which is worse than the tab question ever was.
#
# Worse, three phrasings were silently wrong rather than refused: "open a new tab"
# parsed as a new tab whose address is the string "a new tab", and tried to
# navigate to `https://a new tab`. So the rule is the opposite one: find the
# operation and the target *anywhere* in the sentence, and refuse only what is
# genuinely ambiguous.

#: What `text` may ask for. Closed for the same reason keys and scroll
#: directions are: an unrecognised request is refused by name rather than turned
#: into a guess about which tab the model meant.
TAB_OPS = ("list", "new", "switch", "close", "close_others")

#: Words that mean each operation, in the spelling people use. "delete" is here
#: because it is the word that was actually used -- a tab is deleted, not closed,
#: and a request for the ordinary English word must not come back refused.
_CLOSE_WORDS = frozenset(
    {"close", "delete", "remove", "dismiss", "quit", "exit", "kill", "discard", "shut"}
)
_SWITCH_WORDS = frozenset(
    {"switch", "go", "jump", "return", "back", "next", "previous", "forward", "back"}
)
_LIST_WORDS = frozenset({"list", "show", "what", "which", "how", "many", "see", "view"})
_NEW_WORDS = frozenset({"new", "another", "fresh", "extra", "additional"})

#: "open" is both a new tab and a switch, and the difference is whether "new" is
#: also in the sentence: "open tab 4" is a switch to tab 4, "open a new tab" is a
#: new one. Without this, "open tab 4" was read as a new tab *addressed to* the
#: string "tab 4".
_OPEN_WORDS = frozenset({"open", "launch", "start", "load", "browse", "visit"})

#: Ordinals, spelled and suffixed. "the 1st tab" and "the first tab" are the same
#: request and both are how it gets said -- and `router._ORDINAL_WORDS` already
#: exists because speech-to-text produces them.
#:
#: Bare cardinals are deliberately absent: "one" appears in "no one", "anyone"
#: and "one of them", and reading it as tab 1 would close something the user never
#: named. "second" is absent for the same reason from the *new*-tab words below --
#: it is far more often "the second tab" than "open a second tab", and the wrong
#: reading there is a new tab where a switch was meant.
TAB_ORDINALS: dict[str, str] = {
    "first": "1", "1st": "1",
    "second": "2", "2nd": "2",
    "third": "3", "3rd": "3",
    "fourth": "4", "4th": "4",
    "fifth": "5", "5th": "5",
    "sixth": "6", "6th": "6",
    "seventh": "7", "7th": "7",
    "eighth": "8", "8th": "8",
    "ninth": "9", "9th": "9",
    "tenth": "10", "10th": "10",
    #: Its own marker rather than ``-1``: a negative index would collide with
    #: "previous", and "delete the last tab" is a request for a *specific* tab.
    "last": "last",
}

#: Bare cardinals, which are how "close tab two" arrives. Present for every one
#: except "one": it appears in "no one", "anyone" and "one of them", and there is
#: no reading of "close one" that is safe, so it is handled as unresolvable below.
_TAB_CARDINALS: dict[str, str] = {
    "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}

#: A token that *looks* like it was naming a tab but is not one this accepts --
#: "1.5", "one". Combined with "no target anywhere", this is the one thing worth
#: refusing, and only for `close`: reading it as "the tab in use" would destroy a
#: page the user never named, which is the exact failure this parser exists to
#: stop. For a switch the same reading is harmless, so it is allowed there.
_BAD_TARGETS = frozenset({"one", "0"})

#: Words for the tab already in use. "close this tab" and a bare "close" mean the
#: same thing, which is the only reason a target is optional at all.
_THIS_WORDS = frozenset({"this", "that", "it", "current", "here", "same"})

#: Words for a tab relative to the one in use. `+1` and `-1` are resolved against
#: the registry, which is the only place that knows the order. "others" and "rest"
#: are here so "close the other tabs" can be told from "close the other tab".
_RELATIVE = {
    "other": "+1", "others": "+1", "another": "+1", "next": "+1", "following": "+1",
    "previous": "-1", "prev": "-1", "back": "-1", "preceding": "-1",
    "rest": "+1", "remaining": "+1",
}

#: Filler to drop from a "new" request before deciding whether what is left is an
#: address. This is the difference between "new tab https://example.com" (a real
#: address) and "open a new tab" (nothing at all).
_TAB_FILLER = frozenset({"tab", "tabs", "a", "an", "the", "for", "with", "to", "of", "and", "please"})

#: Where an address starts inside a sentence. Deliberately narrow: the failure
#: being avoided is navigating to `https://a new tab`, and a search phrase is
#: better answered by no address at all than by a broken one.
#:
#: Found by searching the **original** text rather than by deleting the words
#: around it. Both failures that mattered came from that: deleting a fixed filler
#: list missed "at" in "a new tab at https://...", and tokenising turned
#: `https://example.com` into `https example com` -- which then reassembled as the
#: address `com`.
_URL_START = re.compile(
    r"(?:https?://|www\.)[\w\-./#?=&%:@+~]*"
    r"|[\w-]+\.(?:com|org|net|io|co|dev|app|uk|de|fr|es|jp|in|bd|ca|au|tv|me)"
    r"(?:[/\w?#=&%+-]*)?",
    re.IGNORECASE,
)


#: What each tab operation costs the user in attention.
#:
#: **Reading, switching and opening are `read`, and that is the whole of the "she
#: always asks" report.** The test used everywhere in this file is whether the act
#: changes anything *off* the screen, and switching tabs does not: it changes which
#: of the pages the agent already opened is in front, exactly as `scroll` moves a
#: viewport and `back` walks history -- all reads. Asking for it was asking about
#: the agent's own housekeeping, on a voice turn where every card is another
#: second the user spends being interrupted.
#:
#: Opening a tab is a read for the reason `open` is one: it puts a page on screen
#: and nothing else.
#:
#: Closing is an `act`, because it destroys a page -- and the asymmetry is the
#: point. The operations that cannot lose anything never interrupt; the ones that
#: can always do. Lives here rather than in either tool because **both** browser
#: tools drive one browser, and two copies of this table is how they end up
#: charging the user differently for the same request.
TAB_ACTION_CLASS: dict[str, str] = {
    "list": "read",
    "switch": "read",
    "new": "read",
    "close": "act",
    "close_others": "act",
}

#: The verb used when a tab operation *does* ask.
#:
#: Shared by both tools on purpose. `permission_gate` collapses two pending cards
#: into one by matching (tool, verb, what it would do) -- and it cannot collapse
#: two cards that name themselves differently. When `browse` asked with the verb
#: `tab` and `web_control` with `switchtab`, a model that tried one and then the
#: other produced **two** cards for one request, which is the "asks twice, answered
#: twice" report. One vocabulary, and the machinery already in the gate does the
#: rest.
TAB_ASK_VERB = "tab"


def _tokens(raw: object) -> list[str]:
    # Numbers keep their decimal point, so "1.5" is one token that resolves to
    # nothing rather than two tokens where the first one resolves to tab 1.
    return re.findall(r"[a-z]+|\d+(?:\.\d+)?", str(raw or "").casefold())


def _target_in(words: list[str]) -> str | None:
    """The tab a sentence names, or ``None`` when it names none.

    Order matters: a digit or an ordinal is a name, this/that is "the one in
    use", and a relative word is next-or-previous. The first match wins, so
    "close the first tab" is tab 1 and not "the tab called first".
    """
    for word in words:
        if word.isdigit():
            return word
        if word in TAB_ORDINALS:
            return TAB_ORDINALS[word]
        if word in _TAB_CARDINALS:
            return _TAB_CARDINALS[word]
    for word in words:
        if word in _THIS_WORDS:
            return ""
        if word in _RELATIVE:
            return _RELATIVE[word]
    return None


def _names_no_tab(words: list[str]) -> bool:
    """Whether the sentence tried to name a tab and did not manage it.

    "1.5" and "one" are the cases: something was aimed at a specific tab and no
    tab matches. Only consulted for `close`, where reading it as "the one in use"
    would destroy a page nobody named.
    """
    return any(word in _BAD_TARGETS or re.fullmatch(r"\d+\.\d+", word) for word in words)


def parse_tab_request(raw: object) -> tuple[str, str]:
    """``(op, arg)`` from a sentence about tabs.

    Empty means ``list``: asking about tabs without saying what to do is a
    question about them, the same defaulting :func:`normalize_scroll` does.

    The target is looked for anywhere rather than in a fixed slot, and an
    operation with no target means the tab in use -- ``close`` and ``delete`` with
    nothing after them are both a complete request.

    ``close_others`` is its own operation rather than a target, because "close the
    other tabs" destroys several pages and "close the last one" destroys one, and
    a tool that cannot tell them will do one when asked for the other.

    Refused only for a sentence with no operation *and* no target in it. Anything
    else resolves to the operation it names, because a refusal costs a retry and
    a retry costs a second approval card.
    """
    words = _tokens(raw)
    if not words:
        return "list", ""

    target = _target_in(words)
    relative = target in ("+1", "-1")
    closing = bool(_CLOSE_WORDS & set(words))

    if closing and relative:
        # "the other tab" and "the other tabs" are both "the rest of them" as far
        # as closing goes -- there is no way to know which single one was meant, so
        # this is the reading that never destroys a page the user kept.
        return "close_others", ""
    if closing:
        # The one refusal this parser makes. A sentence that aimed at a specific
        # tab and named none cannot be closed "safely" as the tab in use -- that
        # destroys a page the user never mentioned, which is the failure that
        # sent the report in the first place.
        if target is None and _names_no_tab(words):
            raise ValueError("ambiguous")
        return "close", target if target is not None else ""

    if _NEW_WORDS & set(words):
        return "new", _address_after(raw)

    if _SWITCH_WORDS & set(words):
        # "back to the first tab" carries both a switch word and an ordinal, and
        # the ordinal is the target. A switch that named nothing means *another*
        # tab, which is what "switch tabs" asks for; a switch that named this one
        # means the tab in use, and `_target_in` caught that as "".
        return "switch", target if target is not None else "+1"

    if _OPEN_WORDS & set(words):
        # No "new" in the sentence, so this is going *to* a tab that exists.
        if target is not None:
            return "switch", target
        return "new", _address_after(raw)

    if _LIST_WORDS & set(words):
        return "list", ""

    if target is not None:
        # A bare "2" or "tab 3" is a switch, with no verb in front of it.
        return "switch", target

    # Nothing recognisable. Not an error -- asking about tabs is the default
    # reading, and a refusal here is what sends the model looking for another way.
    return "list", ""


def _address_after(raw: object) -> str:
    """An address for a new tab, or ``""`` when the sentence named none.

    Cut out of the original text from wherever the address starts, so nothing
    about the words around it can end up inside the address.
    """
    match = _URL_START.search(str(raw or ""))
    return match.group(0).strip(" .,:;-") if match else ""


# -- observe ----------------------------------------------------------------


def observe_payload(
    *, title: object, url: object, text: object, controls: Sequence[object]
) -> str:
    """The page as a compact payload: title, address, text, and controls.

    JSON with a fixed shape rather than a paragraph, because the model reads
    this to decide its *next* action -- it needs the reference of the third
    button, not a description of the page's mood. Every piece is clipped
    before it joins the budget, so the whole answer fits the same ceiling
    every other tool result answers under.
    """
    reported: list[dict[str, str]] = []
    for item in controls[:MAX_REPORTED_CONTROLS]:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "")
        if not REF_RE.match(ref):
            continue
        kind = str(item.get("tag") or "").casefold()
        entry_type = str(item.get("type") or "").casefold()
        if entry_type and kind:
            kind = f"{kind}:{entry_type}"
        entry = {
            "ref": ref,
            "kind": kind,
            "label": clip(str(item.get("label") or kind or "control"), LABEL_CHARS),
        }
        # The real address, for links. This was the missing half of "observe": a
        # model shown eight labels and no hrefs cannot open the thing it is
        # looking at, so on a results page its only move is to *invent* an address
        # -- and an invented one is a dead end that reports itself as a broken
        # page rather than as a guess. A link it can read is a link it can use.
        href = str(item.get("href") or "").strip()
        if href and kind.startswith("a"):
            entry["href"] = clip(href, HREF_CHARS)
        reported.append(entry)
    body = " ".join(str(text or "").split())[:OBSERVE_TEXT_CHARS]
    return as_payload(
        {
            "title": clip(str(title or ""), TITLE_CHARS),
            "url": clip(str(url or ""), URL_CHARS),
            "text": body,
            "controls": reported,
        }
    )
