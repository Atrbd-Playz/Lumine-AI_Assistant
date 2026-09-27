"""Print Lumine's tool registry as JSON, for the Tools page.

Mirrors ``provider_catalog.py``: a tiny CLI the desktop layer runs to read backend
knowledge without importing Python into the app. The registry in
``tools/tools_registry.py`` stays the single source of truth -- this file only
serialises what is already declared there, so a tool added in one place appears in
the app without a second edit.

Usage::

    python agent/tool_catalog.py            # compact JSON on stdout
    python agent/tool_catalog.py --pretty   # indented, for humans
    python agent/tool_catalog.py --check    # exit 1 if a tool has no card

Why this is not derived from the docstrings: a tool's docstring is the *model's*
schema. It argues for when to call the tool and how to paraphrase the result, in
the imperative, at length. Showing that on a settings card would be noise, and
parsing it would break the first time somebody reworded a sentence.
``TOOL_CARDS`` holds the person-facing description instead, and ``--check``
exists so the two cannot drift.

``enabled`` reflects the environment toggles the worker itself applies, which is
the point: a card that claimed a tool was available while
``LUMINE_DISABLED_TOOLS`` had switched it off would be a lie with a green tick on
it.

``categories`` is the ordered tab list for the Tools page, published rather than
hardcoded in the frontend for the same reason ``KeySlot`` descriptors come from
``providers.py``: the vocabulary of what Lumine can do belongs to the worker, and
a second copy in TypeScript is a second thing to forget to update. ``--check``
rejects a card whose category is not declared there, because the page derives its
tabs from this list and a tool in an undeclared category is a tool with nowhere to
appear.
"""

from __future__ import annotations

import argparse
import json
import sys

try:
    from .tools.tools_registry import (
        ALL_TOOLS,
        TOOL_CARDS,
        tool_cards,
        tool_category_list,
    )
    from .tools.tools_permission import tool_id
except ImportError:  # running as `python agent/tool_catalog.py`
    from tools.tools_registry import (
        ALL_TOOLS,
        TOOL_CARDS,
        tool_cards,
        tool_category_list,
    )
    from tools.tools_permission import tool_id


def catalog_issues() -> list[str]:
    """Ways the registry and the cards can disagree.

    A pure function so ``test_tool_catalog.py`` can assert the two are in step
    without a filesystem or a subprocess.
    """
    issues: list[str] = []
    registered = {tool_id(tool) for tool in ALL_TOOLS}
    declared_categories = {entry["id"] for entry in tool_category_list()}

    for missing in sorted(registered - set(TOOL_CARDS)):
        # The failure this prevents is a tool that works but has no card, so it
        # never appears in the app and its existence has to be read from source.
        issues.append(f"{missing}: registered but has no card")

    for orphan in sorted(set(TOOL_CARDS) - registered):
        # The other direction: a card for a tool that no longer exists is a page
        # offering something that cannot be called.
        issues.append(f"{orphan}: has a card but is not registered")

    for ident, card in sorted(TOOL_CARDS.items()):
        if not str(card.get("label", "")).strip():
            issues.append(f"{ident}: card has no label")
        if card.get("effect") not in ("answers", "opens"):
            # A card that does not say whether a tool acts on the machine leaves
            # the reader to guess from its name, which is the decision they are
            # being asked to make by leaving it switched on.
            issues.append(f"{ident}: card effect must be 'answers' or 'opens'")
        if card.get("category") not in declared_categories:
            # A category the frontend has no strip for. The page derives its tabs
            # from the published list rather than hardcoding names, so an
            # undeclared category does not error -- it hides the tool behind
            # nothing, which is the reason this is an issue rather than a shrug.
            issues.append(
                f"{ident}: category {card.get('category')!r} is not in TOOL_CATEGORIES"
            )
        for parameter in card.get("parameters", []):
            if not str(parameter.get("name", "")).strip():
                issues.append(f"{ident}: a parameter has no name")
            if not str(parameter.get("help", "")).strip():
                issues.append(f"{ident}: parameter {parameter.get('name')} has no help")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true", help="indent the JSON")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate that every registered tool has a card, and exit non-zero if not",
    )
    args = parser.parse_args(argv)

    if args.check:
        issues = catalog_issues()
        if issues:
            for issue in issues:
                print(f"tool catalog issue: {issue}", file=sys.stderr)
            return 1
        print(f"tool catalog ok ({len(ALL_TOOLS)} tools)")
        return 0

    payload = {"version": 2, "categories": tool_category_list(), "tools": tool_cards()}
    print(
        json.dumps(
            payload,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
