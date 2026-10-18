# Browser Control Subsystem — Target Architecture

Status: mixed. The page-observation and grounding slice below is implemented; the
six additional capability phases remain proposed. Nothing here deletes an
existing module — the track is *evolve in place* (see `MIGRATION.md`).

## Design principles

1. **The seams stay.** `Engine`, `SnapshotStore`, the ref+generation identity
   model, `parse_action`, the permission gate, the task engine, the verification
   ladder, `progress.py`, and the `lumine.browser` topic are load-bearing and
   unchanged in shape. New capability plugs into them.
2. **One authority per vocabulary.** Verb names, action classes, and error kinds
   each get exactly one module that defines them; every parser imports rather than
   restates. (Gap 10.)
3. **A refusal names its cause and its remedy.** Every new failure path returns a
   classified `kind` plus a sentence the model can act on — retry, re-observe, or
   escalate. (Gap 4.)
4. **The shared browser is sacred.** Any change to context-level flags must be
   proven not to weaken the WhatsApp path, with a test, not an assertion in prose.
5. **Local fixtures, never third-party sites**, in every new test — the pattern
   `test_browser_search_real.py` already established with a loopback server.

## Delivered slice — Observe → Ground → Act → Verify

The production observer shared by `browse` and `web_control` is
`state.py::_JS_COLLECT`. One observation carries visible controls and bounded
structured page items. Video and product cards retain their title, visible text,
safe destination URL, price when present, and the reference of the card's primary
link. Site-identified card selectors run before generic list containers so a
large site's navigation rows cannot consume the item limit.

`locators.py` can resolve a named link against the title of its containing item;
`session.py` grounds controls and exposes native select options and the current
selection. `browse`'s select action uses the same fresh reference and verifies
the selected result in a later observation. The existing route guard, permission
classes, password refusal, untrusted-page wrapping, tab ceiling, and shared
browser ownership rules remain in force.

This slice does not implement shadow-DOM traversal, frame refs for `browse`,
crash recovery, coordinate actions, file transfers, task intervention/replan, or
an activity panel. Those are the proposed phases below, not claims about current
behavior. Live-site evidence and limitations are recorded in `TEST_PLAN.md`.

Search results are included in the search tool's bounded report, so a follow-up
can act on a title already present rather than repeat the search. Submitting the
same query while its results URL is still open reuses that page. The isolated
browser launches at 960×680 with a dark appearance and background media
suspension disabled; these launch defaults apply when Lumine owns a new browser,
not when it joins an already-running Chromium process.

## Component map (what grows, what is added)

```text
agent/tools/browser/
  errors.py        [NEW]   ErrorKind enum + EngineError subclasses — one taxonomy
  transfers.py     [NEW]   Upload/download allowlist logic (Phase 5)
  engine.py        [GROW]  kind-carrying errors, browser-restart hook (Phase 1)
  locators.py      [GROW]  shadow-DOM piercing, coordinate path behind a token (P1/P4)
  state.py         [GROW]  shadow-aware collection, frame-scoped registries (Phase 1)
  session.py       [GROW]  frame refs for browse, crash detection, recovery entry (P1)
  actions.py       [GROW]  imports errors.py kinds; upload/download verbs (Phase 5)
  control.py       [GROW]  same, for web_control (Phase 5)
  tools.py         [GROW]  docstring updates for new verbs (Phases 4/5)
  search.py        [GROW]  loopback-fixture-tested parser, fallback chain (Phase 2)
  preview.py       [GROW]  activity record type alongside browser_preview (Phase 6)
agent/runtime/
  task_model.py    [GROW]  awaiting_intervention state + resume signal (Phase 3)
  task_engine.py   [GROW]  intervention hold/release, replan trigger on classified
                           engine failure (Phase 3)
agent/tools/task_runner.py [GROW] plan_for: replan-on-failure scenario (Phase 3)
lumine-ui/src/pages/home/components/BrowserPreview.tsx [GROW] activity list (Phase 6)
```

