# Browser Control Subsystem — Test Plan

Status: the page-awareness slice is verified below; the additional phases remain
proposed. Each proposed phase must pass its rows before its gate in `ROADMAP.md`
is closed.

## Principles

1. **Deterministic local fixtures only.** A loopback HTTP server, saved HTML
   documents served from it, and `LUMINE_BROWSER_ALLOW_LOCAL=1` — the pattern
   `test_browser_search_real.py` already established. No third-party site is ever
   required to pass a test; `test_browser_search_real.py`-style live probes stay
   manual and opt-in.
2. **The suite command never changes.** `python -m unittest discover -s agent\tests
   -p "test_*.py"` from the repository root, and it stays green every phase —
   2,426 tests, 3 skipped at the Phase 0 baseline, plus whatever each phase adds.
3. **A changed assertion carries a reason.** When an existing test's expression
   changes because a mechanism changed (only `test_browser_lifecycle.py:324` is
   expected to), the new assertion cites `MIGRATION.md`'s supersession entry in a
   comment next to it.
4. **The fixture server is one module.** `agent/tests/browser_fixture_server.py`
   (new, test-only): serves in-memory routes, a download endpoint emitting any
   content-type and disposition, an upload form, a shadow-DOM page, a multi-frame
   page, a scroll-reflow page (a button that grows the document above a target),
   and a page whose layout depends on viewport size. Tests compose it; no test
   writes its own server.

## Baselines carried forward

| Suite | Count at Phase 0 | Rule |
| --- | --- | --- |
| Browser subset (`test_browser*.py`) | 457 | grows every phase |
| Full suite | 2,426 (3 skipped) | green at every phase gate |
| WhatsApp safety (`test_whatsapp_safety.py`) | passing | green, and *strengthened* in Phase 5 |
| Tool catalog (`tool_catalog.py --check`) | passing | passing after any tool-docstring change |

## Delivered verification — 2026-10-18

### Deterministic coverage

- Browser subset: **498 tests passed** after the live-site fix.
- Full agent suite: **2,436 tests passed, 3 skipped**.
- `agent/tool_catalog.py --check`: passed (`17 tools`).
- The Amazon-like fixture inserts 24 generic navigation list items before the
  products and still asserts that product/video cards, prices, and their grounded
  controls are observed.

### Public-site smoke through Lumine's browser code

The smoke used the repository's `browse`/`session` Playwright implementation in a
new tab, waited for each page to settle, and closed the test tab afterward. It
used public search pages only: no login, personal-data submission, cart action,
or purchase.

| Site | Observed and acted on | Verification |
| --- | --- | --- |
| YouTube | Searched `lofi music`; observed `1 A.M Study Session 📚 [lofi hip hop]` with a grounded link. | Clicking the observed reference reached `https://www.youtube.com/watch?v=lTRiuFIWV54&list=RDlTRiuFIWV54&start_radio=1`; the watch-page title matched the selected video. |
| Amazon | Typed `bluetooth earbuds` into the observed `Search Amazon` control and pressed Enter; the fresh page was titled `Amazon.com : bluetooth earbuds` and showed matching product titles/prices. A separate `wireless headphones` result read included `Hybrid Active Noise Cancelling Bluetooth Headphones` at `$32.99`. | Chose `Price: Low to High`; the fresh snapshot reported that selection, the URL included `s=price-asc-rank`, and the first results were ordered from `$5.66`, `$5.99`, `$5.99`, `$6.49`, `$6.55`. |

The first live Amazon observation exposed a real bug: generic list entries used
the bounded item budget before the product cards. Explicit site/card selectors
were moved ahead of generic list selectors, then the fixture and live smoke were
rerun successfully. On the Sony product detail page, Amazon displayed a
"add to cart to see price" message; no cart action was taken, so that item's
detail-page price was not verified. YouTube's navigation/title transition was
verified; uninterrupted media playback was not an acceptance check.

## Personal browser engine and name-based tabs

