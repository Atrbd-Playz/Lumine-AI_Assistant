# 02 — Test inventory

Every scenario below was executed against the real Playwright/CDP runtime and
the real browser described in [01-environment.md](01-environment.md). There are
**68 deterministic local scenarios** and **15 public-site observations**.

One rule applies to all of them: **the assertion reads the page after the
action.** A tool that says "Clicked: Primary Action" is not a pass; the pass is
`#mutation-result` containing the text that only that click can produce. Where a
postcondition could not be observed (native dialogs), the scenario is recorded as
`UNSUPPORTED` rather than inferred from the driver's return value.

Statuses are the seven required classifications:

`PASS` implemented + verified · `FAIL` implementation defect · `UNRELIABLE`
works but not deterministically · `UNSUPPORTED` capability absent ·
`INTENTIONALLY_REFUSED` refused by design · `BLOCKED` environment prevented the
observation · `NOT_TESTED` (not used in the final matrix).

---

## Phase 1 — Lifecycle & navigation

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-01-01 | Launch browser and navigate to target URL | Landed on the fixture with the expected title | PASS |
| TC-01-02 | Handle multiple redirects | 3 redirects resolved to the final URL | PASS |
| TC-01-03 | Handle HTTP 404 | 404 page observed with the correct response text | PASS |
| TC-01-04 | SPA client-side hash navigation | `#/dashboard` view rendered, hash changed, no reload | PASS |
| TC-01-05 | History back/forward traversal | Back reached `static_elements`, forward reached `forms` | PASS |
| TC-01-06 | Report an aborted connection and stay usable | `ERR_EMPTY_RESPONSE` reported; **next open needed 2 attempts** | UNRELIABLE |
| TC-01-07 | Terminate an endless redirect loop with a named error | `ERR_TOO_MANY_REDIRECTS` reported; **next open needed 2 attempts** | UNRELIABLE |

## Phase 2 — Page inspection

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-02-01 | Visible controls vs hidden / zero-dimension elements | Hidden and zero-size elements absent from candidates | PASS |
| TC-02-02 | Unicode, Bangla and emoji inspection | Bangla sentence and emoji glyphs extracted intact | PASS |
| TC-02-03 | A read reports the viewport it read and moves with the page | `scroll.y 0 → 2371`; below-fold marker became visible | PASS |

## Phase 3 — Element discovery

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-03-01 | Semantic ranking and ordinal disambiguation | "second Add to Cart" resolved to the second button | PASS |
| TC-03-02 | SVG and unlabelled icon buttons via `title` / `aria-label` | Search and refresh icons located | PASS |
| TC-03-03 | Resolve an input by its associated visible label | Field value stayed `''`; error `more than one thing on the page matches` | **FAIL** (DISC-1/DISC-2) |
| TC-03-04 | Resolve a text field by placeholder and verify the value | Value became `synthetic catalog query` | PASS |
| TC-03-05 | Refuse an ambiguous name instead of clicking a match | Refused, and the page did not mutate | PASS |

## Phase 4 — Pointer interaction

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-04-01 | Single, double and right click | `click`, `dblclick` and `contextmenu` listeners all fired | PASS |
| TC-04-02 | Hover and submenu activation | Submenu revealed; child link activated | PASS |
| TC-04-03 | Drag & drop with observed drop state | Raised `'Page' object is not callable`; target text unchanged | **FAIL** (PTR-1) |
| TC-04-04 | Act on a control by the reference `observe` produced | `r8` produced `Clicked: Primary Action` | PASS |

## Phase 5 — Keyboard control

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-05-01 | Fill and sequential type into form inputs | Submitted strings matched exactly | PASS |
| TC-05-02 | Submit a form with Enter and verify the submitted value | `fill` refused before the key was pressed; page state `not submitted` | **FAIL** (downstream of DISC-1/DISC-2) |
| TC-05-03 | Close an in-page overlay with Escape | Overlay `open → closed`, `hidden=True` | PASS |

