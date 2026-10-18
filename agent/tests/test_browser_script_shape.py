"""Every script the browser ships to the page must *parse*, and Python cannot see that.

The measured failure this guards is one line. `_JS_REGION_TEMPLATE` took ``budget``
as an arrow-function parameter and then declared ``const budget`` inside the same
scope:

```js
([selectors, fieldMap, budget]) => {
  const budget = Math.max(200, Number(budget) || 2400);
```

That is a ``SyntaxError``, and it is not a dramatic one. Playwright reports it as a
*failed evaluate* rather than as a broken script, so :func:`state.read_region` logged
``[browser] region read failed: Page.evaluate: SyntaxError: Identifier 'budget' has
already been declared`` and returned the empty answer it returns for a page that went
away.

Which is the whole reason this is a test rather than only a fix. **On every page, for
the entire life of the feature, the per-site map did nothing** and the reader fell
back to the generic ladder. Nothing raised to the model; the tool reported a normal,
shorter read. The only outward sign was five lines of noise in a *passing* test run.

The parsing itself is shared with ``test_messaging_script_shape.py`` through
`js_script_shape`, because the two packages are in the same business and the
messaging package has been bitten by this class of fault more often, not less. This
file names what is particular to the browser: which scripts discovery must keep
finding, and the one known fragment.
"""

from __future__ import annotations

import unittest

import agent.tools.browser as browser_package

from agent.tests import js_script_shape


class BrowserScriptShapeTests(js_script_shape.ScriptShapeSuite):
    """Parse every script `agent.tools.browser` hands to ``page.evaluate``."""

    PACKAGE = browser_package

    CRITICAL_SCRIPTS = (
        # The one that shipped broken.
        "state._JS_REGION_TEMPLATE",
        # Every observe and every act reads it before anything else.
        "state._JS_COLLECT",
        # The whole page snapshot, and the largest script in the package.
        "session._JS_OBSERVE",
        # Not named `_JS_*` -- the WhatsApp sign-in state read, and the one a
        # prefix scan misses. It is a script like the rest.
        "session._WA_STATE",
    )

    # `_JS_STALE` opens with a bare `const` and closes with a top-level `return`,
    # so it is legal only spliced into an arrow body. If the rule stopped
    # recognising that, the parse test would report it as a broken script and the
    # fix it pointed at ("fix _JS_STALE") would be the wrong one. The holder list
    # is measured from the source, not guessed: `_JS_SCROLL` opens with
    # `const root` and re-reads the generation itself, so it does not hold it.
    KNOWN_FRAGMENT = "session._JS_STALE"
    KNOWN_HOLDERS = (
        "session._JS_FACTS",
        "session._JS_CLICK",
        "session._JS_FILL",
        "session._JS_SELECT",
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
