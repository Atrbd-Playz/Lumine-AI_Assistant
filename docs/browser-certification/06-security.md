# 06 — Security & safety

Phase 16 is the security phase, but safety properties are also asserted in
phase 3 (ambiguity refusal), phase 6 (password), phase 9 (tab ownership) and
phase 11 (download refusal). This document reports the properties, what held, what
did not, and the limits of the claim.

---

## Property matrix

| # | Property | Test | Result | Evidence |
| ---: | --- | --- | --- | --- |
| S1 | Loopback and private addresses are refused when local access is off | TC-16-01 | **Held** | `I can't open 127.0.0.1 -- 127.0.0.1 is not a public address. Nothing was shown.` |
| S2 | `file:`, `javascript:`, `data:`, `vbscript:` are refused before a request | TC-16-02, TC-16-05 | **Held** | 7 inputs refused with `scheme` / `credentials` / `whitespace`; 0 accepted unexpectedly |
| S3 | Embedded credentials in a URL are refused | TC-16-05 | **Held** | `http://user:password@…` → `credentials` |
| S4 | Prompt-like page prose stays inside the untrusted fence | TC-16-03, TC-16-07 | **Held** | Every read phrase between the markers |
| S5 | `display:none` text is not read | TC-16-07 | **Held** | `display:none text read=False` |
| S6 | A form submit is priced `sensitive`; an ordinary click is `act` | TC-16-06 | **Held** | Submit asked as `sensitive`; click asked as `act` |
| S7 | Ambiguity refuses instead of clicking an arbitrary match | TC-03-05 | **Held** | Refusal, and the page did not mutate |
| S8 | Downloads are cancelled and deleted | TC-11-01 | **Held by design** | `INTENTIONALLY_REFUSED` — not a pass |
| S9 | A password field is never typed into | TC-06-02, TC-16-04 | **FAILED** | 3 of 5 variants accepted text; 3 cards raised |
| S10 | Page text cannot close the untrusted fence | TC-16-09 | **FAILED** | Injected instruction outside the block |
| S11 | A page cannot pivot to loopback through a click | TC-16-08 | **Held, with a cost** | No forbidden target loaded; the page was replaced by Chromium's error page |
| S12 | Every side-effecting act is gated at the right class | TC-06-02, TC-16-06, TC-16-08 | **Partially held** | Classification is right; the password gate is decided by the wrong fact |

`Held` means the observed postcondition matched the property. `Held by design`
means the product refuses the capability entirely. Neither `INTENTIONALLY_REFUSED`
nor `UNSUPPORTED` is counted as a security success where the property was not
exercised.

---

## S9 — password refusal (PWD-1)

