"""Which controls an `observe` offers, decided by the page and not by a guess.

Two things are tested here that are easy to get wrong and invisible until a user
is waiting:

* **Results must survive the chrome filter.** On a video results page the
  masthead and the guide come first in document order, so a report that spends
  its budget on document order offers eight navigation links and not one result.
  The filter is what fixes that, and it is a general rule -- a masthead or a
  navigation landmark is furniture on *every* site -- rather than a list of one
  site's custom element names.
* **A link must carry its own address.** A model shown labels with no `href`
  cannot open what it is looking at, so on a results page its only move is to
  invent an address, and an invented id loads as a real page whose title says the
  video does not exist.

The observe script is *executed*, against a stub DOM, rather than read. A filter
that reads correctly but walks ancestors wrongly is not a filter that works, and
the bug it produces is a results page with no results on it -- silent, and
identical to "the model misread the page".

The DOM is built here rather than fetched. The rule under test is "walk the
ancestors", and a live page would make the suite depend on a site that changes
its markup weekly.
"""

from __future__ import annotations

import json
import subprocess
import unittest

from agent.tools.browser import state
from agent.tools.browser.actions import observe_payload
from agent.tools.browser.session import _JS_OBSERVE, _Browser, _Tab

# The stub DOM. Deliberately minimal: exactly the surface `_JS_OBSERVE` touches,
# so if the script starts using something else this fails loudly rather than
# passing against a fake that happens to accommodate it.
_STUB = r"""
function makeNode(spec) {
  return {
    tagName: spec.tag,
    _attrs: Object.assign({}, spec.attrs || {}),
    parentElement: null,
    getAttribute(n) { return Object.prototype.hasOwnProperty.call(this._attrs, n) ? this._attrs[n] : null; },
    setAttribute(n, v) { this._attrs[n] = v; },
    removeAttribute(n) { delete this._attrs[n]; },
    getBoundingClientRect() { return { width: spec.w == null ? 120 : spec.w, height: 20 }; },
    get textContent() { return spec.text || ''; },
  };
}
const byId = {};
const all = [];
const body = makeNode({ tag: 'BODY' });
const html = makeNode({ tag: 'HTML' });
html.parentElement = null;
body.parentElement = html;
const nodes = [];
function build(spec, parent) {
  const node = makeNode(spec);
  node.parentElement = parent;
  nodes.push(node);
  byId[spec.id || ('n' + nodes.length)] = node;
  for (const child of spec.children || []) build(child, node);
  return node;
}
for (const root of TREE) build(root, body);
all.push(...nodes);
const document = {
  documentElement: html,
  body: body,
  title: PAGE_TITLE,
  querySelectorAll: (sel) => all.filter((n) => sel.split(',').some((part) => part.trim().startsWith(n.tagName.toLowerCase() + '[') || part.trim() === n.tagName.toLowerCase())),
};
const location = { href: PAGE_URL };
"""

_RUNNER = _STUB + "\nconst observe = " + _JS_OBSERVE + "\n" + r"""
const out = observe(GENERATION);
// The generation is written onto the document rather than returned, so the stub
// reports it back -- otherwise the stamp is unobservable and a test that claims
// to check it is checking nothing.
out.generation = Number(document.documentElement.getAttribute('data-lumine-gen'));
out.stamped = nodes.filter((n) => n.hasOwnProperty('_attrs') && n.getAttribute('data-lumine-ref')).map((n) => n.getAttribute('data-lumine-ref'));
process.stdout.write(JSON.stringify(out));
"""