## Phase 6 — Form interaction

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-06-01 | Dropdowns, checkboxes, radio buttons | Every state change verified in the DOM | PASS |
| TC-06-02 | Strict structural refusal on password fields | **Not refused** (`refused=False`, field not empty, 1 approval card) | **FAIL** (PWD-1) |
| TC-06-03 | Disabled control refused, then enabled and acted on | Click timed out while disabled; succeeded after enablement | PASS |
| TC-06-04 | Act on results created after a debounce | Blocked at `fill` by label resolution; results never requested | UNRELIABLE (downstream of DISC-1/DISC-2) |

## Phase 7 — Scrolling & viewport

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-07-01 | Scroll and verify the repeat-read notice | Viewport moved; an identical repeated read raised the notice | PASS |
| TC-07-02 | Scroll to content below the fold and read it | `final y=2371`; below-fold marker present; text changed | PASS |
| TC-07-03 | Infinite-scroll pagination | Rows `40 → 80` after two bottom scrolls | PASS |

## Phase 8 — Complex DOM

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-08-01 | Open shadow-root discovery | Button present in the DOM, **absent** from `observe` | UNSUPPORTED |
| TC-08-02 | Child-iframe control discovery | Iframe button **absent** from the top-level read | UNSUPPORTED |
| TC-08-03 | Act on a control in a named same-origin iframe | Frame resolved; semantic preflight refused with the false ambiguity message | UNSUPPORTED |
| TC-08-04 | Cross-origin iframe isolation and semantic action | Parent read worked; child control absent; frame-targeted click did not act | UNSUPPORTED |

## Phase 9 — Tabs & popups

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-09-01 | New tab, list, switch, close | All four operations changed the registry as asked | PASS |
| TC-09-02 | A JavaScript popup is a distinct tab | Registered as tab 14 at the popup's URL | PASS |
| TC-09-03 | Close every other tab, keeping the one in use | `tabs 4 → 1`; the active tab was kept | PASS |
| TC-09-04 | A popup that closes itself leaves the registry | Opened 3, popup closed, 2 remained | PASS |

## Phase 10 — Dialogs & permissions

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-10-01 | Native dialog handling | Alert auto-dismissed by the driver; **never observed or recorded** | UNSUPPORTED |
| TC-10-02 | Read and dismiss an in-page modal | Named dialog found; close removed the overlay | PASS |
| TC-10-03 | Native `confirm` / `prompt` / `beforeunload` | `confirm=false`, `prompt=null`, navigation proceeded; answers not chosen by Lumine | UNSUPPORTED |

## Phase 11 — Downloads & uploads

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-11-01 | Download interception and deletion by design | Download cancelled and the file deleted | INTENTIONALLY_REFUSED |
| TC-11-02 | File upload | No verb can set a file input | UNSUPPORTED |

## Phase 12 — Screenshots & vision

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-12-01 | Viewport screenshot with JPEG downscaling and PNG | Preview 46,118 B JPEG; full 38,848 B PNG | PASS |
| TC-12-02 | The image reflects the page, and the preview is bounded | Two pages produced different bytes; preview under cap and smaller than full | PASS |

## Phase 13 — Session sharing & state synchronisation

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-13-01 | Both tools share one live page and one session | Same URL; `find` saw the control; the mutation was visible to `browse` | PASS |
| TC-13-02 | A tab opened by one tool is listed and closable by the other | `web_control` listed the `browse` tab and closed it | PASS |
| TC-13-03 | Client-side route state stays synchronised | Both tools carried `#/dashboard` and read the same rendered view | PASS |

## Phase 14 — Planner intelligence

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-14-01 | Open → find → act with verification | 3-step pipeline reached `observed` | PASS |
| TC-14-02 | Avoid redundant re-navigation | Short-circuited to the current snapshot without reloading | PASS |
| TC-14-03 | Find and activate a dynamically hydrated control | Delayed control located; page-side effect observed | PASS |
| TC-14-04 | A planner step that finds nothing fails cleanly | Step reported `failed`; dependent act failed; page still usable | PASS |

## Phase 15 — Failure recovery

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-15-01 | Stale generation rejection | Generation `999999` refused with "observe it again" | PASS |
| TC-15-02 | Survive a page that destroys its own document | Read returned; session recovered | PASS |
| TC-15-03 | Uncaught page errors are observable and not fatal | `window_error` and `unhandled_rejection` recorded; next page worked | PASS |

