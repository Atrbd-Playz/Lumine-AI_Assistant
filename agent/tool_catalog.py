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
from collections.abc import Mapping

try:
    from .settings.config_store import effective_document
    from .tools.tools_registry import (
        ALL_TOOLS,
        TOOL_CARDS,
        tool_cards,
        tool_category_list,
    )
    from .tools.tools_permission import TOOL_ACTION_CLASS, TOOLS_SECTION, tool_id
except ImportError:  # running as `python agent/tool_catalog.py`
    from settings.config_store import effective_document
    from tools.tools_registry import (
        ALL_TOOLS,
        TOOL_CARDS,
        tool_cards,
        tool_category_list,
    )
    from tools.tools_permission import TOOL_ACTION_CLASS, TOOLS_SECTION, tool_id


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
        if card.get("effect") not in ("answers", "opens", "acts", "sends"):
            # A card that does not say whether a tool acts on the machine leaves
            # the reader to guess from its name, which is the decision they are
            # being asked to make by leaving it switched on. "acts" reaches a
            # web page, "sends" leaves the conversation entirely -- both are
            # distinctions a reader needs before saying yes.
            issues.append(f"{ident}: card effect must be one of 'answers', 'opens', 'acts', 'sends'")
        if ident not in TOOL_ACTION_CLASS:
            # The floor a tool asks from comes from this map, and a tool missing
            # from it falls through to the *strictest* class -- so nothing unsafe
            # happens. What does happen is a card that says "Asks you first"
            # about a lookup, and a policy page that cannot be trusted to
            # describe itself. The class is declared next to the tool on purpose;
            # this makes forgetting it a failing check instead of a wrong label.
            issues.append(f"{ident}: no action class declared")
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

    # Switches written into the saved document for tools that do not exist.
    # Checked here rather than in `settings/validation.py` because this is the
    # only place that knows the tool list without importing the registry into
    # settings -- which would be a cycle, since the registry reaches the
    # permission gate and that reaches settings.
    for stale in sorted(_switch_ids() - registered):
        issues.append(
            f"{TOOLS_SECTION}.{stale}: the saved document has a switch for a tool "
            "that is not registered, so it does nothing"
        )
    return issues


def _switch_ids() -> set[str]:
    """Tool ids the saved document carries a switch for.

    Defensive on purpose: this function exists so ``--check`` reports a
    stale switch rather than refusing to start because of one, and an unreadable
    or half-written config file is exactly when somebody needs that report.
    """
    try:
        document, _source, _diagnostics = effective_document()
    except Exception:  # noqa: BLE001 - an unreadable file is not a crash here
        return set()
    section = document.get(TOOLS_SECTION) if isinstance(document, Mapping) else None
    if not isinstance(section, Mapping):
        return set()
    return {str(key).strip().lower() for key in section if str(key).strip()}


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