No module is removed. No tool is renamed. No existing verb changes meaning.

## Phase 1 — engine completion

### `errors.py`: one taxonomy

`EngineError` already carries `kind: str | None`. The taxonomy becomes a real enum
and every raise site in `engine.py`, `session.py`, `actions.py` maps to it:

| `ErrorKind` | Meaning | Model's correct reaction |
| --- | --- | --- |
| `timeout` | navigation or action exceeded its budget | re-observe, then decide |
| `not_found` | ref or selector absent from the live document | re-observe (generation may have moved) |
| `ambiguous` | several candidates, none confident | choose from candidates or ask |
| `stale` | ref's generation no longer matches the document | re-observe |
| `refused` | route guard, password field, structural refusal | do not retry; change approach |
| `detached` | frame or element gone mid-action | re-observe |
| `crashed` | browser or page process died | escalate to task engine (replan) |
| `busy` | tab cap, lock held, WhatsApp busy | wait or close a tab |

`tool_results.wrap_untrusted` still wraps the payload; the `kind` rides as a
top-level key so the model reads it before the prose. Nothing about the gate,
the audit, or the verification ladder changes: a classified failure is still a
`failed`/`uncertain` verification state — the taxonomy tells the *model* what to
do, it does not upgrade any outcome claim.

### Shadow-DOM piercing

`state.py`'s collection walks `shadowRoot` recursively at every host it meets, with
a depth cap (default 10, constant, not a setting). `locators.py`'s role ladder
queries through open shadow roots — Playwright's `locator` engine already pierces
closed-open boundaries for CSS; the accessibility-role walk gains an explicit
descent so role+name matching sees shadowed controls. Refs minted inside a shadow
root record their host chain, so a re-resolve after a re-render walks the same
path. **Closed** shadow roots are not pierced — that is the platform's consent
boundary, same as the desktop UIA tree refusing a protected control.

### Frame refs for `browse`

`state.py`'s element registry becomes frame-aware: a ref's identity is
`(tab, generation, frame_path, element_key)`. `browse observe` reports frames as
first-class entries (`frame: <n>, name, url`) and `browse act` accepts `frame=` the
way `web_control` already does. The frame path is a stable ordinal per document
(index into the frame tree, re-walked on resolve), not a frame handle — a handle
dies on navigation, an ordinal re-derives.

### Crash and restart recovery

The invariant that constrains everything here: **a guest never stops or relaunches
the host browser.** `_close_remote(shared=True)` is the existing expression of it.

- `engine.py` classifies Playwright's disconnect errors as `crashed` instead of
  retrying the operation against a dead connection.
- Session-level recovery is *rejoin*: read `browser-cdp.txt`, `connect_over_cdp`
  again, rebuild the tab registry from the live pages. If the endpoint is gone and
  this process was the **owner** (the `_OwnerLock` holder), relaunch is allowed —
  a guest never relaunches.
- Recovery mints a new generation for every tab: after a restart no outstanding
  ref can be trusted, and pretending otherwise is how a stale click survives a
  crash.
- A failed recovery surfaces `crashed` to the task engine, which owns the replan
  decision (Phase 3). The engine never loops on restart.

## Phase 2 — contract and schema hardening

- **One verb enum.** `actions.py` gains `VERBS` as the single source;
  `tools.py`, `control.py`, and `sites.py` validate against it. An unknown verb is
  a `not_found`-classed parse refusal naming the valid set, not a silent no-op.
- **DDG parser hardening.** A local loopback fixture serves saved DuckDuckGo HTML
  (several variants, including a no-results page and a CAPTCHA page); `search.py`
  gains a fallback chain — `html` endpoint parser → generic `result`-class parser →
  a refusal that names the page state. `web_search.py`'s parser shares the same
  fixture corpus; the two parsers already differ by transport and must not drift
  by markup interpretation.
