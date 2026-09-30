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

# The script path -- what ``python agent/agent.py`` actually exercises -- resolves
# each module by its bare name with ``agent/`` on ``sys.path``. A module regrouped
# into a subpackage is therefore imported *through* that subpackage:
# ``settings.config_store``, not ``config_store``. Listing the old flat name here
# would pass silently while production broke.
AGENT_MODULES = [
    "agent",
    # agent/ root -- entrypoint, the CLIs Tauri runs by filename, LiveKit helpers
    "provider_catalog",
    "validate_config",
    "livekit_token",
    "livekit_dispatch",
    "livekit_room",
    # settings/
    "settings.config_store",
    "settings.validation",
    "settings.llm_config",
    "settings.pipeline_config",
    "settings.session_preferences",
    "settings.providers",
    # pipeline/
    "pipeline.pipeline_factory",
    # runtime/
    "runtime.runtime_events",
    "runtime.failure_gate",
    "runtime.llm_errors",
    "runtime.context_trim",
    "runtime.latency",
    "runtime.emotion_contract",
    "runtime.emotion_voice",
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
        # Scan the regrouped subpackages as well. The glob used to reach only the
        # flat `agent/` folder, so moving a module into one of these would have
        # quietly taken it out of range of the very guard written for it.
        roots = [AGENT_DIR] + [AGENT_DIR / d for d in ("settings", "pipeline", "runtime")]
        paths = [p for root in roots for p in sorted(root.glob("*.py"))]
        for path in paths:
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
            "import pipeline.pipeline_factory as pipeline_factory\n"
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
            "import agent.settings.config_store, agent.settings.validation, agent.pipeline.pipeline_factory\n"
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