def run_observe(
    tree: list[dict],
    generation: int = 7,
    url: str = "https://www.youtube.com/results?search_query=a+song",
    title: str = "a song - YouTube",
) -> dict:
    """Execute the real `_JS_OBSERVE` over a stub DOM and return its result."""
    script = (
        "const TREE = " + json.dumps(tree) + ";\n"
        "const GENERATION = " + str(generation) + ";\n"
        "const PAGE_URL = " + json.dumps(url) + ";\n"
        "const PAGE_TITLE = " + json.dumps(title) + ";\n" + _RUNNER
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise AssertionError(f"the observe script failed: {result.stderr.strip()[:400]}")
    return json.loads(result.stdout)


def link(href: str, text: str, **attrs) -> dict:
    return {"tag": "A", "attrs": {"href": href, **attrs}, "text": text}


# -- a stub with a layout -----------------------------------------------------
#
# The stub above has no `window`, no `createTreeWalker` and no `createRange`,
# which is exactly the environment `onScreenText` answers `''` for -- so those
# tests pin the *degrade* path (head text, as before) without saying so. This
# second stub has all three, and text nodes carry a box, which is what makes
# the rule under test observable at all: a read is the text whose **line boxes
# meet the viewport**, so the same page read at two scroll positions must
# produce two different texts. That is the whole fix for the reported failure
# -- six observes and four scrolls returning the same 127 words of header,
# because every read started at character zero of the document.
_LAYOUT_STUB = r"""
const TEXTS = [];
function under(t, root) {
  for (let n = t.parentElement; n; n = n.parentElement) if (n === root) return true;
  return false;
}
function makeNode(spec) {
  return {
    tagName: spec.tag,
    _attrs: Object.assign({}, spec.attrs || {}),
    parentElement: null,
    getAttribute(n) { return Object.prototype.hasOwnProperty.call(this._attrs, n) ? this._attrs[n] : null; },
    setAttribute(n, v) { this._attrs[n] = v; },
    removeAttribute(n) { delete this._attrs[n]; },
    getBoundingClientRect() { return { width: spec.w == null ? 120 : spec.w, height: 20, top: 0, bottom: 20 }; },
    get textContent() { return spec.text || ''; },
    // What a real DOM has and the first stub did not: an element's text is the
    // text of everything under it. The region ladder compares a region's text
    // against a floor before believing it, so a `main` whose textContent is ''
    // would be skipped as an empty wrapper and the test would silently be
    // reading the whole body instead of the region.
    get innerText() { return TEXTS.filter((t) => under(t, this)).map((t) => t.nodeValue).join(' '); },
  };
}
const all = [];
function build(spec, parent) {
  const node = makeNode(spec);
  node.parentElement = parent;
  all.push(node);
  if (spec.text) {
    TEXTS.push({
      nodeValue: spec.text,
      parentElement: node,
      _rects: (spec.lines || [[0, 20]]).map(([top, bottom]) => ({ top: top, bottom: bottom })),
    });
  }
  for (const child of spec.children || []) build(child, node);
  return node;
}
const body = makeNode({ tag: 'BODY' });
const html = makeNode({ tag: 'HTML' });
html.scrollHeight = PAGE_HEIGHT;
const nodes = [];
for (const root of TREE) nodes.push(build(root, body));
const document = {
  documentElement: html,
  scrollingElement: html,
  body: body,
  title: PAGE_TITLE,
  querySelectorAll: (sel) => [body, ...nodes].filter((n) => sel.split(',').some((part) => part.trim().startsWith(n.tagName.toLowerCase() + '[') || part.trim() === n.tagName.toLowerCase())),
  querySelector: (sel) => {
    const want = String(sel).toLowerCase();
    for (const n of [html, body, ...nodes]) {
      if (n.tagName.toLowerCase() === want) return n;
      if (want.startsWith('#') && n._attrs.id === want.slice(1)) return n;
    }
    return null;
  },
  createTreeWalker: (root) => {
    const mine = TEXTS.filter((t) => under(t, root));
    let i = 0;
    return { nextNode: () => (i < mine.length ? mine[i++] : null) };
  },
  createRange: () => ({
    _n: null,
    selectNode(n) { this._n = n; },
    // The text node's own line boxes. A `<div>` spanning the page would
    // otherwise qualify at every scroll position -- which is the bug in a new
    // hat, and the reason this is per text node rather than per element.
    getClientRects() { return (this._n && this._n._rects) || []; },
  }),
};
const location = { href: PAGE_URL };
const window = HAS_WINDOW ? {
  innerHeight: VIEWPORT_H,
  scrollY: SCROLL_Y,
  getComputedStyle: () => ({ visibility: 'visible', display: 'block' }),
} : undefined;
"""

_LAYOUT_RUNNER = _LAYOUT_STUB + "\nconst observe = " + _JS_OBSERVE + "\n" + r"""
const out = observe(GENERATION);
out.stamped = nodes.filter((n) => n.getAttribute('data-lumine-ref')).map((n) => n.getAttribute('data-lumine-ref'));
process.stdout.write(JSON.stringify(out));
"""

_REGION_RUNNER = _LAYOUT_STUB + "\nconst readRegion = " + state._JS_REGION_TEMPLATE + "\n" + r"""
// One array argument, destructured by the arrow -- the same shape Playwright
// receives from `page.evaluate(script, [selectors, fields, budget])`. Calling
// it with three arguments leaves every parameter undefined and the script
// legitimately answers with the empty region.
process.stdout.write(JSON.stringify(readRegion([REGION_SELECTORS, {}, 2400])));
"""


def run_observe_laid_out(
    tree: list[dict],
    *,
    scroll_y: int = 0,
    viewport_h: int = 800,
    page_height: int = 5000,
    has_window: bool = True,
    generation: int = 7,
    url: str = "https://www.ryans.com/pc-power-km02w",
    title: str = "KM-02W — Ryan's",
) -> dict:
    """The real `_JS_OBSERVE` over a stub that has geometry, at a scroll position."""
    script = (
        "const TREE = " + json.dumps(tree) + ";\n"
        "const GENERATION = " + str(generation) + ";\n"
        "const PAGE_URL = " + json.dumps(url) + ";\n"
        "const PAGE_TITLE = " + json.dumps(title) + ";\n"
        "const PAGE_HEIGHT = " + str(page_height) + ";\n"
        "const VIEWPORT_H = " + str(viewport_h) + ";\n"
        "const SCROLL_Y = " + str(scroll_y) + ";\n"
        "const HAS_WINDOW = " + ("true" if has_window else "false") + ";\n" + _LAYOUT_RUNNER
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise AssertionError(f"the observe script failed: {result.stderr.strip()[:400]}")
    return json.loads(result.stdout)


def run_region_laid_out(
    tree: list[dict],
    selectors: list[str],
    *,
    scroll_y: int = 0,
    viewport_h: int = 800,
    page_height: int = 5000,
) -> dict:
    """The real `_JS_REGION_TEMPLATE` over the same laid-out stub."""
    script = (
        "const TREE = " + json.dumps(tree) + ";\n"
        "const GENERATION = 7;\n"
        "const PAGE_URL = 'https://www.ryans.com/pc-power-km02w';\n"
        "const PAGE_TITLE = 'KM-02W';\n"
        "const PAGE_HEIGHT = " + str(page_height) + ";\n"
        "const VIEWPORT_H = " + str(viewport_h) + ";\n"
        "const SCROLL_Y = " + str(scroll_y) + ";\n"
        "const HAS_WINDOW = true;\n"
        "const REGION_SELECTORS = " + json.dumps(selectors) + ";\n" + _REGION_RUNNER
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise AssertionError(f"the region script failed: {result.stderr.strip()[:400]}")
    return json.loads(result.stdout)


#: The page from the incident, with the geometry that made it fail: a header at
#: the very top, the product's name below the fold, and the price further down
#: still. At a viewport of 800 the window covers `scrollY - 400` to
#: `scrollY + 1200`, so no single read can see the header and the price
#: together -- which is the point. A read that always returns the header is a
#: read that can never answer what the thing costs.
PRODUCT_PAGE = [
    {
        "tag": "HEADER",
        "children": [
            {
                "tag": "A",
                "attrs": {"href": "https://www.ryans.com/"},
                "text": "Ryan's — Computer & Technology",
                "lines": [[5, 25]],
            },
        ],
    },
    {
        "tag": "MAIN",
        "children": [
            {
                "tag": "H1",
                "text": "KM-02W Wired Gaming Keyboard and Mouse Combo with RGB Backlight",
                "lines": [[900, 950]],
            },
            {
                "tag": "DIV",
                "text": "Price: 1,450 BDT and it ships tomorrow from our Dhaka warehouse",
                "lines": [[1500, 1540]],
            },
        ],
    },
    {"tag": "FOOTER", "text": "Terms of Use and Privacy Policy", "lines": [[4800, 4830]]},
]


class ViewportWindowTests(unittest.TestCase):
    """A read answers with what is *in view*, not with the head of the document."""

    def test_a_read_at_the_top_does_not_see_the_price(self):
        out = run_observe_laid_out(PRODUCT_PAGE, scroll_y=0)
        self.assertIn("KM-02W", out["text"])
        self.assertNotIn("1,450", out["text"])

    def test_a_read_further_down_sees_the_price_and_not_the_header(self):
        # The direction that matters, and the one the reported failure was
        # missing: scrolling has to *change what comes back*. 1400 puts the
        # window at 1000-2600, which holds the price and misses both the
        # header (5-25) and the product name (900-950).
        out = run_observe_laid_out(PRODUCT_PAGE, scroll_y=1400)
        self.assertIn("1,450", out["text"])
        self.assertNotIn("Ryan's", out["text"])
        self.assertNotIn("KM-02W", out["text"])

    def test_a_read_at_the_bottom_sees_only_the_footer(self):
        out = run_observe_laid_out(PRODUCT_PAGE, scroll_y=4400)
        self.assertIn("Privacy Policy", out["text"])
        self.assertNotIn("1,450", out["text"])

    def test_the_scroll_position_is_reported_with_the_text_it_was_taken_at(self):
        # Without this number the windowed text is uninterpretable: the model
        # cannot tell "I read further down" from "I read the same 400
        # characters again", and `scroll down and read again` is a sentence it
        # has no way to confirm it followed.
        out = run_observe_laid_out(PRODUCT_PAGE, scroll_y=1400)
        self.assertEqual(out["scroll"]["y"], 1400)
        self.assertEqual(out["scroll"]["max"], 5000 - 800)

    def test_a_read_with_a_shorter_viewport_sees_less(self):
        # The window is a fraction of the viewport, not a fixed slice, so a
        # small window reads a smaller piece of page: at height 400 the window
        # is -200 to 600, which holds the header and misses both the product
        # name (900) and the price (1500).
        out = run_observe_laid_out(PRODUCT_PAGE, scroll_y=0, viewport_h=400)
        self.assertIn("Ryan's", out["text"])
        self.assertNotIn("KM-02W", out["text"])
        self.assertNotIn("1,450", out["text"])

    def test_without_a_window_the_head_is_still_a_read(self):
        # The degrade path, pinned rather than assumed. No `window` means the
        # helper cannot answer, and an empty answer here would be a page that
        # reports no text at all -- so the head is kept, exactly as it was
        # before this existed.
        out = run_observe_laid_out(PRODUCT_PAGE, has_window=False)
        self.assertIn("KM-02W", out["text"])
        # Present but null: the page could not answer where the read was taken
        # from, which is a different statement from "at the top" -- and the
        # Python side drops a null rather than reporting a position it does
        # not have.
        self.assertIsNone(out.get("scroll"))

    def test_the_chrome_filter_still_applies_to_a_laid_out_page(self):
        # The windowing is a second rule on top of the first, not a
        # replacement: a header inside the window is still chrome.
        out = run_observe_laid_out(PRODUCT_PAGE, scroll_y=0)
        hrefs = [c["href"] for c in out["controls"]]
        self.assertNotIn("https://www.ryans.com/", hrefs)


class RegionWindowTests(unittest.TestCase):
    """The *region* read is windowed too, and it is the one carrying the price.

    `read_region` is what both tools merge over the body text, and it used to
    return the region from its first character -- so on a tall product page it
    answered with the same header the body read had. The windowing has to be
    here as well as in the body read, or the merge would quietly replace a
    windowed read with an unwindowed one.
    """

    def test_a_region_read_at_the_top_misses_the_price(self):
        out = run_region_laid_out(PRODUCT_PAGE, ["main"], scroll_y=0)
        self.assertEqual(out["region"], "main")
        self.assertIn("KM-02W", out["text"])
        self.assertNotIn("1,450", out["text"])

    def test_a_region_read_further_down_finds_the_price(self):
        # The reported failure, at the level it actually happened: the price
        # was in the region all along, and the read never scrolled to it.
        out = run_region_laid_out(PRODUCT_PAGE, ["main"], scroll_y=1400)
        self.assertEqual(out["region"], "main")
        self.assertIn("1,450", out["text"])
        self.assertNotIn("KM-02W", out["text"])

    def test_the_region_is_still_the_one_the_ladder_chose(self):
        # Windowing decides *which text* comes back, never *which region* it
        # came from -- a caller comparing two reads still needs to know they
        # were of the same region.
        out = run_region_laid_out(PRODUCT_PAGE, ["#nope", "main", "body"], scroll_y=1400)
        self.assertEqual(out["region"], "main")

    def test_a_region_whose_text_is_out_of_view_falls_back_to_its_whole_text(self):
        # The fallback path, exercised rather than assumed: a region entirely
        # outside the window answers with `raw` -- the region's own text -- so
        # the read is still this region and not an error or an empty string.
        # A single line box far below the fold is the shape that reaches it.
        page = [{
            "tag": "MAIN",
            "text": "Price: 1,450 BDT and it ships tomorrow from our Dhaka warehouse within two working days.",
            "lines": [[5000, 5020]],
        }]
        out = run_region_laid_out(page, ["main"], scroll_y=0)
        self.assertEqual(out["region"], "main")
        self.assertIn("1,450", out["text"])


#: A results page shaped the way one is actually built: masthead first, then the
#: guide, then the results, then an injected banner, then a footer. Document order
#: is exactly what the filter has to overcome.
RESULTS_PAGE = [
    {
        "tag": "HEADER",
        "children": [
            link("https://www.youtube.com/", "Home"),
            link("https://www.youtube.com/feed/subscriptions", "Subscriptions"),
            link("https://www.youtube.com/feed/explore", "Explore"),
        ],
    },
    {
        "tag": "DIV",
        "attrs": {"role": "navigation"},
        "children": [
            link("https://www.youtube.com/feed/trending", "Trending"),
            link("https://www.youtube.com/feed/history", "History"),
        ],
    },
    {
        "tag": "MAIN",
        "children": [
            {
                "tag": "DIV",
                "children": [
                    link("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "A Song"),
                    link("https://www.youtube.com/watch?v=Xh1tKj0y_mQ", "Some Other Song"),
                ],
            },
            {
                "tag": "DIV",
                "attrs": {"role": "banner"},
                "children": [link("https://www.youtube.com/banner-ad", "Sponsored")],
            },
        ],
    },
    {"tag": "FOOTER", "children": [link("https://www.youtube.com/terms", "Terms")]},
]

CHROME = (
    "https://www.youtube.com/",
    "https://www.youtube.com/feed/subscriptions",
    "https://www.youtube.com/feed/explore",
    "https://www.youtube.com/feed/trending",
    "https://www.youtube.com/feed/history",
    "https://www.youtube.com/banner-ad",
    "https://www.youtube.com/terms",
)
RESULTS = (
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=Xh1tKj0y_mQ",
)


class ChromeFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hrefs = [c["href"] for c in run_observe(RESULTS_PAGE)["controls"]]

    def test_every_kind_of_chrome_is_skipped(self):
        # A header element, a role="navigation" drawer, a role="banner" slot and a
        # footer -- the four ways a site spells "this is my own furniture". Missing
        # any one of them puts navigation back into a results report.
        for href in CHROME:
            self.assertNotIn(href, self.hrefs, f"chrome survived the filter: {href}")

    def test_the_actual_results_survive(self):
        # The direction that matters. A filter that drops everything is also a
        # filter that drops the thing the user asked for, and it fails the same
        # silent way.
        for href in RESULTS:
            self.assertIn(href, self.hrefs, f"a result was filtered out: {href}")

    def test_the_report_is_spent_on_content_not_navigation(self):
        self.assertEqual(len(self.hrefs), len(RESULTS))

    def test_content_outside_a_landmark_is_not_mistaken_for_chrome(self):
        # The over-filtering direction. A result nested under an ordinary div
        # must not be dropped just because it has ancestors.
        page = [
            {"tag": "SECTION", "attrs": {"role": "region"}, "children": [
                {"tag": "DIV", "children": [
                    link("https://www.youtube.com/watch?v=aaaaaaaaaaa", "Kept"),
                ]},
            ]},
        ]
        hrefs = [c["href"] for c in run_observe(page)["controls"]]
        self.assertEqual(hrefs, ["https://www.youtube.com/watch?v=aaaaaaaaaaa"])

    def test_a_page_footer_is_skipped_and_an_articles_footer_is_kept(self):
        # The subtlety the filter is built on. "Is this link in a footer" answers
        # both of these the same way, and only one of them is chrome: a page-level
        # footer is Terms and Privacy, an article footer is the byline and the
        # tags. A rule that cannot tell them apart throws away the content half of
        # the time, which is worse than the navigation it was fixing.
        page = [
            {"tag": "FOOTER", "children": [link("https://site/terms", "Terms")]},
            {"tag": "ARTICLE", "children": [
                {"tag": "FOOTER", "children": [
                    link("https://site/story/byline", "By A Writer"),
                ]},
            ]},
        ]
        hrefs = [c["href"] for c in run_observe(page)["controls"]]
        self.assertNotIn("https://site/terms", hrefs)
        self.assertIn("https://site/story/byline", hrefs)


class RefTests(unittest.TestCase):
    def test_references_are_minted_for_survivors_only(self):
        # Contiguous and starting at r1. Gaps would mean a control was collected
        # and then hidden, which makes a model's "act on r4" land somewhere it
        # cannot see.
        refs = [c["ref"] for c in run_observe(RESULTS_PAGE)["controls"]]
        self.assertEqual(refs, ["r1", "r2"])

    def test_the_document_is_stamped_with_the_generation(self):
        # Every later action checks this, so a reference cannot survive the page
        # it was read from. If the stamp ever stops being written, every stale-ref
        # refusal silently becomes a no-op.
        result = run_observe(RESULTS_PAGE, generation=42)
        self.assertEqual(result["generation"], 42)

    def test_a_button_gets_no_href(self):
        page = [{"tag": "MAIN", "children": [
            {"tag": "BUTTON", "text": "Subscribe"},
        ]}]
        controls = run_observe(page)["controls"]
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0]["href"], "")


class PayloadTests(unittest.TestCase):
    def test_a_link_reaches_the_model_with_its_address(self):
        payload = json.loads(
            observe_payload(
                title="Results",
                url="https://www.youtube.com/results?search_query=song",
                text="",
                controls=[
                    {"ref": "r1", "tag": "a", "href": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "label": "A Song"},
                    {"ref": "r2", "tag": "button", "href": "", "label": "Subscribe"},
                ],
            )
        )
        first, second = payload["controls"]
        self.assertEqual(first["kind"], "a")
        self.assertEqual(first["href"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        # A button has no address, so it is not handed an empty one. The absence is
        # the information: an empty href reads as "go nowhere", and a model that
        # believes a button has a destination will try to open one.
        self.assertNotIn("href", second)

    def test_a_long_address_is_clipped_rather_than_dropped(self):
        # Clipped, not truncated to nothing: the video id lives in the query
        # string, so a cut that kept only the host and path would lose the one
        # part that identifies the video.
        href = (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVMabc&index=17"
            "&t=42s&pp=abcdefghijklmnopqrstuvwxyz"
        )
        payload = json.loads(
            observe_payload(
                title="Results", url="https://www.youtube.com/", text="",
                controls=[{"ref": "r1", "tag": "a", "href": href, "label": "A Song"}],
            )
        )
        got = payload["controls"][0]["href"]
        self.assertIn("watch?v=dQw4w9WgXcQ", got)
        self.assertLessEqual(len(got), 100)


class RepeatReadTests(unittest.TestCase):
    """Saying so when a read returns the text this tab already returned.

    The incident this pins: six `observe` calls and four scrolls on one product
    page, every read coming back with the same 127 words of header, with
    nothing in any of them telling the model it was re-reading the same
    window. The scroll that does not move looks like success from the outside,
    so the model kept asking and kept getting the same text.
    """

    def _browser(self, key: int = 1):
        """A `_Browser` with one tab, and the tab's digest slot reachable."""
        tab = _Tab(key, page=None, generation=1)
        owner = object.__new__(_Browser)
        owner._tabs = [tab]
        owner._active = key
        return owner, tab

    @staticmethod
    def _read(url: str = "https://shop.example/item", y: int = 0, text: str = "Price: 1,450") -> dict:
        return {"url": url, "text": text, "scroll": {"y": y, "max": 4000}}

    def test_the_first_read_says_nothing(self):
        owner, _tab = self._browser()
        result = self._read()
        owner._flag_repeat_read(result)
        self.assertNotIn("notice", result)

    def test_the_same_text_at_the_same_position_is_flagged(self):
        owner, _tab = self._browser()
        first = self._read()
        owner._flag_repeat_read(first)
        second = self._read()
        owner._flag_repeat_read(second)
        self.assertIn("notice", second)
        self.assertIn("same text", second["notice"])

    def test_the_same_text_after_a_scroll_that_moved_is_not_flagged(self):
        # A scroll that worked produces a different window, and a different
        # window is a different read even on the same page -- flagging it
        # would teach the model that scrolling does nothing.
        owner, _tab = self._browser()
        owner._flag_repeat_read(self._read(y=0))
        result = self._read(y=1400)
        owner._flag_repeat_read(result)
        self.assertNotIn("notice", result)

    def test_each_tab_keeps_its_own_digest(self):
        # Two tabs, two histories. A digest kept per browser would make a
        # read of tab 2 look like a repeat of tab 1 the moment both showed
        # the same page -- which is exactly the pair of tabs "compare two
        # pages" exists to create.
        owner, _tab = self._browser(key=1)
        other = _Tab(2, page=None, generation=1)
        owner._tabs.append(other)
        owner._flag_repeat_read(self._read())
        owner._active = 2
        result = self._read()
        owner._flag_repeat_read(result)
        self.assertNotIn("notice", result)

    def test_the_notice_names_the_numbers_to_check(self):
        # A warning that says "you already read this" without saying *how to
        # move on* costs the model a turn it will spend re-reading. The scroll
        # position is the thing to look at, so the sentence names it.
        owner, _tab = self._browser()
        owner._flag_repeat_read(self._read())
        result = self._read()
        owner._flag_repeat_read(result)
        self.assertIn("scroll", result["notice"])


if __name__ == "__main__":
    unittest.main()