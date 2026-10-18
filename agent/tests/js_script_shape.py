"""Parse the in-page scripts a package ships, with a real JavaScript parser.

This is the shared half of `test_browser_script_shape.py` and
`test_messaging_script_shape.py`. The two packages are in the same business --
they hand strings to ``page.evaluate`` -- and they have both been bitten by the
same class of fault, where a script that does not parse is not a crash but a
*silent no-op*:

* In the browser package, `_JS_REGION_TEMPLATE` declared ``const budget`` under a
  parameter of the same name. Playwright reports a ``SyntaxError`` from ``evaluate``
  as a failed call, the caller logged a warning and returned the empty answer it
  returns for a page that went away, and the per-site map did nothing on every page
  while the suite stayed green.

* In the messaging package, the same shape has been found repeatedly and is
  written up at length in ``agent/AGENTS.md``: a helper concatenated in front of an
  arrow function parses as ``f(){...}(x) => {...}`` and the page answers
  ``SyntaxError``, so ``open_chat`` returned ``False`` for chats that opened
  perfectly well by hand.

A brace balance is not the right instrument for either. It passes ``const budget``
declared twice, and it passes a script that is syntactically wrong somewhere a brace
does not go -- so this compiles them instead, with ``vm.Script``.

``node --check`` would also compile them, and was the first choice. It is not used
because a ``.js`` file passed to ``node`` is wrapped in a CommonJS function, where a
**top-level return is legal** -- which made ``_JS_STALE``, a *fragment*, appear to
parse on its own. The browser has no such wrapper.
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import shutil
import subprocess
import tempfile
import unittest

#: Compiles every script and prints ``label -> reason`` as JSON. A blank reason
#: means it parsed. One process rather than one per script: thirteen invocations
#: cost seven seconds for three tests, on this machine more than the 261-test
#: WhatsApp suite beside it.
_DRIVER = """
const fs = require('fs');
const vm = require('vm');
const scripts = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const out = {};
for (const name of Object.keys(scripts)) {
  try {
    new vm.Script(scripts[name], { filename: name });
    out[name] = '';
  } catch (err) {
    out[name] = String((err && err.message) || err);
  }
}
process.stdout.write(JSON.stringify(out));
"""


def candidate_strings(module) -> dict[str, str]:
    """Module-level, non-dunder, multi-line strings.

    Each qualifier is load-bearing: **module-level** so a message composed inside a
    function is not mistaken for one; **non-dunder** because ``__doc__`` is a
    multi-line string in the modules that hold these; **multi-line** because no
    script here is a single line, and that is what keeps a module-level refusal
    message out of the set.
    """
    return {
        name: value
        for name, value in vars(module).items()
        if name.startswith("_")
        and not name.startswith("__")
        and isinstance(value, str)
        and "\n" in value
    }


def partition(package) -> tuple[dict[str, str], dict[str, str]]:
    """Split a package's scripts from its inlined fragments.

    Returns ``(scripts, fragments)``. A fragment is one whose text appears verbatim
    inside another of the module's strings -- that is what *inlined* means here, and
    saying it directly is more honest than inferring it from a concatenation in the
    source, which would also catch a constant merely referenced elsewhere.

    Everything excluded is excluded from *parsing*, never from *coverage*:
    ``test_a_fragment_is_covered_by_the_script_that_holds_it`` asserts each
    fragment's text survives inside a script that is parsed.
    """
    scripts: dict[str, str] = {}
    fragments: dict[str, str] = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        short = module.__name__.rsplit(".", 1)[-1]
        strings = candidate_strings(module)
        for name, value in strings.items():
            label = f"{short}.{name}"
            if any(value in other for other_name, other in strings.items() if other_name != name):
                fragments[label] = value
            else:
                scripts[label] = value
    return scripts, fragments


def discover_scripts(package) -> dict[str, str]:
    """Every script a package hands to ``page.evaluate``."""
    scripts, _fragments = partition(package)
    return scripts


def check_all(scripts: dict[str, str]) -> dict[str, str]:
    """Parse every script in one node process.

    Returns ``label -> reason``, where a blank reason means the script parsed. The
    report arrives as JSON rather than as an exit code so that one failure does not
    lose the others, and each one keeps its own name for the ``subTest``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        payload = os.path.join(tmp, "scripts.json")
        # Written to a file and not passed as argv: a script here runs to several
        # kilobytes and thirty of them together sit well past the length Windows
        # puts on a command line.
        with open(payload, "w", encoding="utf-8") as handle:
            json.dump(scripts, handle, ensure_ascii=False)
        result = subprocess.run(
            ["node", "-e", _DRIVER, payload], capture_output=True, text=True, timeout=60
        )
    if result.returncode != 0:
        raise AssertionError(
            "the parse driver itself failed, so no script was parsed:\n"
            f"{(result.stderr or result.stdout or '').strip()[:600]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            "the parse driver wrote no report:\n"
            f"{(result.stdout or result.stderr or '').strip()[:600]}"
        ) from None


