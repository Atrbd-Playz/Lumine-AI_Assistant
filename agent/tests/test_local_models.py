"""Discovery for a local model server.

The unit under test here is the *parsing* and *address arithmetic*, not the
network. A test that started a real model server would be slow, would need a GPU
or a multi-gigabyte download, and would fail on a machine that is not that
machine — which is the definition of a test suite that gets skipped rather than
run. The HTTP call is exercised by faking `httpx.get`, so the shapes that
actually break (`/v1` stripping, an empty server, a server that only speaks
OpenAI) are all reachable in milliseconds.
"""

import json
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

from agent import local_models

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class FakeResponse:
    def __init__(self, status_code: int, body=None, *, invalid_json: bool = False):
        self.status_code = status_code
        self._body = body
        self._invalid = invalid_json

    def json(self):
        if self._invalid:
            raise ValueError("not json")
        return self._body


class FakeHttpx:
    """A stand-in for the one `httpx.get` call the script makes.

    Records the paths it was asked for, because *which* route a server answered
    is part of the answer: the script reports it so the settings screen can say
    whether it found an Ollama or an LM Studio.
    """

    def __init__(self, routes: dict[str, FakeResponse], error: Exception | None = None):
        self.routes = routes
        self.error = error
        self.asked: list[str] = []

    def get(self, url: str, timeout: float | None = None):
        self.asked.append(url)
        if self.error is not None:
            raise self.error
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path
        if path not in self.routes:
            return FakeResponse(404)
        return self.routes[path]


def _with_fake_httpx(fake: FakeHttpx):
    """Install `fake` as the `httpx` module, and return the patched object.

    The script imports `httpx` inside the function so a missing dependency is a
    payload rather than an ImportError at module load. Patching
    `sys.modules` is therefore the only way in -- and it also means the test does
    not need `httpx` installed, which matters because the agent's dependency set
    is not this test's to assume.
    """
    return unittest.mock.patch.dict(sys.modules, {"httpx": fake})


class RootDerivationTests(unittest.TestCase):
    """`/v1` is the OpenAI surface; `/api/tags` is the server's own.

    They sit on the same port, so a discovery request appended to a base URL
    that already ends in `/v1` asks for a route that does not exist and gets a
    404 -- which reads as "no model server here" when in fact one is running a
    few characters away.
    """

    def test_drops_the_openai_version_prefix(self):
        self.assertEqual(local_models._root("http://localhost:11434/v1"), "http://localhost:11434")

    def test_drops_a_trailing_slash_first(self):
        # Order matters. A `/v1/` would not match the suffix, and the request
        # would go to `/v1//api/tags`.
        self.assertEqual(local_models._root("http://localhost:11434/v1/"), "http://localhost:11434")

    def test_handles_the_other_versioned_prefixes(self):
        for base in ("http://host/api/v1", "http://host/openai/v1"):
            self.assertEqual(local_models._root(base), "http://host", base)

    def test_leaves_a_bare_root_alone(self):
        # Some servers are configured to serve OpenAI routes from the root. There
        # is no prefix to remove, and inventing one would break them.
        self.assertEqual(local_models._root("http://host:1234"), "http://host:1234")

    def test_does_not_strip_a_partial_match(self):
        # `/v1beta` is not `/v1`. Truncating it would produce `/v1beta` -> `` and
        # then request the root, which answers something unrelated.
        self.assertEqual(local_models._root("http://host/v1beta"), "http://host/v1beta")


class ShapeParsingTests(unittest.TestCase):
    def test_reads_ollama_tags(self):
        body = {
            "models": [
                {"name": "qwen3:8b", "size": 5207029248, "digest": "abc", "details": {"family": "qwen3"}},
                {"name": "llama3.2:3b", "size": 2019393189},
            ]
        }
        rows = local_models._models_from_tags(body)
        self.assertEqual([row["id"] for row in rows], ["llama3.2:3b", "qwen3:8b"])
        self.assertEqual(rows[0]["size"], 2019393189)

    def test_reads_the_openai_shape(self):
        body = {"object": "list", "data": [{"id": "gemma-3-12b"}, {"id": "phi-4"}]}
        self.assertEqual(
            [row["id"] for row in local_models._models_from_openai(body)],
            ["gemma-3-12b", "phi-4"],
        )

    def test_sorts_case_insensitively(self):
        # A server's own order is pull order, which is not a ranking. Capitalised
        # names must not all pile up at the end as though they were a separate
        # group of things.
        body = {"models": [{"name": "zeta"}, {"name": "Alpha"}, {"name": "beta"}]}
        self.assertEqual(
            [row["id"] for row in local_models._models_from_tags(body)],
            ["Alpha", "beta", "zeta"],
        )

    def test_survives_a_shape_it_does_not_recognise(self):
        # A server that answers 200 with something else. Returning [] is right;
        # raising would turn "unexpected response" into a crash in a settings
        # screen.
        for body in (None, [], {}, {"models": "nope"}, {"models": [None, 3, {}]}, {"data": {}}):
            self.assertEqual(local_models._models_from_tags(body), [], body)
            self.assertEqual(local_models._models_from_openai(body), [], body)

    def test_skips_an_entry_with_no_name(self):
        # A nameless entry is a row the user cannot pick, so it must not become a
        # blank option in the dropdown.
        rows = local_models._models_from_tags({"models": [{"name": "  "}, {"name": "ok:1b"}]})
        self.assertEqual([row["id"] for row in rows], ["ok:1b"])

    def test_ignores_a_nonsensical_size(self):
        # `size` is a nicety -- it is what makes "3B" more useful than "3b" -- so a
        # missing or absurd one drops the field instead of failing the read.
        rows = local_models._models_from_tags({"models": [{"name": "a", "size": 0}, {"name": "b", "size": "12"}]})
        self.assertEqual(rows, [{"id": "a"}, {"id": "b"}])