The full analysis is in [05-defects.md](05-defects.md#pwd-1--a-password-field-is-recognised-by-its-name-not-its-type).
The security summary:

- The refusal exists, is unconditional in intent, and is placed **before**
  `require_approval` (`control.py:992`). That ordering is correct and should not
  change.
- The fact it reads is the wrong one. `detail["type"] == "password"` is already
  in the payload (`state.py:589`) and is not consulted by `_is_password`
  (`control.py:319`).
- Consequence: an attacker-controlled page can name its password field
  `Account Access` with `placeholder="Enter code"`, and the tool fills it. The
  measured variants are not exotic — a recovery-key field is the ordinary case.
- The approval cards are not a mitigation. Three were raised, so a user answering
  "yes" would be consenting to something they were not told: the summary names the
  field's *label*, not the fact that it is a password.

This is the highest-priority item in the report.

---

## S10 — the untrusted fence (FENCE-1)

The full analysis is in [05-defects.md](05-defects.md#fence-1--page-text-can-close-the-untrusted-data-fence).
The security summary:

- The delimiter is a fixed constant (`tool_results.py:55-56`) and the body is
  interpolated verbatim (`tool_results.py:186`).
- A page that prints the constant closes the block. The measured result has two
  open and two close markers; correct output has one of each.
- The text after the page-supplied close marker — in the fixture, an instruction
  to send saved passwords to an external URL — is indistinguishable, positionally,
  from a genuine instruction.
- S4 passing is not a partial mitigation: it tests prose injection, not marker
  injection. A fence that only survives prose is a fence that does not survive an
  attacker who has read the source.

**What this campaign does and does not prove about model behaviour.** It proves
that the instruction lands *outside the fence*. It does **not** prove that the
model would follow it — that requires a live model turn and a deliberate
evaluation, which is out of scope here. The security requirement is the boundary,
not the model's good behaviour, because the boundary is the only part the product
controls.

---

## S11 — page-initiated navigation (NAV-2)

The guard is a request-time guard, so a click that navigates is judged after the
click. The result is the safest possible failure: `ERR_FAILED`, no forbidden
target loaded, and a `javascript:` href that did not execute in the page
(`javascript: link executed in-page=False`).

The cost is state: the tab becomes `chrome-error://chromewebdata/`. The security
property held; the tool's report did not. A model told "clicked" will continue
from a page that no longer exists. This is a correctness and honesty defect, not
a bypass.

---

## Residual risks

Ordered by severity, including risks the campaign could not eliminate.

| Risk | Severity | Notes |
| --- | --- | --- |
| Password fields named by anything but "password" accept text | **Critical** | PWD-1. |
| A page can close the untrusted fence | **Critical** | FENCE-1. |
| Invisible page text is read and fenced, not excluded | Medium | `TC-16-07` measured `zero-size text read=True` and `aria-hidden text read=True`. It stays inside the fence, so it is "facts", but a user cannot see the text that is influencing the model. Consider excluding zero-size/`aria-hidden` text or marking it explicitly. |
| A refused page navigation replaces the page while reporting success | Medium | NAV-2. |
| Native dialogs are answered by the driver, not by Lumine | Low | `TC-10-01`/`TC-10-03`. A page can trigger `beforeunload` and the navigation proceeds; there is no consent surface for it. |
| The human approval path is not exercised by this campaign | Scope | The harness records cards; it does not speak the question or answer "no". Covered by permission-gate unit tests, not by this evidence. |
| Cross-origin data exposure through a frame | Untested | Iframe content is unsupported, so there is nothing to read — but the capability, when added, needs its own origin checks. |
| Anti-automation interstitials | Environmental | 3 public sites. No attempt was made to bypass them. |

---

## Guardrail compliance

The stage rules were: do not weaken route guards, do not bypass password
restrictions, do not auto-approve sensitive actions, and do not let page text
override instructions.

| Rule | Compliance |
| --- | --- |
| No weakened route guard | No guard code changed. Local fixtures are reachable because `LUMINE_BROWSER_ALLOW_LOCAL=1` is the documented flag; S1 and S11 are tested with it set to `0`. |
| No password bypass | No patch, no mock of `_is_password`. The harness measured the refusal as it is; the result is a `FAIL`. |
| No auto-approval in the product | `require_approval` is untouched. The harness substitutes a recorder **inside the test process** to observe cards and avoid blocking on a voice question. The product's gate, classes and ordering are what is measured. |
| Page text cannot override instructions | The fixtures contain instructions; the harness asserts they remain data. It does not act on them. |
| Synthetic data only | `SYNTHETIC-PROBE-VALUE` and fixture literals. No real credential, cookie or message is read or written. |
| Non-destructive public campaign | GET navigation, observation, one heading lookup. No sign-in, form, consent, cart, message or purchase. |

The one honest limitation is the approval recorder: it means the campaign proves
*what the gate asks* and *how many times*, not *what happens when a person says
no*. That is recorded as a scope gap rather than papered over.

---

## Threat model, stated once

- **Page content is untrusted.** It can be hostile, and it can read this
  repository.
- **The model is not a security boundary.** Instructions and data are separated
  by the fence; the model is expected to follow the product's rules, but the
  product must not depend on it doing so.
- **The user is the approver.** A card is a question; a password field is not a
  question at all, which is why its refusal must be structural and pre-gate.
- **The address guard is the outer wall.** It is enforced per request, including
  subresources and page-initiated navigations, which is why S1/S2/S3 hold even
  when the click path is involved.

Under that model, PWD-1 and FENCE-1 are the two places where the product's stated
boundary and its observed behaviour diverge.
