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
resolving the native profile named `Lumine`, passing its on-disk profile
directory into the launcher, and falling back to a spare user-data directory
when that profile is held. A held directory is re-checked for about a second and
a half before that fallback, so a lock Chromium is still releasing keeps the
personal profile; a lock somebody holds does not clear and the spare is used.
Endpoint files are isolated by engine and owner
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

## Background media acceptance — measured on a retired profile

These runs were made against a separate, Lumine-owned Brave profile that the
product no longer uses; browser tools now open the personal Brave `Lumine`
profile, where playback and ad blocking are reported working. Keep the table as
a manual checklist for background playback rather than as a description of a
shipped failure. The evidence below is retained because it records what was
measured, and because a future profile change should be re-tested against it.

| # | Scenario | Procedure and pass condition |
|---|---|---|
| 1 | Foreground, video and audio | Play for at least 10 minutes with the browser visible. Playback time advances for the full interval without an unintentional pause or error. |
| 2 | Minimized | Repeat while the browser is minimized for 10 minutes; restore it and record whether media time advanced continuously. |
| 3 | Occluded | Repeat with another application fully covering the browser for 10 minutes; return and record media time and playback state. |
| 4 | Tab switch | Play in one tab, switch to another tab for 10 minutes, then inspect the original tab. |
| 5 | Multiple tabs | Keep several tabs open while the video plays for 10 minutes; verify the video tab was not discarded and media time advanced. |
| 6 | Unrelated automation | Run a bounded browser action in a different tab while the video plays; verify the original media remains uninterrupted. |
| 7 | CDP reconnect | Detach and reconnect the automation client without closing the browser; verify playback and media time continue. |
| 8 | Recovery | Interrupt the automation connection, recover it, and verify there is no duplicate video tab and no playback restart. |
| 9 | Browser restart | Close and reopen the dedicated browser for at least three sessions; record each playback duration and verify no stale endpoint prevents startup. |
| 10 | Persistent profile | Sign in in the dedicated profile, restart the browser, then verify the sign-in remains and the video plays for 10 minutes. |

Run the foreground/minimized/occluded cases against personal Brave, the prior
Lumine runtime when available, and the independent Lumine profile. Test an
audio-focused YouTube session as well as a video session. On Windows, record
Brave version, GPU/hardware-acceleration setting, codec shown by YouTube Stats
for nerds, power mode, and any relevant Media Foundation/GPU errors. Keep the
network constant and record the time of any network interruption. Until these
actual-browser runs are recorded, background-playback compatibility is **not
verified**; the existing DNS and launch-argument unit tests are not evidence of
10-minute playback.

### Live playback investigation — 2026-10-30

One foreground attempt was made in Lumine's independent Brave profile
(Brave `156.0.8078.74`), not personal Brave. The public, audio-and-video
`1 A.M Study Session` video loaded and advanced briefly (`readyState=4`,
854×480), then failed within the first minute. The failure state was
`paused=true`, `currentTime=0`, `duration=NaN`, `readyState=0`,
`networkState=0`, no `MediaError`, `visibilityState=visible`, and
`wasDiscarded=false`; the page showed YouTube's generic playback error. The
Brave process and local CDP endpoint remained available. An explicit Play-button
retry did not recover playback.

A reload with CDP network events enabled recorded repeated
`googlevideo.com` `/videoplayback` Fetch failures as
`net::ERR_BLOCKED_BY_CLIENT`; no HTTP error response or DNS-resolution failure
was observed. YouTube logging/advertising requests were also blocked by the
client. This is browser-side request blocking, strongly consistent with Brave's
default Shields in the fresh dedicated profile; it is not evidence of a DNS,
decoder, tab-discard, visibility, or browser-crash failure. The exact internal
filter responsible has not been isolated, so the finding is not attributed to
Shields with certainty in this initial sample; no security setting was changed
for it. The unrelated blank-tab path is a separate CDP/startup lifecycle issue.

### Site-only Shields comparison — 2026-10-30