class DiscoveryTests(unittest.TestCase):
    def _run(self, fake: FakeHttpx, base_url="http://localhost:11434/v1"):
        with _with_fake_httpx(fake):
            return local_models.discover(base_url)

    def test_prefers_the_servers_own_route(self):
        fake = FakeHttpx(
            {
                "/api/tags": FakeResponse(200, {"models": [{"name": "qwen3:8b"}]}),
                "/v1/models": FakeResponse(200, {"data": [{"id": "openai-model"}]}),
            }
        )
        payload = self._run(fake)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["route"], "/api/tags")
        self.assertEqual([row["id"] for row in payload["models"]], ["qwen3:8b"])
        # Stopped at the first hit. Asking twice is a second round trip for a
        # second opinion nobody needs.
        self.assertEqual(fake.asked, ["http://localhost:11434/api/tags"])

    def test_falls_back_to_the_openai_route(self):
        # LM Studio, vLLM, llama.cpp's server and text-generation-webui all speak
        # OpenAI and none of them answer `/api/tags`. Which of the two exists is a
        # property of the server, not a question to put to the user.
        fake = FakeHttpx(
            {
                "/api/tags": FakeResponse(404),
                "/v1/models": FakeResponse(200, {"data": [{"id": "gemma-3-12b-it-qat"}]}),
            }
        )
        payload = self._run(fake)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["route"], "/v1/models")
        self.assertEqual([row["id"] for row in payload["models"]], ["gemma-3-12b-it-qat"])

    def test_a_running_server_with_nothing_pulled_is_reachable_but_empty(self):
        # The two states need different words. This one means "pull a model"; the
        # other means "start the server". Reporting them the same way sends people
        # to look in the wrong place.
        fake = FakeHttpx({"/api/tags": FakeResponse(200, {"models": []})})
        payload = self._run(fake)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["models"], [])
        self.assertIn("no models", payload["detail"])

    def test_an_unreachable_server_fails_without_raising(self):
        # The caller is a dropdown. It needs a reason to show, not an exception.
        fake = FakeHttpx({}, error=ConnectionRefusedError("connection refused"))
        payload = self._run(fake)
        self.assertFalse(payload["ok"])
        self.assertIn("ConnectionRefusedError", payload["error"])
        self.assertIn("Start the model server", payload["detail"])

    def test_a_server_answering_something_that_is_not_json(self):
        # A 200 with HTML is a proxy or a captive portal, not a model server.
        fake = FakeHttpx(
            {
                "/api/tags": FakeResponse(200, invalid_json=True),
                "/v1/models": FakeResponse(200, invalid_json=True),
            }
        )
        payload = self._run(fake)
        self.assertFalse(payload["ok"])
        self.assertIn("no JSON", payload["error"])

    def test_both_routes_rejecting_is_a_failure(self):
        fake = FakeHttpx({"/api/tags": FakeResponse(500), "/v1/models": FakeResponse(403)})
        payload = self._run(fake)
        self.assertFalse(payload["ok"])
        # The *last* answer is reported, because that is the one from the route a
        # real OpenAI-compatible server would have used.
        self.assertIn("403", payload["error"])

    def test_reports_the_address_it_asked(self):
        # So a person can see that Lumine looked where they thought it looked.
        fake = FakeHttpx({"/api/tags": FakeResponse(200, {"models": [{"name": "a"}]})})
        payload = self._run(fake, "http://127.0.0.1:11434/v1")
        self.assertEqual(payload["baseUrl"], "http://127.0.0.1:11434/v1")
        self.assertEqual(fake.asked, ["http://127.0.0.1:11434/api/tags"])

    def test_measures_a_latency(self):
        fake = FakeHttpx({"/api/tags": FakeResponse(200, {"models": [{"name": "a"}]})})
        payload = self._run(fake)
        self.assertIsInstance(payload["latencyMs"], int)
        self.assertGreaterEqual(payload["latencyMs"], 0)


class DefaultAddressTests(unittest.TestCase):
    def test_reads_the_address_from_the_catalog(self):
        # Declared once, in the catalog, so a profile saved with the default and
        # one resolved from the catalog cannot disagree.
        self.assertEqual(local_models.default_base_url(), "http://localhost:11434/v1")

    def test_falls_back_for_an_unknown_provider(self):
        self.assertEqual(local_models.default_base_url("nope"), local_models.FALLBACK_BASE_URL)


class CommandLineTests(unittest.TestCase):
    """The script is a subprocess target, so its exit code is part of its API.

    Rust reads stdout on success and the exit code decides whether there is
    anything to read. A script that prints a perfectly good payload and exits 1
    would be reported by the desktop layer as a crash.
    """

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "agent" / "local_models.py"), *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=60,
        )

    def test_a_bare_address_is_named_as_a_typo(self):
        # The most likely mistake, and naming it beats the connection error it
        # would otherwise produce.
        result = self._run("localhost:11434")
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "not_an_address")
        self.assertIn("http://", payload["detail"])
        # Zero even here. See `main`: a non-zero exit would make the desktop layer
        # discard the message it just printed.
        self.assertEqual(result.returncode, 0)

    def test_an_unreachable_server_still_prints_its_reason(self):
        # Port 9 is the discard service, so nothing is listening on it. The point
        # is that the reason survives to stdout, and that the exit code does not
        # take it with the whole stdout buffer.
        result = self._run("http://127.0.0.1:9/v1", "--timeout", "0.2")
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("detail", payload)
        self.assertIn("Start the model server", payload["detail"])


if __name__ == "__main__":
    unittest.main()