## Phase 16 — Security & safety

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-16-01 | Route guard blocks loopback and private IPs | With local access **off**, loopback refused by name | PASS |
| TC-16-02 | Refuse `file:` and `javascript:` schemes | Both rejected before any request | PASS |
| TC-16-03 | Prompt-like page text stays delimited | Visible and zero-size text stayed inside the fence | PASS |
| TC-16-04 | Refuse a password field whichever signal names it | **3 of 5 accepted text**, 3 approval cards raised | **FAIL** (PWD-1) |
| TC-16-05 | Refuse dangerous schemes and embedded credentials | 7 inputs refused with the right reason; 0 accepted unexpectedly | PASS |
| TC-16-06 | Price a submit as sensitive and a click as an act | Submit asked as `sensitive`; ordinary click asked as `act` | PASS |
| TC-16-07 | Do not read `display:none`; keep reads fenced | Hidden text not read; every read phrase inside the fence | PASS |
| TC-16-08 | A page cannot pivot to loopback through a click | No forbidden target loaded; **the page was replaced by Chromium's error page** | UNRELIABLE (NAV-2) |
| TC-16-09 | Page text cannot close the untrusted fence | **Instruction landed outside the fence** | **FAIL** (FENCE-1) |

## Phase 17 — Performance

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-17-01 | Latency of core operations | Navigation 187.1 ms, observe 48.2 ms, act 169.7 ms | PASS |
| TC-17-02 | Repeated latency stays in a voice-turn budget | Observe median 30.6 ms; click median 71.9 ms; screenshot 79.8 ms | PASS |

## Phase 18 — Stability

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-18-01 | Repeated cross-fixture navigation and observation | 3 dual-page cycles, no leak, no error | PASS |
| TC-18-02 | Repeated tab open/close returns to the same size | 5/5 cycles; `tabs 2 → 2`; page still usable | PASS |

## Phase 19 — Real-world compatibility (local)

| ID | Scenario | Observed postcondition | Status |
| --- | --- | --- | --- |
| TC-19-01 | Open and read two unauthenticated public sites | `example.com` and `wikipedia.org` read and fenced | PASS |

---

## Phase 19 — Public-site campaign

Fifteen unauthenticated sites, one observation each, no interaction beyond a
semantic heading lookup.

| Site | Observed | Status |
| --- | --- | --- |
| Google public search results | Page read (2,400 chars) | PASS |
| MDN Web Docs | Page read (2,163 chars) | PASS |
| The Guardian public news page | Page read (1,974 chars) | PASS |
| eBay public category page | Redirected to `splashui/challenge` | BLOCKED |
| Reddit public landing page | `net::ERR_ABORTED` | BLOCKED |
| YouTube public search results | Page read (1,692 chars) | PASS |
| Gmail public sign-in boundary | Page read (1,814 chars) | PASS |
| Google Drive public sign-in boundary | Page read (1,936 chars) | PASS |
| OpenStreetMap public map | Page read (2,471 chars) | PASS |
| GitHub public repository | Page read (2,261 chars) | PASS |
| Stack Overflow public questions | Anti-automation interstitial ("Just a moment…") | BLOCKED |
| Playwright-hosted TodoMVC SPA | Page read (800 chars) | PASS |
| The Internet entry-ad overlay demo | Page read (785 chars) | PASS |
| httpbin redirect chain | Page read (1,145 chars) | PASS |
| Example Domain | Page read (1,226 chars) | PASS |

The three `BLOCKED` results are environmental. They are not counted as passes and
they are not browser defects; they are recorded because a campaign that hid them
would be claiming site coverage it did not have.

---

## Where each record lives

| Artifact | Contents |
| --- | --- |
| `certification_results.group{1..7}.json` | 15 + 11 + 11 + 7 + 10 + 11 + 3 scenarios |
| `certification_results.json` | merged 83 records, seven statuses, capability matrix |
| `public_compatibility_results.json` | the 15 public observations |
| `evidence/group*.log` | stdout/stderr for each bounded run |
| `evidence/repro_defects.log` | the five reproductions in [05-defects.md](05-defects.md) |
