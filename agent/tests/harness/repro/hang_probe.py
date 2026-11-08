"""Reproduce and diagnose the full-suite hang observed at Phase 16.

Runs every certification test in file order with ``faulthandler`` dumping every
thread's stack on a timer, so a wedged await is visible as a traceback instead of
as silence. This is a diagnostic only; it changes nothing about the product.

Usage::

    .\\.venv\\Scripts\\python.exe agent\\tests\\harness\\repro\\hang_probe.py
"""

from __future__ import annotations

import faulthandler
import sys
import time
import unittest
from pathlib import Path

# `agent/agent.py` shadows the `agent` package when the agent directory comes
# first on sys.path, so both go on in that order and the root is what makes the
# package importable.
agent_dir = Path(__file__).resolve().parents[3]
repo_root = agent_dir.parent
for entry in (str(agent_dir), str(repo_root)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from agent.tests.harness.test_certification_phases import BrowserCertificationTestCase


def main() -> None:
    # A few seconds of grace per test, then a stack dump every 30s. The dump is
    # the whole point: a `Future pending` with no timeout leaves no other trace.
    faulthandler.dump_traceback_later(45, repeat=True, file=sys.stderr)
    print(f"hang probe started at {time.strftime('%H:%M:%S')}", flush=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(BrowserCertificationTestCase)
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stderr)
    result = runner.run(suite)
    faulthandler.cancel_dump_traceback_later()
    print(
        f"hang probe finished at {time.strftime('%H:%M:%S')} "
        f"tests={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
