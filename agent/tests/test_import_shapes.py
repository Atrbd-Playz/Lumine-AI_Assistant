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
    "perf_report",
    "livekit_token",
    "livekit_dispatch",
    "livekit_room",
    "whatsapp_probe",
    # The second WhatsApp diagnostic. It is a *separate* CLI rather than a flag on
    # `whatsapp_probe` because the two measure different states: that one reads a
    # chat with an empty composer, where the send button does not exist yet, and
    # this one types into a live one. Merging them means the report can only ever
    # describe one of the two states -- which is how a closed chat was diagnosed
    # as having stale selectors.
    "whatsapp_message_probe",
    # The send probe is a CLI rather than something to `exec` into a one-liner:
    # written that way it broke on the first line that read `sys.argv`, which is a
    # poor thing to hand somebody who is trying to verify a bug fix. It sends for
    # real unless given `--read-only`.
    "whatsapp_send_probe",
    # The Music settings probe. It lives at the root because `python_env::
    # run_agent_script` resolves by filename, and it walks with the same scanner
    # the tool plays from so the count it reports cannot disagree with what Lumine
    # would find.
    "media_probe",
    # The search behind the Music page's search box. A separate CLI rather than a
    # flag on `media_probe.py` because it is a different question — "what is in
    # here" versus "which of them did you mean" — and merging them would make one
    # of the two answers conditional on the other's arguments.
    "media_search",
    # The Music end-to-end check. Like `media_probe.py` it lives at the root and
    # is a script in its own right, so this guard is what proves
    # `tools/media/control.py`'s two-level imports survive the flat shape.
    "media_check",
    # The ranking report. Pure reads and one print, so it runs as a script against
    # a real library -- which is the only way to see whether a *ranking* is right.
    "media_rank",
    # The Music *widget*'s queue button. It is a root script because Rust runs it
    # by filename, and it is the second caller of `tools/media/queue.py` -- the
    # widget and the voice path must queue the same way, so the guard is what
    # proves both shapes survive.
    "media_queue",
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
    "runtime.visual_state",
    "runtime.frame_sampler",
    # The provider-boundary observer. It imports `frame_sampler` through the
    # package, so it has the same two shapes to survive -- and it is the module
    # whose whole job is to be *reachable* at the moment an image is built, so a
    # shape that fails is a feature that silently stops reporting.
    "runtime.vision_link",
    "runtime.permission_gate",
    "runtime.audit_log",
    "runtime.emotion_contract",
    "runtime.emotion_voice",
    # runtime/ -- the orchestration layer. Listed explicitly rather than left to
    # `tools.tools_registry` to pull in: these are the modules a future edit is
    # most likely to break with a function-level import, and the guard below only
    # sees files it is pointed at.
    "runtime.verification",
    "runtime.router",
    "runtime.task_model",
    "runtime.task_engine",
    "runtime.progress",
    "runtime.history_consent",
    # tools/ -- imported through its package by the script-shaped worker
    # (`from tools.tools_registry import get_tools`), so the package itself is
    # the name to exercise; every tool module comes with it.
    "tools.tools_registry",
    # tools/messaging/ -- named individually because each of these is reachable
    # as a script in its own right during debugging, and because the messaging
    # surface is where the two import shapes have collided before: a sibling
    # imported bare (`permission_gate`) works through the package and fails when
    # `runtime/` itself is the package, which only this guard notices.
    "tools.messaging.whatsapp",
    "tools.messaging.whatsapp_ops",
    "tools.messaging.whatsapp_refs",
    "tools.messaging.whatsapp_files",
    "tools.messaging.whatsapp_web",
    "tools.messaging.whatsapp_read",
    "tools.messaging.whatsapp_chats",
    "tools.messaging.whatsapp_account",
    # tools/desktop/ -- the same shape, and the same reason the messaging ones
    # are named individually: `whatsapp_desktop` reaches a sibling in this
    # package (`from ..desktop import uia`) and a top-level sibling in the
    # script shape (`from desktop import uia`). Getting that pair wrong works
    # through the package, fails when `tools/` itself is the package, and only
    # this guard notices -- which is exactly how it would reach a release.
    "tools.desktop.uia",
    "tools.desktop.input",
    "tools.desktop.capture",
    "tools.desktop.ops",
    "tools.desktop.control",
    # tools/media/ -- named individually for the reason the desktop ones are. The
    # tool module reaches *two* levels up (`...runtime`) as well as one
    # (`..tools_compat`), and those two fail under different conditions: through
    # the package both work, while in the script shape `tools/` is top-level so the
    # three-dot form is already past the root and only the `runtime` spelling
    # resolves. A single try block covering both would compile, import fine as
    # `agent.tools.media.control`, and fail on the first `python
    # agent/media_probe.py` -- which is the shape Tauri runs.
    "tools.media",
    "tools.media.library",
    "tools.media.match",
    "tools.media.scanner",
    "tools.media.player",
    "tools.media.state",
    "tools.media.control",
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
        roots = [AGENT_DIR] + [
            AGENT_DIR / d
            for d in (
                "settings",
                "pipeline",
                "runtime",
                "tools",
                "tools/browser",
                "tools/messaging",
                "tools/desktop",
                "tools/media",
            )
        ]
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