- **Model-generated call tests.** A fixture corpus of realistic tool-call JSON
  (from recorded sessions, sanitized) is replayed through each tool's argument
  parser and must produce either a valid call or a classified refusal — never a
  `KeyError`, never a silent mis-parse.

## Phase 3 — intervention and replan

- **`awaiting_intervention`** joins the task states. A step may hold the task in
  it when the failure is one a human resolves: a login wall, a CAPTCHA, a declined
  approval the user then grants, a download needing a destination choice. Holding
  is bounded by the same 60 s default the gate uses, and holding publishes a
  `lumine.tool` step record so the UI can say what Lumine is waiting for.
- **Resume detection.** Intervention release is a signal (an authenticated final
  transcript, the same channel approvals use), and the engine re-observes before
  resuming — never trusts the pre-hold snapshot.
- **Replan triggers.** `task_runner.plan_for` gains a replan scenario: when a
  deterministic step fails with `crashed` or `refused`, the plan is regenerated
  with the failure context, bounded by a replan budget (one replan per task by
  default) so a deterministic failure cannot loop through the planner forever.

## Phase 4 — coordinate clicks behind a freshness token

Supersedes the `locators.py` refusal, in writing (see `MIGRATION.md`). The rule
that makes it safe is that **a generation number alone is not freshness.**

A generation mints on navigation and SPA body-swap. It does *not* mint when the
page scrolls, the window resizes, a lazy image loads and reflows the layout, a
sticky header appears, or the device pixel ratio changes. All of those move pixels
under a screenshot while the generation stays put. So the coordinate token binds
to a **viewport signature**, captured at screenshot time and re-verified
immediately before dispatch:

```text
token = {
  tab,                      # which tab the screenshot came from
  generation,               # document identity (existing model)
  url,                      # belt: a same-generation URL change is still a refusal
  viewport: { w, h, dpr },  # CSS pixels and device pixel ratio
  scroll: { x, y },         # window scroll offset at capture
  layout: { scrollW, scrollH, elementCount },  # cheap document shape digest
  captured_at,              # monotonic clock
}
```

Dispatch requires **every** field to match the live document, checked in the same
tick as the click:

- any mismatch → `stale` refusal: "the page moved since that screenshot — take
  another one", never a click at adjusted coordinates;
- a token older than `COORDINATE_MAX_AGE` (default 15 s) → `stale`, because a slow
  model turn can outlive a live page even with identical dimensions;
- the click resolves the element under those coordinates *in the live DOM* and
  reports its role+name in the result, so the user and the audit both see what was
  actually pressed;
- the coordinate path is only ever a fallback when the role+name ladder found a
  single **unconfident** candidate — a confident ref still takes the ref path, and
  no coordinate can reach a password field, because that refusal is structural and
  checked before the token.

Coordinates are per-tab and never cached across observes: a token is minted by one
`screenshot`/`observe` and dies with the next document, the next scroll, or its own
age. Whichever is checked first.

## Phase 5 — transfers

Supersedes the `accept_downloads=False` guard, in writing — **with the
WhatsApp-preservation safeguard**, which is the load-bearing part.

### The blast radius, named

`accept_downloads` is a **context-level** flag, and the context is shared with the
WhatsApp path over CDP. `whatsapp_files.py` currently refuses URL attachments with
the stated reason "downloading would mean lifting `accept_downloads=False`". That
sentence stops being true the moment the flag changes, and the guard it was
standing in for must be rebuilt where the flag was.

### Safeguard A — downloads preserve the WhatsApp shared-browser guarantees

The guarantee WhatsApp actually relies on is not the flag; it is **no file lands on
this machine unless Lumine deliberately put it there.** The flag was one expression
of it. With `accept_downloads=True`, Playwright holds every download as a `Download`
object in its own temp area; nothing reaches a user path until `save_as` is called,
and the object can be discarded. So the guarantee is rebuilt as an interceptor:

