"""The only WhatsApp messaging implementation is the isolated Web session."""

from __future__ import annotations

import unittest
from pathlib import Path

from agent.tools.messaging import whatsapp, whatsapp_ops as ops


class WebOnlyTests(unittest.TestCase):
    def test_desktop_messaging_adapter_is_removed(self):
        module_path = Path(whatsapp.__file__).with_name("whatsapp_desktop.py")
        self.assertFalse(module_path.exists())
        self.assertFalse(hasattr(whatsapp, "desktop_backend"))

    def test_every_backend_candidate_is_whatsapp_web(self):
        self.assertEqual(
            [backend.name for backend in whatsapp.backend_candidates()],
            ["WhatsApp Web"],
        )

    def test_operation_catalog_exposes_only_web(self):
        self.assertEqual(ops.PATHS, ("web",))
        self.assertFalse(
            any(ops.supported_by(operation.verb, "desktop") for operation in ops.OPERATIONS)
        )


class LaunchingTheInstalledAppIsSeparate(unittest.TestCase):
    def test_explicit_desktop_app_request_still_routes_to_open_app(self):
        from agent.runtime import router
        from agent.tests.test_router import ALL_TOOLS

        facts = router.MachineFacts.build(installed={"whatsapp"})
        for request in ("open desktop whatsapp", "open the whatsapp app"):
            with self.subTest(request=request):
                route = router.classify(request, facts, tools=ALL_TOOLS)
                self.assertEqual(route.tool, "open_app")
                self.assertEqual(route.environment, router.DESKTOP)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