The browser lifecycle tests cover saved/environment engine precedence, automatic
Brave discovery, clear refusal when explicitly selected Brave is unavailable,
resolving the native profile named `Lumine`, and passing its on-disk profile
directory into the launcher. Endpoint files are isolated by engine and owner
teardown only removes the matching endpoint. They also assert the Brave launch restores
component updates, background networking, and extensions, while Chromium keeps
Playwright's default arguments. The media DNS test exercises two consecutive
transient failures before recovery.
The UI setting persists
`desktop.browsers.personalEngine`; switching takes effect only after the existing
Lumine browser is closed. Tab tests cover selecting and closing by page title or
site, asking when a name is ambiguous, and giving a useful not-found response.
These are local tests: ad delivery and uninterrupted playback on YouTube are not
asserted, and Brave Shields cannot guarantee either. Lumine no longer ships or
applies a custom ad filter; Brave Shields handles ad blocking.

## Phase 1 — engine completion

**Unit**

- `errors.py`: every `ErrorKind` member is raised by at least one site; every
  engine/session failure path maps to exactly one kind; no bare `EngineError`
  without a kind survives (grep-asserted in the test).
- Shadow piercing: a page with controls nested three shadow roots deep is
  collected, refed, matched by role+name, and clicked; a **closed** shadow root is
  not pierced and the control inside is absent from the snapshot (the consent
  boundary, asserted both ways); the depth cap refuses at its constant with a
  `not_found` rather than recursing forever.
- Frame refs: the multi-frame fixture yields per-frame refs; `browse act` with
  `frame=1` presses a control in the second frame; a ref from frame 0 used after
  the frame tree changes resolves to `detached`, never to the wrong frame's
  control.

**Integration**

- Recovery: kill the owned browser mid-observe → `crashed`, not a hang and not a
  generic timeout; a guest process (joined over CDP, endpoint published by
  another) whose connection drops attempts **rejoin only** — asserted by a
  monkeypatched launch that fails the test if a guest ever calls it; after a
  successful recovery every outstanding ref is stale (generation bumped).
- The host-browser invariant: recovery in a guest leaves the host's pages open and
  unminimized — the WhatsApp-tab-preservation assertion style from
  `test_whatsapp_safety.py` is reused.

**Regression watch**

- All 457 existing browser tests unchanged and green; the shadow-descent change to
  `state.py`'s collector must not alter snapshots of shadow-free pages byte-for-byte
  (a fixture diff test).

## Phase 2 — contract and schema

**Unit**

- Verb enum: for every verb that parses today (extracted from the current parsers'
  accepted sets), the enum accepts it — a corpus test, so the enum cannot silently
  narrow the contract; an unknown verb returns a classified parse refusal naming
  the valid set.
- DDG parser: the fixture corpus serves ≥4 DuckDuckGo HTML variants (normal,
  no-results, CAPTCHA/interstitial, malformed) — `search.py` extracts results from
  the first two, refuses the last two by name; both `web_search.py` and
  `search.py` run over the same corpus and agree on the extracted tuples.
- Model-generated call corpus: recorded, sanitized tool-call JSON replayed through
  `browse`, `web_control`, and `search_web` argument parsers; every case produces
  a valid call or a classified refusal — never a `KeyError`, `IndexError`, or
  silent mis-parse (parametrised over the corpus files).

**Integration**

- `tool_catalog.py --check` passes; the Tools page accent test still counts
  `sends` as the minority (the docstring additions must not add a `sends` verb).

## Phase 3 — intervention and replan

**Unit**

- Task model: `awaiting_intervention` is a legal state with legal transitions
  (enter from `running`, exit to `running` on resume, to `failed` on timeout);
  the state table stays a total `Record`-equivalent (a new state fails the build
  if a transition map is not exhaustive — mirroring the `LumineState` pattern).
- Intervention hold is bounded: a held task that receives no release signal within
  the timeout fails with a classified reason, and the hold publishes exactly one
  `lumine.tool` step record naming what is being waited on.

**Integration**

- Resume: a fixture page that requires a button press *after* the hold (simulating
  a login) — the engine re-observes before resuming and does not act on the
  pre-hold snapshot (the page swapped its controls during the hold; acting on the
  old refs must fail as `stale`, and the resumed run must succeed on fresh refs).
