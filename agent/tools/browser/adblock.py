"""Block the ad and tracker hosts a page asks for, before they are asked.

Why this lives here and not in a browser extension: `browse` owns its own
persistent Chromium profile and there is no extension pipeline behind it -- and
more to the point, an extension would be a third-party list arriving over the
network on every launch. What follows is a small, *local*, readable list, and
the whole thing is one function the route guard calls.

Three decisions carry it, and each was a bug first in the planning:

- **The block happens in the route guard, before DNS.** ``_guard`` already sees
  every request the page makes; asking ``should_block`` *before* the host
  resolution means a blocked request never leaves the machine, which is both
  faster and the only version that actually blocks a tracker.
- **It never blocks a media host.** ``*.googlevideo.com`` is where YouTube's
  video segments arrive. A list that blocked it would "work" in every test that
  does not play a video and break playback in the one case a user notices --
  which is precisely the ~49 s stall this module was written beside. The media
  hosts are named rather than merely absent, so adding a new block cannot
  silently add them back.
- **The switch is an environment variable that ships ON.** ``0``/``false``/
  ``off`` disable it, matching :data:`.session.HEADLESS_VAR`'s spellings, so a
  page that genuinely breaks under blocking has an escape hatch that does not
  require a rebuild.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

#: The switch. Off is `0`/`false`/`off`; **anything else, including an
#: unrecognised value, is on**, because this key only ever *removes* something
#: nobody asked for. A typo turning the blocker off silently would read as the
#: blocker not working, which is the same class of mistake as the
#: ``desktop.input`` fail-open/fail-closed pair -- and the answer here is the
#: opposite one because the cost of a wrong read is a few unblocked requests,
#: not an act on somebody's desktop.
ADBLOCK_VAR = "LUMINE_BROWSER_ADBLOCK"

#: Host suffixes that are advertising or tracking infrastructure. Matched on the
#: **host boundary**: `doubleclick.net` matches `ad.doubleclick.net` and
#: `doubleclick.net`, and does *not* match `notdoubleclick.net` -- a plain
#: substring test would block a real site whose name merely contains one of
#: these.
HOST_SUFFIXES: tuple[str, ...] = (
    # Google's advertising stack.
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "google-analytics.com",
    "googletagmanager.com",
    "googletagservices.com",
    "adservice.google.com",
    # GA4's collection endpoint. Named as its own host rather than by the
    # broader `google.com`, which would take Google Search, Maps and every
    # other Google page with it -- the block has to be the tracker, not the
    # company.
    "analytics.google.com",
    # The rest of the usual network ad buyers and exchanges.
    "adnxs.com",
    "adsrvr.org",
    "adtechus.com",
    "pubmatic.com",
    "rubiconproject.com",
    "criteo.com",
    "criteo.net",
    "taboola.com",
    "outbrain.com",
    "amazon-adsystem.com",
    "scorecardresearch.com",
    "quantserve.com",
    "connect.facebook.net",
    "facebook.net",
    "moatads.com",
    "smartadserver.com",
    "openx.net",
    "zedo.com",
    "teads.tv",
    "sharethrough.com",
    "yieldmo.com",
    "media.net",
    "casalemedia.com",
    "contextweb.com",
    "bidswitch.net",
    "33across.com",
    "eyeota.net",
    "rlcdn.com",
    "adform.net",
    "simpli.fi",
    "sitescout.com",
    "turn.com",
)

#: URL paths that are advertising or tracking, for a host the list does not
#: name. These are matched against the *path*, so a first-party analytics beacon
#: on a site's own domain is caught without blocking the domain itself.
PATH_PATTERNS: tuple[str, ...] = (
    "/pagead/",
    "/adsense",
    "/adsbygoogle",
    "/api/stats/ads",
    "/ptracking",
    "/analytics.js",
    "/gtm.js",
    "/gtag/js",
)

#: Hosts that must **never** be blocked, checked before the block lists. These
#: are the ones whose absence is a user-visible break rather than a missing ad:
#: YouTube's video segments arrive from ``googlevideo.com``, and its thumbnail
#: and caption hosts carry the page's own content. Named rather than merely
#: absent, so the next person to add a suffix has to walk past this list.
NEVER_BLOCK_HOSTS: tuple[str, ...] = (
    "googlevideo.com",
    "ytimg.com",
    "youtube.com",
    "ggpht.com",
    "lumine.local",
)


def enabled() -> bool:
    """Is ad blocking on? Default **on**; `0`/`false`/`off` disable it."""
    raw = (os.getenv(ADBLOCK_VAR, "") or "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    return True


def _host_matches(host: str, suffix: str) -> bool:
    """``host`` is ``suffix`` or a subdomain of it, on the host boundary."""
    return host == suffix or host.endswith("." + suffix)


def should_block(url: object) -> bool:
    """Should this URL be aborted by the route guard?

    Pure over the URL string, so it is testable without a browser and cheap
    enough to run on every request a page makes.
    """
    if not enabled():
        return False
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    host = (parts.hostname or "").casefold()
    if not host:
        return False
    # The media and content hosts win over every block, checked first so that
    # adding a broad suffix later cannot quietly take them out.
    for safe in NEVER_BLOCK_HOSTS:
        if _host_matches(host, safe):
            return False
    for suffix in HOST_SUFFIXES:
        if _host_matches(host, suffix):
            return True
    path = (parts.path or "").casefold()
    lowered = str(url or "").casefold()
    for pattern in PATH_PATTERNS:
        if pattern in path or pattern in lowered:
            return True
    return False


#: Best-effort cosmetic hiding. Injected as a stylesheet on every page, and
#: **never** allowed to fail a browser start: a page that cannot load is a
#: failure, a missing cosmetic rule is a slightly uglier page. Deliberately
#: conservative -- no scriptlets, no `:has()` chains, nothing that reads as
#: automation to a site's anti-adblock detector.
COSMETIC_CSS = """
/* Lumine: hide the usual ad slots without touching the page's own layout. */
ins.adsbygoogle,
iframe[src*="doubleclick.net"],
iframe[src*="googlesyndication.com"],
div[id^="google_ads"],
div[id^="div-gpt-ad"],
aside[data-ad],
[data-testid*="ad-container"],
.promoted-result,
.ytd-promoted-sparkles-web-renderer,
#masthead-ad,
#player-ads,
ytd-promoted-video-renderer,
ytd-display-ad-renderer,
html.google-auto-placed {
  display: none !important;
}
"""

#: The script that installs the stylesheet. Kept as one string so a failure to
#: inject is one `try` at the call site rather than a partial install.
COSMETIC_INIT_JS = """
(() => {
  const install = () => {
    if (document.getElementById('lumine-cosmetic')) return;
    const style = document.createElement('style');
    style.id = 'lumine-cosmetic';
    style.textContent = %s;
    (document.head || document.documentElement).appendChild(style);
  };
  install();
  // A single-page app replaces the head on navigation; re-install once per
  // document rather than per mutation, so the observer cannot spin.
  new MutationObserver(install).observe(document.documentElement, {
    childList: true, subtree: false
  });
})();
""" % repr(COSMETIC_CSS)