1. **Discard by default.** A context-level download handler deletes every download
   the instant it appears, unless a *pending download token* claims it. A page
   initiated download that nobody asked for — including in the WhatsApp tab,
   including mid-conversation — is discarded and never saved. This is stricter than
   the old flag in one way (a download can no longer be silently refused at the
   network layer *and* the temp artifact is bounded and cleaned) and equivalent in
   the way that matters (nothing lands on a user path unvalidated).
2. **A claim is a token, minted by the tool.** `browse download` (and only it)
   mints a single-use token bound to (tab, the exact URL pattern the user asked
   for, the allowlisted destination root) immediately before triggering the
   navigation or click that produces the download. The interceptor matches the
   incoming `Download` against the pending token — tab and suggested filename
   checked — and discards on any miss. Tokens expire in seconds.
3. **The destination is allowlisted, resolved-then-compared**, exactly like
   `whatsapp_files.py`: a dedicated downloads root, `Path.resolve()` on both sides,
   extension allowlist (no executables, no scripts, no archives), size cap. The
   resolved path lands inside the root or the download is deleted, not saved.
4. **Nothing auto-opens.** A saved download is reported by path; no `startfile`,
   no attachment to any conversation, no read-back into the model's context beyond
   name and size.
5. **The WhatsApp attach path is untouched and re-proven.** `whatsapp_files.py`
   keeps refusing URLs — its reason text is rewritten to cite the interceptor
   rather than the flag, and `test_whatsapp_safety.py` gains a case: a download
   triggered *from the WhatsApp tab* is discarded, never saved. The existing
   tests that pin the shared-browser contract (`test_whatsapp_safety.py`,
   `test_browser_lifecycle.py`) are updated deliberately, with the reason written
   next to the changed assertion — `test_browser_lifecycle.py:324`'s
   `accept_downloads is False` becomes an assertion about the interceptor being
   registered, because the interceptor is what the guarantee now rests on.

### Uploads

`browse upload <ref> <path>` and `web_control upload` take a path through the same
allowlist shape: resolved-then-compared against configured roots, extension
allowlist, size cap — the `whatsapp_files.py` module is imported, not reimplemented
(gap: one boundary, one implementation). The path never reaches the model's result
beyond its name; the permission class is `sensitive`, so an upload asks unless
granted, matching `set_input_files`'s existing treatment on the WhatsApp path.

### Why the whole of Phase 5 is worth its blast radius

Both halves were approved as supersessions of recorded refusals. The alternative —
keeping the refusals — means a user asking "download this file" or "upload this
photo to that form" gets a hard no from a browser that can otherwise drive the
entire page. The safeguards above are the price of the approval, and each has a
test that fails if it is weakened.

## Phase 6 — activity records

- `preview.py` publishes a second record type, `browser_activity`, on the same
  `lumine.browser` topic: verb, target (ref or URL), classified outcome kind, and
  duration, one per `act`/`open`/`download`/`upload`. `voice-manager.ts` already
  type-checks incoming records, so an unknown type is ignored by old frontends —
  additive by construction.
- `BrowserPreview.tsx` renders the recent activity list beside the screenshot,
  capped at a small constant, and shows the current URL and tab count from the
  same records. No new topic, no new permission surface.

## What this architecture deliberately does not build

- No model-authored JavaScript — `execute` stays absent from `browse`.
- No route-guard relaxation — loopback stays blocked without
  `LUMINE_BROWSER_ALLOW_LOCAL`; that flag remains the *only* way local fixtures
  run.
- No new browser, no second context, no per-agent browser. The singleton stands.
- No LLM planner in this track — replan calls the existing `plan_for`, which stays
  deterministic; the planner upgrade is runtime-track Phase 8, out of scope here.
- No removal of `whatsapp_desktop.py`, `site_map.py`, or any module the audit
  found mature — maturity is not obsolescence.
