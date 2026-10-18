"""The per-site content map: what it accepts, what it refuses, and how it matches.

`sites_map.yaml` is *configuration handed to a browser*, so the two properties
worth pinning here are the ones a hand-edited file can break without any code
changing:

* **A selector we would not run is dropped, not raised.** A typo in a file
  somebody edits while the worker runs must cost one site its refinement and fall
  back to the generic ladder -- it must not take the tool down, and it must not be
  silent either, because a silently dropped `content` selector is a page that
  quietly stopped being readable in the way it used to be.

* **Matching is on host *labels*, not on strings.** `amazon.in` has to cover
  `www.amazon.in` and `smile.amazon.in`, and `notamazon.in` has to cover nothing
  at all. A plain `endswith` gets the second one wrong, and the cost of getting it
  wrong is one site reading its page through another site's selectors.

Everything here runs against files in a temp directory and pure calls. No test
opens a page: the map decides *what to ask for*, and asking is `state`'s job.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.tools.browser import site_map


class ParseTests(unittest.TestCase):
    """`parse` is pure, so every rule about the file's shape is testable here."""

    def test_a_host_and_its_fields_become_one_entry(self):
        entries = site_map.parse(
            {
                "amazon.in": {
                    "content": "#centerCol",
                    "fields": {"price": ".a-price", "title": "#productTitle"},
                }
            }
        )
        entry = entries["amazon.in"]
        self.assertEqual(entry.content, "#centerCol")
        self.assertEqual(dict(entry.fields), {"price": ".a-price", "title": "#productTitle"})

    def test_a_url_key_and_a_hostile_spelling_still_name_one_host(self):
        # Both spellings show up in a hand-written file. Refusing the shorter one
        # is a papercut with no upside, and an upper-case host is the same host.
        entries = site_map.parse(
            {"https://WWW.Example.com/path": {"content": "main", "fields": {}}}
        )
        self.assertEqual(sorted(entries), ["example.com"])

    def test_the_www_label_is_dropped_because_the_file_copies_the_address_bar(self):
        # Keeping it meant a map written the way its author *saw* the site
        # silently matched nothing: the read fell back to the generic ladder,
        # which looks exactly like a map that never mentioned the host.
        entries = site_map.parse({"www.example.com": {"content": "main"}})
        self.assertEqual(sorted(entries), ["example.com"])

    def test_only_www_is_dropped_and_only_as_a_whole_label(self):
        # `m.youtube.com` is a genuinely different surface, and `www2.example.com`
        # is a different host that merely starts with the same letters.
        entries = site_map.parse(
            {
                "m.youtube.com": {"content": "#mobile"},
                "www2.example.com": {"content": "#www2"},
            }
        )
        self.assertEqual(sorted(entries), ["m.youtube.com", "www2.example.com"])

    def test_a_host_that_is_nothing_but_the_www_label_survives_the_slice(self):
        # The trailing dot is stripped before the label check, so the slice never
        # runs past the end of the string and the degenerate key is left as the
        # host it spelled out. It matches nothing (`match` is label-wise), which
        # is the correct outcome for a key nobody should have written.
        entries = site_map.parse({"www.": {"content": "main"}})
        self.assertEqual(sorted(entries), ["www"])

    def test_an_entry_with_nothing_usable_is_left_out_rather_than_reported(self):
        # `is_usable` is the gate: an entry that names neither a region nor a
        # field would be a no-op, and a no-op entry is indistinguishable from a
        # map that never mentioned the host at all.
        entries = site_map.parse({"example.com": {}, "": {"content": "main"}})
        self.assertEqual(entries, {})

    def test_an_unusable_content_selector_is_dropped_logged_and_not_raised(self):
        # `javascript:` inside an attribute selector is exactly what the CSS
        # allowlist's second check exists to refuse -- this is a language handed
        # to the page engine, and the file does not get to bypass it.
        with self.assertLogs("lumine", level="WARNING") as caught:
            entries = site_map.parse(
                {
                    "shop.example": {
                        "content": "[data-url='javascript:alert(1)']",
                        "fields": {"price": "#price"},
                    }
                }
            )
        self.assertTrue(any("unusable selector" in line for line in caught.output))
        # The *entry* survives on its fields, and only the bad half is gone: the
        # site still gets its named facts, and the region falls back to the ladder.
        entry = entries["shop.example"]
        self.assertEqual(entry.content, "")
        self.assertEqual(dict(entry.fields), {"price": "#price"})

    def test_an_unusable_field_selector_drops_that_field_and_keeps_the_entry(self):
        with self.assertLogs("lumine", level="WARNING"):
            entries = site_map.parse(
                {"shop.example": {"content": "main", "fields": {"price": "#price", "x": "&&&"}}}
            )
        self.assertEqual(dict(entries["shop.example"].fields), {"price": "#price"})

    def test_the_field_count_is_capped(self):
        # A hand-maintained file is guarded against rather than trusted. The cap
        # is a ceiling on a mistake, not a target nobody may reach.
        fields = {f"f{i}": f"#f{i}" for i in range(site_map.MAX_FIELDS_PER_ENTRY + 10)}
        entry = site_map.parse({"example.com": {"content": "main", "fields": fields}})[
            "example.com"
        ]
        self.assertEqual(len(entry.fields), site_map.MAX_FIELDS_PER_ENTRY)

    def test_the_entry_count_is_capped(self):
        document = {f"site{i}.example": {"content": "main"} for i in range(site_map.MAX_ENTRIES + 5)}
        self.assertEqual(len(site_map.parse(document)), site_map.MAX_ENTRIES)

    def test_a_document_that_is_not_a_mapping_is_no_entries(self):
        self.assertEqual(site_map.parse(None), {})
        self.assertEqual(site_map.parse(["amazon.in"]), {})
        self.assertEqual(site_map.parse("amazon.in"), {})

    def test_the_shipped_map_parses_with_every_selector_intact(self):
        # The real file, read the way the tool reads it. A selector dropped here
        # would be a site whose refinement is silently already off, which is the
        # failure a unit test on a fixture could never see.
        document = site_map._read_yaml(site_map.DEFAULT_MAP_PATH)
        self.assertIsNotNone(document, "the shipped sites_map.yaml did not parse")
        entries = site_map.parse(document)
        for host, fields in (
            ("amazon.in", ("title", "price")),
            ("youtube.com", ("title", "channel")),
            ("wikipedia.org", ("title",)),
            ("google.com", ("stats",)),
        ):
            with self.subTest(host=host):
                self.assertIn(host, entries)
                self.assertTrue(entries[host].content, f"{host} lost its content selector")
                for name in fields:
                    self.assertIn(name, entries[host].fields)


