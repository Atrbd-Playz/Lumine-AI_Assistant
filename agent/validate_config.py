"""The configuration CLI for Lumine: describe and validate a configuration.

The settings UI needs two answers it cannot compute itself, and it cannot import
Python. This mirrors ``provider_catalog.py``: a tiny CLI the desktop layer runs,
so configuration knowledge stays in one place.

This is the configuration CLI. It answers two questions:

* **describe** - what is the configuration right now? Returns the effective
  document together with where it came from, so the settings screen can show the
  environment-derived profile and offer to customize it.
* **validate** (default) - could this document work? Returns graded diagnostics.

Usage::

    python agent/validate_config.py --describe
    python agent/validate_config.py path/to/lumine.config.json
    echo '{...}' | python agent/validate_config.py -

Validation output is always JSON::

    {"ok": false, "errorCount": 1, "warningCount": 0, "diagnostics": [...]}

``ok`` means there are no blocking errors, which is the condition for letting a
profile become active. No credentials are read: this checks shape and
compatibility, not whether a key is valid.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from .config_store import (
        CONFIG_PATH_ENV,
        config_path,
        config_path_candidates,
        effective_document,
    )
    from .validation import env_credential_status, validate_document
except ImportError:  # running as `python agent/validate_config.py`
    from config_store import (
        CONFIG_PATH_ENV,
        config_path,
        config_path_candidates,
        effective_document,
    )
    from validation import env_credential_status, validate_document


def _read(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8")


def validate_text(raw: str) -> dict[str, object]:
    """Validate a document body. Never raises; problems come back as diagnostics."""
    try:
        document = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return {
            "ok": False,
            "errorCount": 1,
            "warningCount": 0,
            "diagnostics": [
                {
                    "severity": "error",
                    "code": "document.unreadable",
                    "path": "",
                    "message": "The configuration is not valid JSON.",
                    "hint": str(exc),
                }
            ],
        }

    _load_environment()
    diagnostics = validate_document(document, env_credential_status())
    payload = [d.to_dict() for d in diagnostics]
    return {
        "ok": not any(d["severity"] == "error" for d in payload),
        "errorCount": sum(1 for d in payload if d["severity"] == "error"),
        "warningCount": sum(1 for d in payload if d["severity"] == "warn"),
        "diagnostics": payload,
    }


def _load_environment() -> None:
    """Load ``agent/.env`` so credential presence matches what the worker sees.

    Matches the other helper scripts. The contents are never emitted.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parent / ".env")


def describe() -> dict[str, object]:
    """The effective configuration and where it came from.

    ``source`` is ``ui`` when a saved document governs, otherwise ``env`` and the
    document is the read-only profile derived from ``agent/.env``.

    ``configPath`` is the file that was read, and ``searchedPaths`` every location
    considered. A user whose settings were saved but not applied can see here
    that the two sides looked in different places.
    """
    _load_environment()
    document, source, diagnostics = effective_document()
    return {
        "source": source,
        "configPath": str(config_path()),
        "searchedPaths": [str(candidate) for candidate in config_path_candidates()],
        "document": document,
        "diagnostics": [d.to_dict() for d in diagnostics],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--describe",
        action="store_true",
        help="print the effective configuration and its source instead of validating",
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="path to the configuration document, or - for stdin. Defaults to the active one.",
    )
    args = parser.parse_args(argv)

    if args.describe:
        print(json.dumps(describe(), separators=(",", ":")))
        return 0

    source = args.source
    if source is None:
        candidate = config_path()
        if not candidate.exists():
            print(json.dumps({"ok": True, "errorCount": 0, "warningCount": 0, "diagnostics": []}))
            return 0
        source = str(candidate)
    elif source == "-":
        source = "-"

    if source != "-":
        path = Path(source)
        if not path.exists():
            print(
                json.dumps(
                    {
                        "ok": True,
                        "errorCount": 0,
                        "warningCount": 0,
                        "diagnostics": [],
                    }
                )
            )
            return 0

    try:
        raw = _read(source)
    except OSError as exc:
        print(json.dumps({"ok": False, "errorCount": 1, "warningCount": 0,
                          "diagnostics": [{
                              "severity": "error", "code": "document.unreadable",
                              "path": "", "message": "Could not read the configuration.",
                              "hint": str(exc),
                          }]}))
        return 1

    print(json.dumps(validate_text(raw), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
