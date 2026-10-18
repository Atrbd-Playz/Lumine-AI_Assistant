"""Where on a given site the *content* is, and what its fields are called.

## The problem this solves

A page read that returns the whole `body.innerText` is a read that answers
nothing on the pages people actually ask about. A product page's body text is a
cookie banner, a navigation drawer, a recommendation rail and then -- somewhere
after eight hundred characters of everything else -- the price. The reported
failure was exactly that: the tool read the page, the model was told what the
page said, and it could not say what the thing cost.

The fix is not a larger budget. A larger budget reads *more* navigation. The fix
is to know where the site keeps its content, and to read that region instead.

## What an entry is

One row per host:

* ``content`` -- a CSS selector for the region holding the page's substance.
* ``fields`` -- a map of a *name* to a selector inside that region, so "price"
  comes back under the key ``price`` rather than as a sentence containing a
  currency glyph somewhere in a paragraph.

Everything is optional. A site with no entry reads through the generic ladder in
:mod:`.state`, which already knows about ``main``, ``[role=main]``, ``article``
and ``#content``; an entry exists only where that ladder is not good enough.

## What this is *not*, and why the two tables stay apart

:mod:`.sites` answers "how do I *search* on this site" -- which input receives
keystrokes and which control submits them. This module answers "once I am here,
what am I looking at". They share a host column and nothing else, and merging
them would be wrong in both directions: a site can be perfectly searchable and
have no content map at all (a search results page is its own content), and a
site can have a content map and no search entry because nobody has ever asked to
search it.

Keeping them apart also keeps the failure modes apart. A missing *search* entry
degrades to the semantic fallback and says so; a missing *content* entry degrades
to the generic ladder, which is a good answer on almost every site. One table
with both would have to pick one of those two behaviours for both cases.

## Reading the file

The file is read on demand and cached against its own ``(mtime, size)``, so
editing ``sites_map.yaml`` takes effect on the next read with no worker restart,
and an unedited file costs one ``stat`` rather than one parse. ``PyYAML`` is
imported lazily and its absence is a warning rather than an import failure: a
missing optional dependency must not be able to stop the browser tools from
loading, which is the difference between "per-site refinement is off" and "the
tool does not exist".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

try:
    from .locators import LocatorError, valid_css
except ImportError:  # running as a top-level module with agent/ on sys.path
    from locators import LocatorError, valid_css  # type: ignore[no-redef]

logger = logging.getLogger("lumine")

#: Where the shipped map lives, next to this module. Overridable because the
#: right map is machine- and account-specific: the selectors that survive a
#: redesign on one locale do not necessarily survive it on another.
DEFAULT_MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sites_map.yaml")

#: Set to point at a different map. Empty is treated as unset, because an
#: environment variable left over from an experiment must not be able to disable
#: the shipped map by being blank.
ENV_MAP_PATH = "LUMINE_BROWSER_SITE_MAP"

#: The most entries and fields one read will honour. A map is a hand-maintained
#: file, so this is a ceiling against a mistake rather than a target -- but a
#: runaway file must not be able to turn every page read into a selector storm.
MAX_ENTRIES = 200
MAX_FIELDS_PER_ENTRY = 24


@dataclass(frozen=True)
class SiteEntry:
    """Where one host keeps its content, and what its fields are called."""

    host: str
    #: A CSS selector for the content region. Empty means "use the generic
    #: ladder", which is an honest state and not a missing value.
    content: str = ""
    #: Field name -> selector, relative to the content region. Relative rather
    #: than absolute on purpose: a page whose price moved to a new wrapper still
    #: has one price inside its own content, and an absolute selector would have
    #: to be rewritten with it.
    fields: Mapping[str, str] = field(default_factory=dict)

    def is_usable(self) -> bool:
        return bool(self.host) and bool(self.content or self.fields)


#: ``(path, mtime_ns, size) -> entries``. One entry in the cache, because the
#: common case is one map read over and over; a list would be a second thing to
#: evict for a case that does not occur.
_CACHE: dict[tuple[str, int, int], dict[str, SiteEntry]] = {}
#: Whether the "no PyYAML" warning has been said. Once per process: a warning
#: that fires on every page read is noise, and noise is how a real warning
#: stops being noticed.
_WARNED_NO_YAML = False


def map_path() -> str:
    """The map file this process should read."""
    override = str(os.environ.get(ENV_MAP_PATH) or "").strip()
    return override or DEFAULT_MAP_PATH


def _read_yaml(path: str) -> Any:
    """Parse the file, or return ``None`` when it cannot be parsed.

    ``None`` is distinct from ``{}``: an absent map and an empty map are both
    "no entries", but a *broken* map is a mistake somebody should hear about,
    and swallowing it into an empty dict would make that mistake invisible until
    a selector silently stopped matching.
    """
    global _WARNED_NO_YAML
    try:
        import yaml  # noqa: PLC0415 -- deliberately lazy; see the module docstring
    except ImportError:
        if not _WARNED_NO_YAML:
            _WARNED_NO_YAML = True
            logger.warning(
                "PyYAML is not installed, so per-site content maps are off. "
                "Install it with: pip install pyyaml"
            )
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        # A file we cannot read is not a file with no entries; say which.
        logger.warning("[site_map] could not read %s: %s", path, exc)
        return None
    except yaml.YAMLError as exc:  # type: ignore[attr-defined]
        logger.warning("[site_map] %s is not valid YAML: %s", path, exc)
        return None


def _host_of(value: object) -> str:
    """A lowercase hostname from whatever the file gave us.

    Accepts a bare host or a URL, because both spellings show up in hand-written
    configuration and refusing the shorter one is a papercut with no upside.
    """
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    if "://" in text:
        try:
            text = urlsplit(text).hostname or ""
        except ValueError:
            return ""
    # A trailing dot is a valid absolute name and never what a map means.
    text = text.strip(".").strip()
    # A leading ``www.`` is dropped, because the map is hand-written and the
    # entry is nearly always copied out of an address bar. Keeping the label
    # would mean ``www.example.com:`` in the file silently fails to match a page
    # served from ``example.com`` -- and the mismatch is invisible, since the
    # read then falls back to the generic ladder and looks like a map that never
    # mentioned the site at all. ``match`` already covers the other direction
    # (an ``amazon.in`` entry serving ``www.amazon.in``) by matching on labels,
    # so dropping it here makes both spellings of one site one entry.
    #
    # Only ``www`` and only as a whole label: ``m.youtube.com`` is a genuinely
    # different surface and stays written the way it was. The trailing dot is
    # already gone above, so this slice can never leave an empty string.
    if text.startswith("www."):
        text = text[4:]
    return text


def _clean_selector(raw: object) -> str:
    """A selector from the file, or ``""`` when it is not one we would run.

    Validated rather than trusted for the same reason :func:`locators.valid_css`
    exists: this is a language handed to the browser. A selector that fails the
    check is *dropped and logged*, not raised -- the file is configuration, and a
    typo in configuration must not take the tool down; it must make that one
    site fall back to the generic ladder, which is the correct degradation.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return valid_css(text)
    except LocatorError:
        logger.warning("[site_map] ignoring an unusable selector: %r", text[:120])
        return ""


