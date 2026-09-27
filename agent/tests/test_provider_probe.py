"""The credential test, and the honesty rules around its verdict.

A connectivity check that reports "your key is wrong" when the truth is "our
request was malformed" or "the provider is down" is worse than no check: it sends
someone to re-enter a working credential. These tests pin the classification, and
they run against a local server so the suite never touches a real provider.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from agent import provider_probe
import agent.providers as providers_module
from agent.providers import PROVIDERS, ProbeDefinition, catalog_issues, get_provider


class _Handler(BaseHTTPRequestHandler):
    """Answers with whatever the test set up, and records what it was sent."""

    def _respond(self) -> None:
        type(self).received_headers = dict(self.headers)
        type(self).received_path = self.path
        status, body = type(self).reply
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, *args):  # noqa: ANN002 - silence the default logger
        return


class ProbeTestCase(unittest.TestCase):
    """Runs a local HTTP server and adds a provider pointed at it.

    A synthetic provider is used rather than a real one so the suite never
    touches a live API, and so the classification can be driven through every
    status code on demand.
    """

    reply = (200, {"ok": True})
    received_headers: dict = {}
    received_path = ""

    def setUp(self):
        _Handler.reply = self.reply
        _Handler.received_headers = {}
        _Handler.received_path = ""
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/probe"

        import agent.validation as validation_module

        self.addCleanup(self._restore)
        self.original = dict(PROVIDERS)
        self._install(
            ProbeDefinition(url=self.url, auth_header="X-Test-Key", auth_prefix="")
        )

    def _install(self, probe: ProbeDefinition | None) -> None:
        import agent.validation as validation_module

        provider = providers_module.ProviderDefinition(
            id="http",
            label="HTTP Test",
            requires_key=True,
            key_env=("HTTP_API_KEY",),
            setup_url="https://example.invalid/",
            notes="Exists only in this test.",
            probe=probe,
        )
        PROVIDERS[provider.id] = provider
        validation_module.PROVIDERS[provider.id] = provider

    def _restore(self):
        import agent.validation as validation_module

        PROVIDERS.clear()
        PROVIDERS.update(self.original)
        validation_module.PROVIDERS.clear()
        validation_module.PROVIDERS.update(self.original)
        self.server.shutdown()
        self.server.server_close()

    def probe(self, secret: str = "sk-test"):
        # Read at request time, not in setUp: a test sets `self.reply` on the
        # instance after setUp has already run, and a handler frozen with the
        # class default would answer 200 to everything.
        _Handler.reply = self.reply
        with mock.patch.dict("os.environ", {"HTTP_API_KEY": secret}, clear=False):
            return provider_probe.probe("http")

    def headers(self) -> dict:
        return _Handler.received_headers


class ValidCredentialTests(ProbeTestCase):
    def test_a_2xx_means_the_key_works(self):
        self.reply = (200, {"data": []})
        outcome = self.probe()
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["verdict"], provider_probe.VERDICT_VALID)
        self.assertEqual(outcome["status"], 200)
        self.assertIsInstance(outcome["latencyMs"], int)

    def test_the_secret_travels_in_the_header_the_catalog_names(self):
        self.reply = (200, {})
        self.probe("sk-abc123")
        # A key in a URL would land in access logs; the header keeps it out.
        self.assertEqual(self.headers().get("X-Test-Key"), "sk-abc123")
        self.assertNotIn("sk-abc123", _Handler.received_path)

    def test_fixed_headers_are_sent(self):
        self.reply = (200, {})
        self._install(ProbeDefinition(url=self.url, extra_headers=(("X-Version", "2025-01-01"),)))
        self.probe()
        self.assertEqual(self.headers().get("X-Version"), "2025-01-01")

    def test_a_bearer_prefix_is_applied(self):
        self.reply = (200, {})
        self._install(ProbeDefinition(url=self.url, auth_header="Authorization", auth_prefix="Bearer "))
        self.probe("sk-xyz")
        self.assertEqual(self.headers().get("Authorization"), "Bearer sk-xyz")


class RejectedCredentialTests(ProbeTestCase):
    def test_a_listed_status_is_a_rejected_key(self):
        self.reply = (401, {"error": {"message": "Invalid API Key"}})
        outcome = self.probe()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["verdict"], provider_probe.VERDICT_REJECTED)
        self.assertEqual(outcome["status"], 401)

    def test_the_providers_own_message_is_surfaced(self):
        self.reply = (403, {"error": {"message": "Key not authorised for this project"}})
        self.assertIn("not authorised", self.probe()["detail"])

    def test_a_rejection_exits_non_zero(self):
        # A calling script has to be able to tell a rejection from an outage.
        with mock.patch.object(provider_probe, "probe", return_value={"verdict": "rejected", "ok": False}):
            self.assertEqual(provider_probe.main(["http"]), 1)
        with mock.patch.object(provider_probe, "probe", return_value={"verdict": "inconclusive", "ok": False}):
            self.assertEqual(provider_probe.main(["http"]), 0)
        with mock.patch.object(provider_probe, "probe", return_value={"verdict": "valid", "ok": True}):
            self.assertEqual(provider_probe.main(["http"]), 0)


class InconclusiveTests(ProbeTestCase):
    """The cases that must never be reported as a bad credential."""

    def test_a_404_is_inconclusive(self):
        # A wrong path is our mistake, not the user's key.
        self.reply = (404, {"error": {"message": "no such route"}})
        outcome = self.probe()
        self.assertEqual(outcome["verdict"], provider_probe.VERDICT_INCONCLUSIVE)
        self.assertNotEqual(outcome["verdict"], provider_probe.VERDICT_REJECTED)

    def test_a_405_is_inconclusive(self):
        # Verified against Cartesia: a method check runs before authentication, so
        # 405 says nothing at all about the key.
        self.reply = (405, {})
        self.assertEqual(self.probe()["verdict"], provider_probe.VERDICT_INCONCLUSIVE)

    def test_a_server_error_is_inconclusive(self):
        self.reply = (503, {"error": {"message": "upstream unavailable"}})
        outcome = self.probe()
        self.assertEqual(outcome["verdict"], provider_probe.VERDICT_INCONCLUSIVE)
        self.assertIn("does not indicate", outcome["detail"])

    def test_an_unreachable_host_is_inconclusive(self):
        self._install(ProbeDefinition(url="http://127.0.0.1:1/nowhere"))
        outcome = self.probe()
        self.assertEqual(outcome["verdict"], provider_probe.VERDICT_INCONCLUSIVE)
        # The message must not be able to carry a secret.
        self.assertNotIn("sk-test", json.dumps(outcome))

    def test_a_google_style_400_is_only_rejected_when_the_catalog_says_so(self):
        # Google answers an invalid key with 400, not 401. Treating 4xx generically
        # would be wrong in both directions.
        self.reply = (400, {"error": {"message": "API key not valid."}})
        self.assertEqual(self.probe()["verdict"], provider_probe.VERDICT_INCONCLUSIVE)

        self._install(
            ProbeDefinition(url=self.url, invalid_status=(400, 401, 403))
        )
        outcome = self.probe()
        self.assertEqual(outcome["verdict"], provider_probe.VERDICT_REJECTED)
        self.assertIn("not valid", outcome["detail"])


class NoCredentialTests(ProbeTestCase):
    def test_nothing_set_is_reported_as_no_secret(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            outcome = provider_probe.probe("http")
        self.assertEqual(outcome["verdict"], provider_probe.VERDICT_NO_SECRET)
        self.assertIn("HTTP_API_KEY", outcome["detail"])

    def test_an_empty_credential_is_not_sent(self):
        with mock.patch.dict("os.environ", {"HTTP_API_KEY": "   "}, clear=False):
            outcome = provider_probe.probe("http")
        self.assertEqual(outcome["verdict"], provider_probe.VERDICT_NO_SECRET)

    def test_a_provider_with_no_probe_says_so_rather_than_pretending(self):
        self._install(None)
        outcome = self.probe()
        self.assertEqual(outcome["verdict"], provider_probe.VERDICT_NO_PROBE)

    def test_an_unknown_provider_is_reported(self):
        self.assertEqual(
            provider_probe.probe("nope")["verdict"], provider_probe.VERDICT_UNKNOWN_PROVIDER
        )

    def test_the_probe_never_raises(self):
        # Every failure is a verdict, because the caller is a settings screen that
        # needs something to show.
        for provider_id in ("http", "nope", "silero", "livekit"):
            with self.subTest(provider=provider_id):
                self.assertIn("verdict", provider_probe.probe(provider_id))


class CatalogProbeTests(unittest.TestCase):
    def test_every_probe_is_https(self):
        # The probe sends a real credential to this address.
        for provider in PROVIDERS.values():
            if provider.probe is not None:
                with self.subTest(provider=provider.id):
                    self.assertTrue(provider.probe.url.startswith("https://"))

    def test_every_probe_says_which_statuses_mean_a_bad_key(self):
        # Without this a wrong key and an outage are indistinguishable.
        for provider in PROVIDERS.values():
            if provider.probe is not None:
                with self.subTest(provider=provider.id):
                    self.assertTrue(provider.probe.invalid_status)
                    self.assertNotIn(200, provider.probe.invalid_status)

    def test_a_probe_only_exists_for_a_provider_that_needs_a_key(self):
        for provider in PROVIDERS.values():
            if provider.probe is not None:
                with self.subTest(provider=provider.id):
                    self.assertTrue(provider.requires_key)
                    self.assertTrue(provider.key_env)

    def test_google_answers_a_bad_key_with_400_not_401(self):
        # Verified against the live API. If Google ever changes this, the
        # `invalid_status` list is the thing to change with it.
        google = get_provider("google")
        assert google is not None and google.probe is not None
        self.assertIn(400, google.probe.invalid_status)

    def test_the_catalog_self_check_still_passes_with_probes(self):
        self.assertEqual(catalog_issues(), [])

    def test_a_plain_http_probe_is_rejected_by_the_self_check(self):
        broken = get_provider("groq")
        assert broken is not None
        import agent.providers as providers_module

        bad = providers_module.ProviderDefinition(
            **{
                **broken.__dict__,
                "probe": ProbeDefinition(url="http://api.groq.com/openai/v1/models"),
            }
        )
        issues = providers_module.catalog_issues([bad])
        self.assertTrue(any("https" in issue for issue in issues), issues)

    def test_the_public_catalog_never_carries_a_secret(self):
        from agent.providers import to_public_catalog

        blob = json.dumps(to_public_catalog())
        for provider in PROVIDERS.values():
            if provider.probe is not None:
                with self.subTest(provider=provider.id):
                    # The header *name* and fixed headers are public; nothing that
                    # could be a credential may appear.
                    self.assertIn(provider.probe.url, blob)
        for forbidden in ("sk-", "Bearer ey", "api_key="):
            self.assertNotIn(forbidden, blob)


if __name__ == "__main__":
    unittest.main()
