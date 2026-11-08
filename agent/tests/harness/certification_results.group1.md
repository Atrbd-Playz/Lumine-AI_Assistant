# Lumine browser certification — run summary

- Tests run: **15**
- Recorded scenarios: **15**
- Duration: **11.95 s**
- Status counts: `{"FAIL": 1, "PASS": 12, "UNRELIABLE": 2}`

## Capability matrix

| Phase | Area | Result | Counts |
| --- | --- | --- | --- |
| 1 | Lifecycle & Navigation | **UNRELIABLE** | PASS=5, UNRELIABLE=2 |
| 2 | Page Inspection | **PASS** | PASS=3 |
| 3 | Element Discovery | **FAIL** | FAIL=1, PASS=4 |

## Scenarios

| ID | Status | Capability | Evidence |
| --- | --- | --- | --- |
| TC-01-01 | PASS | Launch browser and navigate to target URL | Landed on http://127.0.0.1:14825/fixtures/static_elements.html with verified title |
| TC-01-02 | PASS | Handle multiple redirects | Successfully resolved 3 redirects to http://127.0.0.1:14825/fixtures/static_elements.html |
| TC-01-03 | PASS | Handle HTTP 404 status code | 404 error page observed with correct response text |
| TC-01-04 | PASS | Handle SPA client-side hash navigation | Dashboard View rendered without full page reload |
| TC-01-05 | PASS | Browser history back/forward traversal | Successfully moved back to static_elements and forward to forms |
| TC-01-06 | UNRELIABLE | Report an aborted connection and stay usable afterwards | connection failure reported=True ('I couldn\'t open 127.0.0.1: Page.goto: net::ERR_EMPTY_RESPONSE at http://127.0.0.1:14825/api/abort Call log: - navigating to "http://127.0.0.1:14...'); recovery attempts=2; landed on http://127.0.0.1:14825/fixtures/static_elements.html; last recovery error='' |
| TC-01-07 | UNRELIABLE | Terminate an endless redirect loop with a named error | redirect loop reported=True ('I couldn\'t open 127.0.0.1: Page.goto: net::ERR_TOO_MANY_REDIRECTS at http://127.0.0.1:14825/api/redirect-loop Call log: - navigating to "http://...'); recovery attempts=2; last recovery error='' |
| TC-02-01 | PASS | Distinguish visible controls from hidden/zero-dimension elements | Hidden elements properly excluded from candidates |
| TC-02-02 | PASS | Verify Unicode, Bangla and Emoji text inspection | Bangla sentence and emoji glyphs accurately extracted |
| TC-02-03 | PASS | A read reports the viewport it read and moves with the page | scroll.y 0 -> 2371; text changed=True; below-fold marker visible after scroll=True |
| TC-03-01 | PASS | Semantic locator ranking & ordinal disambiguation | Ordinal ranking resolved 'second Add to Cart' correctly |
| TC-03-02 | PASS | Discover SVG and unlabelled icon buttons via title and aria-label | SVG search button and refresh icon correctly located |
| TC-03-03 | FAIL | Resolve an input by its associated visible label | Input value after label-based fill: ''; error: more than one thing on the page matches |
| TC-03-04 | PASS | Resolve a text field by its placeholder and verify the typed value | find matched=True; fill attempted=True (''); field value='synthetic catalog query' |
| TC-03-05 | PASS | Refuse an ambiguous name instead of clicking an arbitrary match | refused=True ('more than one thing on the page matches'); page mutation unchanged=True |