def parse(document: Any) -> dict[str, SiteEntry]:
    """Entries from a parsed document. Pure, so it is testable without a file."""
    if not isinstance(document, Mapping):
        return {}
    out: dict[str, SiteEntry] = {}
    for raw_host, raw_body in document.items():
        if len(out) >= MAX_ENTRIES:
            break
        host = _host_of(raw_host)
        if not host:
            continue
        body = raw_body if isinstance(raw_body, Mapping) else {}
        content = _clean_selector(body.get("content"))
        fields: dict[str, str] = {}
        raw_fields = body.get("fields")
        if isinstance(raw_fields, Mapping):
            for name, selector in raw_fields.items():
                if len(fields) >= MAX_FIELDS_PER_ENTRY:
                    break
                key = str(name or "").strip()[:60]
                value = _clean_selector(selector)
                if key and value:
                    fields[key] = value
        entry = SiteEntry(host=host, content=content, fields=fields)
        if entry.is_usable():
            out[host] = entry
    return out


def load(path: str | None = None) -> dict[str, SiteEntry]:
    """The map, cached against the file's own identity.

    The cache key is ``(path, mtime_ns, size)`` rather than a boolean "already
    loaded", because the map is meant to be edited while the worker runs -- it is
    how a site's redesign gets fixed. A key without the identity would mean the
    only way to pick up a fix is a worker restart, and a key with only the mtime
    would miss an edit that lands in the same clock tick.
    """
    target = path or map_path()
    try:
        stat = os.stat(target)
    except OSError:
        # No map at all is the ordinary case on a fresh install.
        return {}
    key = (target, stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    entries = parse(_read_yaml(target))
    _CACHE.clear()
    _CACHE[key] = entries
    return entries


def match(url: object) -> SiteEntry | None:
    """The entry for this URL's host, or ``None``.

    Suffix matching on host labels, so ``amazon.in`` covers ``www.amazon.in``
    and ``smile.amazon.in`` while ``notamazon.in`` covers nothing -- the latter
    is the whole reason this is label-wise rather than a string suffix, and a
    plain ``endswith`` would have let one site claim another's selectors.
    """
    try:
        host = (urlsplit(str(url or "")).hostname or "").casefold()
    except ValueError:
        return None
    if not host:
        return None
    entries = load()
    best: SiteEntry | None = None
    best_len = -1
    for known, entry in entries.items():
        if host == known or host.endswith("." + known):
            if len(known) > best_len:
                best, best_len = entry, len(known)
    return best