- Replan: a deterministic step failing with `refused` regenerates the plan via
  `plan_for` with the failure context, exactly once — a scenario where every plan
  contains the same refused step must terminate with `failed` and must not loop
  (a call-count assertion on the planner).

## Phase 4 — coordinate clicks

**Unit — the freshness matrix.** Each row must produce a `stale` refusal, never a
dispatch, driven against the scroll-reflow and viewport-dependent fixture pages:

| Perturbation between screenshot and click | Must refuse |
| --- | --- |
| Window scrolled (any offset) | yes |
| Viewport resized (w or h) | yes |
| Device pixel ratio changed | yes |
| Layout reflowed (element count / scrollHeight changed) | yes |
| Document navigated (generation moved) | yes |
| URL changed under a same-generation SPA | yes |
| Token older than `COORDINATE_MAX_AGE` | yes |
| Nothing changed, age fresh | **clicks** — the positive case, asserted to press the right element |

**Unit — the safety floor**

- A coordinate over a password field refuses with the *structural* password reason
  (checked before the token), and no approval unlocks it.
- A confident role+name ref never takes the coordinate path (an interception
  assertion on the locator ladder).

**Integration**

- Dispatch reports the resolved element's role+name in the result, and the audit
  record names the element, not only the pixel.

## Phase 5 — transfers

**Unit**

- Allowlist: path traversal (`..`), absolute paths outside the root, and a
  symlink pointing out of the root all refuse — resolved-then-compared, the same
  three cases `whatsapp_files.py` pins, driven through the shared implementation
  (one boundary, one test shape).
- Extension and size: an executable, a script, an archive, and an oversized file
  all refuse with their own reasons.
- Token: a `Download` matching the pending token on tab and filename is saved to
  the allowlisted destination; a `Download` on the wrong tab, wrong filename, or
  with no pending token is **discarded** (the temp artifact deleted, asserted via
  the fixture server's download endpoint).

**Integration — the shared-browser guarantees (Safeguard A)**

- `accept_downloads is True` **and** the interceptor is registered — the rewritten
  `test_browser_lifecycle.py:324`, citing `MIGRATION.md`.
- A download triggered **from the WhatsApp tab** mid-conversation is discarded and
  never saved; the WhatsApp conversation's pages stay open and unminimized
  (the existing `test_whatsapp_safety.py` assertions reused).
- `whatsapp_files.py` still refuses URL attachments, and its refusal reason text
  cites the interceptor (a string-assert on the new reason, so the stale
  flag-based rationale cannot come back).
- Upload: `browse upload` with an allowlisted file succeeds on the fixture upload
  form; outside the root, refuses before `set_input_files` is ever reached
  (monkeypatched to fail the test if called).
- Permission class: an upload with no grant raises a permission request
  (`sensitive`), matching the WhatsApp attach path's treatment.

**Security**

- A hostile page that initiates a download with no user request and no token:
  discarded, temp cleaned, no file in any user path (fixture: a page that
  auto-clicks a download link on load).
- A download whose `suggested_filename` tries to escape (path separators, `..` in
  the name) lands as a sanitized basename inside the root or is discarded.

## Phase 6 — activity records

**Unit**

- `browser_activity` records carry verb, target, outcome kind, duration; the
  record type is additive — `voice-manager.ts`'s existing type check ignores it
  (a frontend fixture test: an unknown record type produces no state change).
- Record volume: one record per action verb, none for pure reads unless the verb
  is `observe` with a changed document (no telemetry flood; a cap test).

**Integration**

- The fixture flow (open → act → act) yields a browsable activity list in
  `BrowserPreview.tsx` with the current URL and tab count, driven by the same
  records the model saw.

## Commands

```powershell
# full suite, from the repository root — the gate for every phase
.\.venv\Scripts\python.exe -m unittest discover -s agent\tests -p "test_*.py"

# browser subset during development
.\.venv\Scripts\python.exe -m unittest discover -s agent\tests -p "test_browser*.py"

# tool catalog, after any docstring/verb change
.\.venv\Scripts\python.exe agent\tool_catalog.py --check

# frontend, after Phase 6
cd lumine-ui; npm run build
```