A second foreground run used the public `Big Buck Bunny 60fps 4K` video in the
same dedicated Brave profile. YouTube-only Shields was set to **Allow all
trackers & ads** for this comparison; no global Shields setting was changed.
Playback advanced to `currentTime=32.5` with `readyState=4` and an 854×480
video. At 45.2 seconds the video had stopped: `paused=true`, `currentTime=0`,
`duration=NaN`, `readyState=0`, `networkState=0`, no `MediaError`, visible
document, and `wasDiscarded=false`. YouTube showed its generic playback error.
The page was observed passively for the full 90 seconds; playback did not
recover, and no play/reload action was issued.

The sanitized CDP record contained six `googlevideo.com` `/videoplayback`
Fetch responses with HTTP 403. The browser process and CDP endpoint remained
healthy. This differs from the earlier `ERR_BLOCKED_BY_CLIENT` sample: allowing
YouTube trackers and ads did **not** make playback reliable, and this result
does not establish that Shields is the sole cause. It does rule out a
10-minute pass and leaves the server/CDN rejection unexplained. After the run,
the dedicated profile's YouTube setting was restored to **Block trackers &
ads** and verified in the Shields panel; personal Brave and global settings
were not changed.

To isolate the client-blocked requests, the exact same `aqz-KE-bpKQ` video was
then replayed in the same foreground window with standard YouTube Shields
restored. It advanced to `currentTime=31.2` and failed by 45.1 seconds with the
same paused/empty media state. Six `googlevideo.com` `/videoplayback` Fetch
requests failed as `net::ERR_BLOCKED_BY_CLIENT` (`blockedReason=other`); no
HTTP error response was received. Comparing these runs shows that default
Shields filtering causes the client-side block in this path. The YouTube-only
allow choice removes that local block, but the same video then receives HTTP
403 from `googlevideo.com` and still fails. Shields is therefore a confirmed
contributor, not the complete root cause and not a sufficient fix. Standard
YouTube Shields is restored in the dedicated profile.

Additional evidence separates this from CDP. The same video was navigated to
through Brave's ordinary address bar, then left running for 60 seconds with no
CDP client attached. With standard Shields selected, the media had already
failed to `paused=true`, `currentTime=0`, `readyState=0`, `networkState=0`,
no `MediaError`, visible document, and `wasDiscarded=false`; YouTube showed
the generic playback error. Repeating the normal-UI test with the YouTube-only
allow choice produced the same failure state within 60 seconds. CDP was attached
only after each observation. The dedicated site's standard Shields setting was
then restored and verified in the Shields panel. This rules out CDP attachment
as a necessary cause of the interruption; it does not identify why the CDN
rejects the request.

On a further allow-mode run, one `rr5---sn-t0a7lnee.googlevideo.com` media host
failed with `net::ERR_NAME_NOT_RESOLVED`. A lookup for that exact host failed
through the machine resolver and through both 1.1.1.1 and 8.8.8.8, while other
sampled `googlevideo.com` hosts resolved. A separate allow-mode run sent
`/videoplayback` requests to `rr5---sn-ntqe6n7r.googlevideo.com` and received
HTTP 403 (`text/plain`, server `gvs 1.0`). The response body was not captured,
so no more specific denial reason is claimed. These are distinct, observed
CDN/network failures and neither supports a profile-isolation or decoder root
cause.

The latest instrumented allow-mode run again advanced briefly (`currentTime`
30.7 seconds, `readyState=4`), then stopped by 45.1 seconds with
`paused=true`, `currentTime=0`, `readyState=0`, `networkState=0`, no
`MediaError`, a visible page, and `wasDiscarded=false`; YouTube showed its
generic player error. The browser process and CDP endpoint were still healthy.
Sanitized media responses were HTTP 403 with `text/plain` from `gvs 1.0`.
No response body or signed URL was retained. An expiry-delta value from that
diagnostic was implausible and is not used as evidence. Afterward, the dedicated
profile's Shields panel reported “trackers, ads, and more blocked” and offered
“Allow all trackers & ads” as an action, confirming the site was no longer in
the temporary allow mode. No global setting or Personal Brave setting changed.

### Personal Brave baseline — 2026-10-30

Personal Brave was not running, so a normal, non-debugging `--new-window`
launch opened only the public `Big Buck Bunny` test video in its existing
personal profile. No remote-debugging flag was used, no personal setting was
changed, and no existing personal tab was inspected. Screenshots of the test
window's video area at 35 and 65 seconds both showed YouTube's generic
“Something went wrong” screen. This is a reproduced visible failure, but the
normal-profile run did not expose a trustworthy media-time reading; it is not a
measured 35-second playback duration or a 10-minute test. The test window was
closed afterward. This makes profile isolation alone an unlikely explanation
for the matching dedicated-profile failure, but does not identify whether
Personal Brave's own Shields or extensions contributed.