class ScriptShapeSuite(unittest.TestCase):
    """Parse every script ``PACKAGE`` hands to ``page.evaluate``.

    Subclasses name the package, the scripts whose discovery must keep working, and
    one known fragment -- the case the exclusion rule exists for.
    """

    #: The package whose module-level strings are the scripts.
    PACKAGE = None

    #: Labels whose absence would mean discovery stopped finding scripts. Named
    #: rather than counted: a count is satisfied by four wrong strings, and by
    #: three scripts nothing depends on.
    CRITICAL_SCRIPTS: tuple[str, ...] = ()

    #: A fragment this package is known to ship, and the scripts that hold it. The
    #: point is that the rule recognises a *specific* one, not that some string
    #: happens to be a substring of another.
    KNOWN_FRAGMENT: str = ""
    KNOWN_HOLDERS: tuple[str, ...] = ()

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if cls.PACKAGE is None:
            raise unittest.SkipTest("not a runnable suite")
        if not _has_node():
            # Skipped rather than failed: `test_observe_controls.py` cannot run
            # without node either, so the suite already says so, and a clear skip
            # beats a FileNotFoundError from deep inside a subtest.
            raise unittest.SkipTest(
                "node is not on PATH; test_observe_controls.py runs the observe "
                "script through it, so the suite expects a JavaScript parser"
            )

    def test_the_discovery_finds_the_scripts_and_only_scripts(self):
        # The anti-vacuity guard for discovery. A rule that matches nothing makes
        # the test below pass while checking nothing at all, which is the one way
        # this suite could be worse than not having it.
        found = discover_scripts(self.PACKAGE)
        self.assertTrue(found, f"{self.PACKAGE.__name__} exposed no scripts at all")
        for expected in self.CRITICAL_SCRIPTS:
            self.assertIn(
                expected,
                found,
                f"{expected} was not discovered -- the discovery rule has stopped "
                "seeing scripts, so the parse test below is checking nothing",
            )
        for label, source in found.items():
            with self.subTest(script=label):
                self.assertTrue(source.strip(), f"{label} is empty")

    def test_a_fragment_is_covered_by_the_script_that_holds_it(self):
        # "Not parsed on its own" must never quietly become "not checked". The
        # property is total -- every candidate string is either parsed or present,
        # verbatim, inside one that is -- and it is asserted for every fragment
        # rather than for a list, because a fragment nobody wrote down is the one
        # that would be missed.
        scripts, fragments = partition(self.PACKAGE)
        self.assertTrue(scripts, "no script was discovered at all")
        for label, source in fragments.items():
            with self.subTest(fragment=label):
                holders = [name for name, other in scripts.items() if source in other]
                self.assertTrue(
                    holders,
                    f"{label} is treated as a fragment but no parsed script contains "
                    "it, so it is not checked at all",
                )

    def test_every_script_parses(self):
        scripts = discover_scripts(self.PACKAGE)
        reasons = check_all(scripts)
        for label in sorted(scripts):
            with self.subTest(script=label):
                reason = reasons.get(
                    label, "the parser reported nothing at all for this script"
                )
                self.assertFalse(
                    reason,
                    f"{label} does not parse. Playwright reports this as a failed "
                    "evaluate and the caller falls back to a shorter read, so the "
                    f"script would silently do nothing:\n{reason}",
                )

    def test_the_known_fragment_is_recognised_as_one(self):
        # The case the exclusion rule exists for. A fragment that is not recognised
        # is parsed alone, and the advice it produces -- "fix the fragment" -- is
        # wrong, because a fragment is legal only inside the script that holds it.
        if not self.KNOWN_FRAGMENT:
            self.skipTest("this package ships no inlined fragment")

        scripts, fragments = partition(self.PACKAGE)
        self.assertIn(self.KNOWN_FRAGMENT, fragments)
        self.assertNotIn(self.KNOWN_FRAGMENT, scripts)
        for holder in self.KNOWN_HOLDERS:
            with self.subTest(holder=holder):
                self.assertIn(holder, scripts)
                self.assertIn(fragments[self.KNOWN_FRAGMENT], scripts[holder])

    def test_the_check_would_have_caught_the_measured_bug(self):
        """The harness must discriminate, or the test above is decoration.

        The region reader shipped with this exact defect and a green suite around
        it, so the bug's own shape is fed through the same path: it has to fail, and
        the corrected shape has to pass. A driver that returned blanks for
        everything would make this whole suite a no-op, which is precisely how the
        original bug stayed invisible.
        """
        buggy = (
            "([selectors, fieldMap, budget]) => {\n"
            "  const budget = Math.max(200, Number(budget) || 2400);\n"
            "  return budget;\n"
            "}\n"
        )
        fixed = (
            "([selectors, fieldMap, charBudget]) => {\n"
            "  const budget = Math.max(200, Number(charBudget) || 2400);\n"
            "  return budget;\n"
            "}\n"
        )

        reasons = check_all({"buggy": buggy, "fixed": fixed})
        self.assertTrue(
            reasons.get("buggy"),
            "the parser accepted a parameter redeclared in its own scope, so it "
            "would also have accepted the bug that shipped -- this guard is broken",
        )
        self.assertEqual(
            reasons.get("fixed"),
            "",
            "the parser rejected the corrected shape, so the parse test is failing "
            "scripts for a reason that is not a syntax error",
        )


def _has_node() -> bool:
    import shutil

    return shutil.which("node") is not None
