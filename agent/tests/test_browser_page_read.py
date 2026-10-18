"""Reading the *content* of a page: the region, its fields, and how they merge.

The reported failure this all exists for is one sentence: *she read the page but
could not tell me what it costs.* A `body.innerText` read of a product page is a
cookie banner, a navigation drawer and a recommendation rail, and the price is
somewhere after all of that -- so the fix was never a bigger budget, because a
bigger budget reads *more navigation*. It is knowing where the site keeps its
content (`sites_map.yaml`), reading that region, and putting its facts under
stable names.

Three things are pinned here, and each is a way the read could have been wrong
rather than merely short:

* **The entry leads the ladder, and supplies both halves.** The same entry decides
  which selectors to try *and* which fields to extract -- two lookups would be two
  answers if the file changed between them, and the field selectors are relative
  to the region, so they only mean anything together.

* **A region that is nearly empty is not believed.** `REGION_MIN_CHARS` is what
  stops an empty `<main>` around a canvas turning a page with plenty to say into
  a blank read.

* **The merge loses nothing the caller already had.** Controls were collected from
  the full document; a region read must not be able to take them away.

Nothing here opens a browser: `read_region` is handed a page, so every test hands
it a fake.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from agent.tools.browser import site_map, state
from agent.tools.tool_results import MAX_PAGE_READ_CHARS


class FakePage:
    """A page that records what it was asked and answers with a fixed payload."""

    def __init__(self, payload=None, *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.seen = None
        self.script = None

    async def evaluate(self, script, argument):
        self.script = script
        self.seen = argument
        if self.error is not None:
            raise self.error
        return self.payload


def _entry(host="amazon.in", content="#centerCol", fields=None):
    return site_map.SiteEntry(
        host=host, content=content, fields={"price": ".price"} if fields is None else fields
    )


class ReadRegionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_site_map_entry_leads_the_ladder_and_supplies_the_fields(self):
        page = FakePage({"text": "a price", "fields": {"price": "$49"}, "region": "#centerCol"})
        with mock.patch.object(site_map, "load", return_value={"amazon.in": _entry()}):
            out = await state.read_region(page, url="https://www.amazon.in/dp/B0")

        selectors, fields, budget = page.seen
        # The entry first, then the generic ladder behind it minus the entry's own
        # selector: one entry covers a site's very different page shapes, and a
        # `main` that follows is the fallback, not a second opinion about the same
        # region.
        self.assertEqual(selectors[0], "#centerCol")
        self.assertEqual(selectors[1:], [s for s in state.REGION_LADDER if s != "#centerCol"])
        self.assertEqual(fields, {"price": ".price"})
        self.assertEqual(budget, MAX_PAGE_READ_CHARS)
        self.assertEqual(out["text"], "a price")
        self.assertEqual(out["fields"], {"price": "$49"})
        self.assertEqual(out["region"], "#centerCol")

    async def test_a_host_with_no_entry_reads_through_the_ladder(self):
        page = FakePage({"text": "x" * 100, "fields": {}, "region": "main"})
        with mock.patch.object(site_map, "load", return_value={}):
            await state.read_region(page, url="https://somewhere.example/")
        selectors, fields, budget = page.seen
        self.assertEqual(selectors, list(state.REGION_LADDER))
        self.assertEqual(fields, {})
        self.assertEqual(budget, MAX_PAGE_READ_CHARS)

    async def test_an_entry_that_only_names_fields_leads_with_the_ladder(self):
        # A map may know where a site keeps its price without knowing better than
        # `main` where its prose is. Both halves still come from the same entry.
        page = FakePage({"text": "x" * 100, "fields": {}, "region": "main"})
        with mock.patch.object(
            site_map, "load", return_value={"shop.example": _entry(content="", fields={"p": "#p"})}
        ):
            await state.read_region(page, url="https://shop.example/item")
        selectors, fields, _budget = page.seen
        self.assertEqual(selectors, list(state.REGION_LADDER))
        self.assertEqual(fields, {"p": "#p"})

    async def test_a_page_that_cannot_be_read_is_an_empty_answer_not_a_failure(self):
        page = FakePage(error=RuntimeError("the page went away"))
        with self.assertLogs("lumine", level="WARNING"):
            out = await state.read_region(page, url="https://example.com/")
        self.assertEqual(out, {"text": "", "fields": {}, "region": ""})

    async def test_a_payload_that_is_not_an_object_is_an_empty_answer(self):
        out = await state.read_region(FakePage("nonsense"), url="https://example.com/")
        self.assertEqual(out, {"text": "", "fields": {}, "region": ""})

    async def test_the_budget_is_the_browsers_own_and_not_the_global_ceiling(self):
        # The number that decides whether a price survives the read. 800 is the
        # shared `MAX_RESULT_CHARS`, and a read that stops there ends mid-sentence
        # on the very thing the read was for.
        self.assertEqual(state.MAX_TEXT, MAX_PAGE_READ_CHARS)
        self.assertGreater(state.MAX_TEXT, 800)
        page = FakePage({"text": "x", "fields": {}, "region": ""})
        await state.read_region(page, url="https://example.com/", budget=123)
        self.assertEqual(page.seen[2], 123)


class BoundedFieldsTests(unittest.TestCase):
    def test_a_field_is_one_fact_under_a_stable_name(self):
        out = state._bounded_fields({"price": "  $49.00   ", "title": "A thing"})
        self.assertEqual(out, {"price": "$49.00", "title": "A thing"})

    def test_a_long_value_is_clipped_and_a_blank_one_is_dropped(self):
        out = state._bounded_fields({"price": "x" * 500, "title": "   "})
        self.assertEqual(len(out["price"]), state.MAX_FIELD_VALUE_CHARS)
        self.assertNotIn("title", out)

    def test_the_field_count_is_capped(self):
        out = state._bounded_fields({f"f{i}": str(i) for i in range(state.MAX_REGION_FIELDS + 5)})
        self.assertEqual(len(out), state.MAX_REGION_FIELDS)

    def test_anything_that_is_not_a_mapping_is_no_fields(self):
        self.assertEqual(state._bounded_fields(None), {})
        self.assertEqual(state._bounded_fields(["price"]), {})


class ApplyRegionTests(unittest.TestCase):
    def _payload(self, **over):
        base = {"title": "T", "url": "https://example.com/", "text": "body words", "controls": []}
        base.update(over)
        return base

    def test_a_region_with_something_to_say_replaces_the_body_text(self):
        text = "The price is $49.00 and it ships tomorrow. " * 4
        out = state.apply_region(self._payload(), {"text": text, "fields": {"price": "$49"}, "region": "#centerCol"})
        self.assertEqual(out["text"], text)
        self.assertEqual(out["region"], "#centerCol")
        self.assertEqual(out["fields"], {"price": "$49"})

    def test_a_nearly_empty_region_is_not_believed(self):
        # An empty `<main>` around a canvas, or a landmark a framework renders
        # before its children mount. Believing it would return a blank read for a
        # page that has plenty to say -- so the body text stays exactly as it was.
        original = self._payload()
        out = state.apply_region(original, {"text": "   ", "fields": {}, "region": "main"})
        self.assertEqual(out["text"], "body words")
        self.assertNotIn("region", out)

    def test_the_region_text_is_still_bounded(self):
        out = state.apply_region(self._payload(), {"text": "x" * (state.MAX_TEXT * 2), "fields": {}, "region": "main"})
        self.assertEqual(len(out["text"]), state.MAX_TEXT)

    def test_fields_merge_rather_than_replace(self):
        payload = self._payload(fields={"title": "from the document"})
        out = state.apply_region(payload, {"text": "y" * 200, "fields": {"price": "$49"}, "region": ""})
        self.assertEqual(out["fields"], {"title": "from the document", "price": "$49"})

    def test_an_empty_field_set_leaves_what_was_there(self):
        payload = self._payload(fields={"title": "keep me"})
        out = state.apply_region(payload, {"text": "y" * 200, "fields": {}, "region": ""})
        self.assertEqual(out["fields"], {"title": "keep me"})

    def test_a_payload_that_is_not_an_object_is_returned_untouched(self):
        self.assertIsNone(state.apply_region(None, {"text": "x" * 200}))
        self.assertEqual(state.apply_region("text", None), "text")


class SnapshotFieldTests(unittest.TestCase):
    def test_a_snapshot_carries_its_named_fields(self):
        snap = state.Snapshot(generation=3, url="https://x/", fields={"price": "$49"})
        self.assertEqual(snap.to_dict()["fields"], {"price": "$49"})

    def test_a_page_no_entry_claims_reports_no_fields_key(self):
        # Empty is the honest state -- there is no schema to guess at -- and a key
        # that is always present would invite the model to read `fields: {}` as
        # "this page has no price" rather than "nobody told us where to look".
        snap = state.Snapshot(generation=3, url="https://x/")
        self.assertNotIn("fields", snap.to_dict())

    def test_the_field_map_is_a_copy_not_a_live_reference(self):
        fields = {"price": "$49"}
        snap = state.Snapshot(generation=1, fields=fields)
        out = snap.to_dict()
        out["fields"]["price"] = "$1"
        self.assertEqual(snap.fields["price"], "$49")


class LadderTests(unittest.TestCase):
    def test_the_ladder_ends_at_body_and_reports_that_case_as_no_region(self):
        # Reading all of it is better than refusing -- but `region` is empty for
        # it, so a caller can tell "the whole page" from "the part that mattered".
        self.assertEqual(state.REGION_LADDER[-1], "body")

    def test_the_ladder_is_most_specific_first(self):
        self.assertLess(state.REGION_LADDER.index("main"), state.REGION_LADDER.index("body"))
        self.assertIn('[role="main"]', state.REGION_LADDER)

    def test_the_minimum_for_believing_a_region_is_two_short_sentences(self):
        self.assertGreaterEqual(state.REGION_MIN_CHARS, 40)
        self.assertLess(state.REGION_MIN_CHARS, 400)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