This is a **reproduced playback failure**, not a passing playback test. The
10-minute foreground/minimized/occluded runs, tab-switch and CDP-reconnect
scenarios, audio-only media, Personal Brave playback-time measurements, and the
previous Lumine runtime remain untested. In particular, do not infer background
playback reliability from the initial few seconds of successful decoding or
from a live process/CDP endpoint.

At an interruption, record the video `paused` state, `currentTime`,
`readyState`, `networkState`, and `MediaError` code/message; page
`visibilityState` and `document.wasDiscarded`; recent `Page.lifecycleEvent`
names; console error categories; failed-request host/resource type/error (never
the full URL or query string); Brave process presence; and whether a local CDP
round-trip succeeds. Record whether the user intentionally paused before
classifying a paused video. Capture this before restoring or replaying anything.
Treat a connected CDP socket as control-plane evidence only, not proof that
playback continued.

### Playback stall root cause — measured 2026-11-03/04

The ~40 s stall recorded above was reproduced under a controlled harness —
detached launch, CDP attach, one fresh `user-data-dir` per run, muted
`aqz-KE-bpKQ`, sampled every second for ~65 s — with exactly one variable
changing per pair. Every stall landed in the same window (~40–44 s: the
SABR/player "Something went wrong" moment), independent of engine, profile
state and attach path.

| Held constant | Varied | Result |
|---|---|---|
| engine, flags, attach | launch mode: `launch_persistent_context` (pipe) vs detached process + CDP attach | pipe stalls; detached plays past a minute |
| profile state, attach style | **`--remote-debugging-port=0` vs an explicit port** | `=0` stalled in **10/10** runs (2 fresh profiles, the reused spare, and the real `ensure()` tool path); an explicit port played in **7/7** (Chrome 9339, 9353; Brave 9371, 18397, 1348, plus the fixed spare run) |
| everything else | Google Chrome vs Brave vs Playwright Chromium (headless, temp dir) | engine is not the variable: Chrome and Brave both play |
| everything else | the tool's context-wide route guard | not the variable: stalls without it, plays with it |
| everything else | reused spare profile dir vs fresh | not the variable: the spare plays with an explicit port |
| engine, flags | `--disable-back-forward-cache` | the flag restores Playwright's history behaviour, and is refused: a person's launch does not carry Playwright switches |

A second, separate finding came out of the same harness: **history navigation
on a bfcache-intact browser with the route guard registered.** A restored
entry is invisible to Playwright — `go_back(wait_until="domcontentloaded")`
never returns (its restore hands over an already-loaded document, so no
`domContentLoaded` event fires), and with interception on, `commit` returns
`null` although the URL and title show the tab has moved. `go_history` now
waits on `commit`, decides "moved" by URL evidence, and gives only a
**refetched** entry a bounded `HISTORY_SETTLE_MS = 3000` to present its DOM;
a settle the page never satisfies does not turn a completed hop into a
refusal. The real-browser test
(`test_browser_engine.RealBrowserJourneyTests.test_history_and_scroll_move_a_real_page`)
is the regression gate for it.

The fixed shape is the shipped one: `session.free_debug_port()` picks an
OS-assigned number and `browser_command` writes it explicitly. Manual
verification procedure (any future launch change should re-run it):

1. `python -m unittest discover -s agent\tests -p "test_*.py"` — the full suite
   must pass, including the real-browser journeys.
2. Drive `session._Browser.ensure()` from a scratch script against
   `https://www.youtube.com/watch?v=aqz-KE-bpKQ`, sample `video.currentTime`
   once a second for 75 s, and require `currentTime >= 75` with no player
   error overlay; then `release()` and require the launched pid to be gone.
3. Repeat once more with the personal Brave profile held, so the
   spare-fallback launch is exercised too.

Record which path each run took (`launched ours` vs `joined`), the played-to
time, and the launcher's pid result. A logged stall should also capture the
`paused/currentTime/readyState` shape and one line of not-blocking
failed-request evidence, in the same vocabulary as the 2026-10-30 entries
above.

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
