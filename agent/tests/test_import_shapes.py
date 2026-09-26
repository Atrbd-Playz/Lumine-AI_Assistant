"""Import-shape tests.

Every other test in this suite imports modules as ``agent.<name>``, which is the
package context. The worker does **not** do that: ``python agent/agent.py`` loads
the modules as top-level scripts, so the ``try: from .x import`` / ``except:
from x import`` fallback is the path that actually runs in production.

A function-level relative import is invisible to the rest of the suite and
crashes the first real voice session. These tests run the worker the way the
runtime does, so that class of bug fails here instead.
"""

import subprocess
import sys
import unittest
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
AGENT_MODULES = [
    "agent",
    "emotion_contract",
    "latency",
    "runtime_events",
    "session_preferences",
    "llm_config",
    "pipeline_config",
    "providers",
    "provider_catalog",
    "validation",
    "config_store",
    "validate_config",
    "pipeline_factory",
    "livekit_token",
    "livekit_dispatch",
    "livekit_room",
]


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-W", "ignore", "-c", script],
        cwd=AGENT_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )


class TopLevelImportTests(unittest.TestCase):
    """Each module must import cleanly as a top-level script."""

    def test_every_module_imports_without_a_parent_package(self):
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(AGENT_DIR)!r})\n"
            "import importlib\n"
            f"names = {AGENT_MODULES!r}\n"
            "for name in names:\n"
            "    importlib.import_module(name)\n"
            "print('ok')\n"
        )
        result = _run(script)
        self.assertEqual(
            result.returncode,
            0,
            f"top-level import failed:\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("ok", result.stdout)

    def test_no_module_carries_a_function_level_relative_import(self):
        """A relative import inside a function body breaks the script path.

        Module-scope ones are fine, because they sit inside the try/except
        fallback. This is a cheap structural guard for the mistake that caused a
        crashed voice session.
        """
        import ast

        offenders: list[str] = []
        for path in sorted(AGENT_DIR.glob("*.py")):
            if path.name in {"__init__.py", "agent.py"}:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # Only function and method bodies, not module scope.
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, ast.ImportFrom) and inner.level and inner.level > 0:
                        offenders.append(f"{path.name}:{inner.lineno} inside {node.name}()")
        self.assertEqual(offenders, [], "use a module-scope try/except instead")

    def test_the_worker_entrypoint_resolves_a_profile_as_a_script(self):
        """Exercise the exact call that crashed: build_pipeline from a script."""
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(AGENT_DIR)!r})\n"
            "import asyncio\n"
            "import pipeline_factory\n"
            "resolved = pipeline_factory.resolve_profile(\n"
            "    pipeline_factory.env_profile('legacy_cascade'),\n"
            "    interruption_mode='barge_in',\n"
            ")\n"
            "print(resolved.profile_id, resolved.kind, resolved.llm.model)\n"
        )
        result = _run(script)
        self.assertEqual(result.returncode, 0, f"failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("pipeline", result.stdout)

    def test_the_config_cli_runs_as_a_script(self):
        script = f"import runpy, sys; sys.argv = ['validate_config.py', '--describe']"
        result = _run(script)
        # runpy is awkward to assert on; invoking the file directly is the real test.
        direct = subprocess.run(
            [sys.executable, "-W", "ignore", str(AGENT_DIR / "validate_config.py"), "--describe"],
            cwd=AGENT_DIR,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertIn('"source"', direct.stdout)
        self.assertEqual(result.returncode, 0)

    def test_the_catalog_cli_runs_as_a_script(self):
        direct = subprocess.run(
            [sys.executable, "-W", "ignore", str(AGENT_DIR / "provider_catalog.py"), "--check"],
            cwd=AGENT_DIR,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertIn("catalog ok", direct.stdout)


class PackageImportTests(unittest.TestCase):
    def test_modules_also_import_as_a_package(self):
        script = (
            "import agent.config_store, agent.validation, agent.pipeline_factory\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-W", "ignore", "-c", script],
            cwd=AGENT_DIR.parent,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
