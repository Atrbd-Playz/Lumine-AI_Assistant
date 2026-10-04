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

from agent.tools.browser.actions import observe_payload
from agent.tools.browser.session import _JS_OBSERVE

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


if __name__ == "__main__":
    unittest.main()