class MatchTests(unittest.TestCase):
    """Label-wise suffix matching, and the lookalike it exists to refuse."""

    ENTRIES = site_map.parse(
        {
            "amazon.in": {"content": "#centerCol", "fields": {"price": ".price"}},
            "example.com": {"content": "main"},
        }
    )

    def match(self, url):
        with mock.patch.object(site_map, "load", return_value=self.ENTRIES):
            return site_map.match(url)

    def test_a_subdomain_of_a_known_host_matches(self):
        self.assertEqual(self.match("https://www.amazon.in/dp/B0").host, "amazon.in")
        self.assertEqual(self.match("https://smile.amazon.in/").host, "amazon.in")

    def test_the_host_itself_matches(self):
        self.assertEqual(self.match("https://amazon.in/").host, "amazon.in")

    def test_an_entry_written_with_www_still_reaches_the_bare_host(self):
        # The other direction of the same mistake: the file is copied out of an
        # address bar, so the entry carries `www.` and the page does not.
        entries = site_map.parse({"www.shop.example": {"content": "#shop"}})
        with mock.patch.object(site_map, "load", return_value=entries):
            self.assertEqual(site_map.match("https://shop.example/").content, "#shop")

    def test_a_lookalike_host_matches_nothing(self):
        # The whole reason this is label-wise rather than `endswith`. A string
        # suffix would hand one site another site's selectors, and the page would
        # be read through a region that does not exist on it -- an empty read that
        # looks like the site having nothing to say.
        self.assertIsNone(self.match("https://notamazon.in/dp/B0"))
        self.assertIsNone(self.match("https://evil-amazon.in/"))

    def test_an_unknown_host_and_a_url_with_no_host_are_none(self):
        self.assertIsNone(self.match("https://somewhere.example/"))
        self.assertIsNone(self.match("about:blank"))
        self.assertIsNone(self.match(""))
        self.assertIsNone(self.match(None))

    def test_the_longest_known_label_wins(self):
        # Two entries can match one host (`shop.example.com` and `example.com`);
        # the more specific one is the one the author meant.
        entries = self.ENTRIES | site_map.parse(
            {"shop.example.com": {"content": "#shop"}}
        )
        with mock.patch.object(site_map, "load", return_value=entries):
            self.assertEqual(site_map.match("https://shop.example.com/").content, "#shop")
            self.assertEqual(site_map.match("https://www.example.com/").content, "main")


