"""Run two certification tests with full browser logging captured."""
import logging
import sys
import unittest

sys.path.insert(0, r"G:\Project\Lumine-AI_Assistant")
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(
            r"G:\Project\Lumine-AI_Assistant\agent\tests\harness\evidence\browser_debug.log",
            mode="w",
            encoding="utf-8",
        )
    ],
)
for name in ("asyncio", "urllib3", "websockets"):
    logging.getLogger(name).setLevel(logging.WARNING)

from agent.tests.harness import test_certification_phases as phases

suite = unittest.TestSuite()
loader = unittest.TestLoader()
for name in (
    "test_01_01_browser_launch_and_navigation",
    "test_01_02_redirect_handling",
):
    suite.addTest(phases.BrowserCertificationTestCase(name))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
