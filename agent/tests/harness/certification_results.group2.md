# Lumine browser certification — run summary

- Tests run: **11**
- Recorded scenarios: **11**
- Duration: **24.62 s**
- Status counts: `{"FAIL": 3, "PASS": 7, "UNRELIABLE": 1}`

## Capability matrix

| Phase | Area | Result | Counts |
| --- | --- | --- | --- |
| 4 | Pointer Interaction | **FAIL** | FAIL=1, PASS=3 |
| 5 | Keyboard Control | **FAIL** | FAIL=1, PASS=2 |
| 6 | Form Interaction | **FAIL** | FAIL=1, PASS=2, UNRELIABLE=1 |

## Scenarios

| ID | Status | Capability | Evidence |
| --- | --- | --- | --- |
| TC-04-01 | PASS | Execute single, double, and right click interactions | DOM event listeners verified for click, dblclick, and contextmenu |
| TC-04-02 | PASS | Hover interaction and submenu item activation | Hover triggered submenu reveal; child link successfully clicked |
| TC-04-03 | FAIL | Drag & Drop execution with observed drop state | Drag action raised: that control did not respond: 'Page' object is not callable |
| TC-04-04 | PASS | Act on a control by the reference observe produced | ref=r8; result='Clicked: Primary Action'; error='' |
| TC-05-01 | PASS | Fill and sequential type into form inputs | Input fields verified with exact submitted strings |
| TC-05-02 | FAIL | Submit a form with Enter and verify the submitted value | fill reported=False ('more than one thing on the page matches'); press reported=False (''); page state='not submitted' |
| TC-05-03 | PASS | Close an in-page overlay with the Escape key | overlay 'open' -> 'closed'; hidden=True; press reported=True ('') |
| TC-06-01 | PASS | Interact with dropdowns, checkboxes, and radio buttons | All form element state changes verified in DOM |
| TC-06-02 | FAIL | Strict structural refusal on password fields | refused=False; field_empty=False; approval_calls=1; response='' |
| TC-06-03 | PASS | Do not click a disabled control; act once enablement is satisfied | disabled click refused=True ('that control did not respond: Locator.click: Timeout 6000ms exceeded.\nCall log:\n  - waiting for locator("[data-lumine-ref=\\"r9\\"]")\n    - locator reso'); state 'waiting'->'waiting'; enabled after check=True; final='ordered' |
| TC-06-04 | UNRELIABLE | Act on results that a debounced request created after the first paint | fill reported=False ('more than one thing on the page matches'); search state='idle'; click reported=False (''); after click='idle' |
