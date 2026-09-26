"""Print Lumine's provider catalog as redacted JSON.

Mirrors the existing Tauri helper scripts (``livekit_token.py``,
``livekit_dispatch.py``): a tiny CLI the desktop layer can run to read backend
knowledge without ever importing Python into the app or exposing secrets.

Usage::

    python agent/provider_catalog.py            # catalog JSON on stdout
    python agent/provider_catalog.py --pretty   # indented, for humans
    python agent/provider_catalog.py --check    # exit 1 if the catalog is incoherent

The catalog is static, so this makes no network calls and needs no credentials.
"""

from __future__ import annotations

import argparse
import json
import sys

try:
    from .providers import CATALOG_VERSION, catalog_issues, to_public_catalog
except ImportError:  # running as `python agent/provider_catalog.py`
    from providers import CATALOG_VERSION, catalog_issues, to_public_catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="indent the JSON instead of emitting one compact line",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the catalog and exit non-zero if it is incoherent",
    )
    args = parser.parse_args(argv)

    if args.check:
        issues = catalog_issues()
        if issues:
            for issue in issues:
                print(f"catalog issue: {issue}", file=sys.stderr)
            return 1
        print(f"catalog ok (version {CATALOG_VERSION})")
        return 0

    payload = to_public_catalog()
    print(json.dumps(payload, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