class MapPathTests(unittest.TestCase):
    def test_the_environment_can_point_at_another_map(self):
        with mock.patch.dict(os.environ, {site_map.ENV_MAP_PATH: "/tmp/other.yaml"}):
            self.assertEqual(site_map.map_path(), "/tmp/other.yaml")

    def test_a_blank_override_does_not_disable_the_shipped_map(self):
        # A variable left over from an experiment must not be able to switch the
        # shipped map off by being empty.
        with mock.patch.dict(os.environ, {site_map.ENV_MAP_PATH: "   "}):
            self.assertEqual(site_map.map_path(), site_map.DEFAULT_MAP_PATH)

    def test_the_default_is_next_to_the_module(self):
        self.assertTrue(site_map.DEFAULT_MAP_PATH.endswith("sites_map.yaml"))
        self.assertTrue(os.path.isfile(site_map.DEFAULT_MAP_PATH))


class LoadTests(unittest.TestCase):
    """The file is read on demand, and re-read when it changes."""

    def setUp(self):
        site_map._CACHE.clear()
        self.addCleanup(site_map._CACHE.clear)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "map.yaml")

    def write(self, text: str) -> None:
        Path(self.path).write_text(text, encoding="utf-8")

    def test_an_absent_file_is_no_entries(self):
        self.assertEqual(site_map.load(os.path.join(self.directory.name, "nope.yaml")), {})

    def test_an_edit_is_picked_up_without_a_worker_restart(self):
        # The cache key is the file's own identity, not "already loaded": the map
        # is how a site's redesign gets fixed while the worker is running, so a
        # boolean cache would make a restart the only way to apply an edit.
        self.write("example.com:\n  content: '#first'\n")
        self.assertEqual(site_map.load(self.path)["example.com"].content, "#first")
        self.write("example.com:\n  content: '#second'\nother.example:\n  content: main\n")
        self.assertEqual(site_map.load(self.path)["example.com"].content, "#second")
        self.assertIn("other.example", site_map.load(self.path))

    def test_an_unchanged_file_is_read_once(self):
        self.write("example.com:\n  content: main\n")
        with mock.patch.object(site_map, "_read_yaml", wraps=site_map._read_yaml) as reader:
            site_map.load(self.path)
            site_map.load(self.path)
        self.assertEqual(reader.call_count, 1)

    def test_a_file_that_is_not_valid_yaml_is_a_warning_and_no_entries(self):
        self.write("example.com: [unclosed\n  content: ")
        with self.assertLogs("lumine", level="WARNING") as caught:
            self.assertEqual(site_map.load(self.path), {})
        self.assertTrue(any("not valid YAML" in line for line in caught.output))


class MissingPyYamlTests(unittest.TestCase):
    """PyYAML is optional, and its absence must not be an import failure."""

    def setUp(self):
        site_map._CACHE.clear()
        self.addCleanup(site_map._CACHE.clear)
        self.was_warned = site_map._WARNED_NO_YAML
        self.addCleanup(setattr, site_map, "_WARNED_NO_YAML", self.was_warned)
        site_map._WARNED_NO_YAML = False

    def test_an_absent_pyyaml_is_a_warning_rather_than_a_crash(self):
        # `sys.modules["yaml"] = None` is how `import yaml` is made to fail the
        # way a missing distribution does: `ImportError`, not `AttributeError`
        # from half an import.
        with mock.patch.dict(sys.modules, {"yaml": None}):
            with self.assertLogs("lumine", level="WARNING") as caught:
                self.assertIsNone(site_map._read_yaml(site_map.DEFAULT_MAP_PATH))
        self.assertTrue(any("PyYAML" in line for line in caught.output))

    def test_the_warning_is_said_once_per_process(self):
        # A warning that fires on every page read is noise, and noise is how a
        # real warning stops being noticed.
        with mock.patch.dict(sys.modules, {"yaml": None}):
            # The first call is *supposed* to warn; it is captured rather than
            # left loose so the suite's own output stays readable.
            with self.assertLogs("lumine", level="WARNING"):
                site_map._read_yaml(site_map.DEFAULT_MAP_PATH)
            with self.assertNoLogs("lumine", level="WARNING"):
                self.assertIsNone(site_map._read_yaml(site_map.DEFAULT_MAP_PATH))

    def test_the_browser_tools_load_anyway(self):
        # The difference between "per-site refinement is off" and "the tool does
        # not exist". `parse(None)` is the whole of that: no entries, no raise.
        self.assertEqual(site_map.parse(None), {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
