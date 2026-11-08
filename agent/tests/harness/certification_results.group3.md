# Lumine browser certification — run summary

- Tests run: **11**
- Recorded scenarios: **11**
- Duration: **14.61 s**
- Status counts: `{"PASS": 7, "UNSUPPORTED": 4}`

## Capability matrix

| Phase | Area | Result | Counts |
| --- | --- | --- | --- |
| 7 | Scrolling & Viewport | **PASS** | PASS=3 |
| 8 | Complex DOM | **PARTIAL** | UNSUPPORTED=4 |
| 9 | Tabs & Popups | **PASS** | PASS=4 |

## Scenarios

| ID | Status | Capability | Evidence |
| --- | --- | --- | --- |
| TC-07-01 | PASS | Scroll page and verify repeat-read notice flag | Scroll moved viewport; identical repeated read triggered notice warning |
| TC-07-02 | PASS | Scroll to content below the fold and read it | scroll reported='Scrolled to the bottom of the page.'; final y=2371; below-fold marker=True; text changed=True |
| TC-07-03 | PASS | Trigger and observe infinite-scroll pagination | rows 40 -> 80 after two bottom scrolls |
| TC-08-01 | UNSUPPORTED | Shadow DOM element discovery limitation | Open shadow root button present in observe: False |
| TC-08-02 | UNSUPPORTED | Child iframe control discovery limitation | Child iframe button found in top-level observe: False |
| TC-08-03 | UNSUPPORTED | Act on a control in a named same-origin iframe | The frame was resolved, but semantic preflight refused the unique frame control before acting: more than one thing on the page matches |
| TC-08-04 | UNSUPPORTED | Cross-origin iframe content isolated from parent read and semantic action | parent read=True; child control absent from parent read=True; frames=2; frame-targeted click acted=False ('more than one thing on the page matches'); child state='child idle' |
| TC-09-01 | PASS | Tab lifecycle: new tab, list tabs, switch, and close | Multi-tab switching and lifecycle verified |
| TC-09-02 | PASS | Register a JavaScript popup as a distinct tab | Popup registered as tab 14 at http://127.0.0.1:5289/fixtures/forms.html |
| TC-09-03 | PASS | Close every other tab while keeping the one in use | tabs 4 -> 1; active kept=True; result='Closed 3 other tab(s). 1 left, and it is the one you were on.' error='' |
| TC-09-04 | PASS | A popup that closes itself is removed from the tab registry | tabs opened=3 (popup [22]); close action='clicked'; tabs after=2 |
