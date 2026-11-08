# 09 — Remediation plan

No production code was changed during certification. This is the work that would
close the findings, ordered by what makes the product unsafe or dishonest first,
with the test that proves each fix.

Effort figures are rough engineering estimates for one developer familiar with the
module. They are not commitments.

---

## P0 — blocks any production claim

### 1. PWD-1: decide the password refusal from the element's type

**Change.** In `agent/tools/browser/control.py::_is_password`, return `True` when
`candidate.detail.get("type") == "password"` **before** consulting any word list.
Keep the existing name/value heuristic as a second net for a field whose declared
type is not `password` but whose label is.

**Why first.** It is the only finding where the product performs an action it
promises never to perform, and the fact needed to prevent it is already collected
(`state.py:589`).

**Tests.**

- `TC-16-04` must report `refused by policy = [all five]`, `typed into = []`,
  `approval cards raised = 0`.
- `TC-06-02` must report `refused=True`, `field_empty=True`, `approval_calls=0`.
- Add unit coverage for: `type=password` with an innocuous name; `type=password`
  with no label; `type=text` labelled "Password" (still refused by the net);
  `type=password` addressed by `fill`, `type`, and the planner's step path.
- A regression test that the refusal happens **before** `require_approval`: the
  recorder must see zero calls.

**Do not** replace the word list with a longer word list. The type is the fact.

**Estimate:** ~1 hour plus tests.

---

### 2. FENCE-1: a page cannot terminate the untrusted block

**Change.** In `agent/tools/tool_results.py`:

1. Make the fence per-session and unguessable — e.g.
   `<<<LUMINE_TOOL_DATA:8f3c1a…` / `LUMINE_TOOL_DATA:8f3c1a…>>>` from a nonce
   generated once per process or session.
2. Neutralise any occurrence of the *static* token inside `body` (both open and
   close forms), with a preamble sentence that says one was present. The page's
   words must survive; only their power to act as a delimiter is removed.
3. Apply the same treatment to every caller of `wrap_untrusted`, not only the
   browser path — it is a shared boundary.

**Why a nonce and not just stripping.** Stripping alone protects against today's
constant and fails against a leaked or guessed one; a nonce is structural.

**Tests.**

- `TC-16-09`: exactly `open markers=1; close markers=1`, and the injected
  instruction index is **less** than the close index.
- A unit test that wraps text containing both the open and the close token and
  asserts one marker of each in the result, with the body's words still present.
- A unit test at the truncation boundary: a marker that would be cut in half must
  not produce a valid delimiter.
- `TC-16-03` and `TC-16-07` stay green (prose injection still fenced).
- A new scenario: a page that emits the *nonce-free* prefix only.

**Estimate:** ~2–3 hours including the shared-boundary audit.

---

## P1 — required for a reliable tool

### 3. DISC-1: accessible name before placeholder

**Change.** In `agent/tools/browser/state.py::labelFor`, order by accessible-name
precedence: `aria-label` (and `aria-labelledby` if collected) → `<label for>` →
wrapping `<label>` → `title` → `placeholder` → `name`/`id`. The placeholder
becomes the last resort it is meant to be.

**Dependency.** Do PWD-1 **first**. This change alters `candidate.name`, and a
name-based password check would change what it catches. After PWD-1 the type is
authoritative, so the reorder is safe.

**Tests.**

- `TC-03-03` passes: `Full Name (Required)` fills `#input-text`.
- A field with only a placeholder is still addressable by it.
- A field with `aria-label` and `<label for>` reports the `aria-label`.
- `TC-02-02` (Unicode/Bangla) still passes — labels may contain Bangla.
- Re-judge `TC-05-02` and `TC-06-04` on their own postconditions; do not assume
  they pass because the fill step now works.

**Estimate:** ~1–2 hours plus a regression pass over `TC-03-*`, `TC-05-*`,
`TC-06-*`. The risk is that other tests were quietly relying on placeholder
names.

---

### 4. DISC-2: say what actually happened

**Change.** In `agent/tools/browser/control.py::_element_action`, replace the
unconditional `MSG_AMBIGUOUS` with the resolution's own answer:

- empty → "I couldn't find that on the page" (the sentence `find` already uses);
- several → `resolution.describe()`, which names the candidates;
- a bad `index` → its own message, not "not found".

**Tests.**

- Unit: zero candidates, one candidate, two tied candidates, ordinal out of
  range — four distinct messages.
- `find` and `fill` return the same sentence for the same missing name.
- `TC-08-03`/`TC-08-04` change their message but keep `UNSUPPORTED`; the iframe
  gap must not be disguised as a locator ambiguity.

**Estimate:** ~1 hour.

---

### 5. PTR-1: `page` is a property

**Change.** `agent/tools/browser/engine.py:593`:
`page = locator.page` — a property access, not an await.

**Tests.**

- `TC-04-03` passes: `#drag-target` contains `Item Dropped`.
- The offset parser's bounds still hold (`down 50`, `left 200`, nonsense
  refused).
- A drag the page does not complete still reports failure.
- A unit test with a fake locator whose `page` is a plain object asserts the code
  path does not await it — that is the mistake, not the drag itself.

**Estimate:** ~15 minutes plus tests.

---

### 6. NAV-1: a failed navigation must not cost the next one

**Change.** First instrument `engine.open` around a forced `ERR_EMPTY_RESPONSE`
and `ERR_TOO_MANY_REDIRECTS` to see which state the second `goto` is racing.
Then make recovery explicit: if the current document is
`chrome-error://chromewebdata/`, reset to `about:blank` (or reload the frame)
before navigating, and report the first failure rather than swallowing it.

**Tests.**

- 20 consecutive "fail then open" trials succeed on the first attempt.
- The first error is still reported with its named message.
- `TC-01-06` and `TC-01-07` pass.

**Do not** add a blanket retry to make the symptom disappear; the retry is the
measurement, not the fix.

**Estimate:** ~2–4 hours, mostly investigation.

---

### 7. NAV-2: a refused navigation must not silently replace the page

**Change.** After any action that can navigate (a click on a link, a submit),
compare the resulting URL. If it is `chrome-error://chromewebdata/` or an error
document, report the navigation as refused/blocked and say the page was replaced,
with the way back. Optionally pre-inspect a link's `href` and refuse a target the
address guard would refuse, before clicking — defence in depth, not a substitute
for the guard.

**Tests.**

- `TC-16-08` passes: no forbidden load **and** either the page is preserved or
  the result says it was replaced.
- The click result distinguishes "clicked, page unchanged" from "clicked,
  navigation blocked".
- No `javascript:` href executes in-page (already true).

**Estimate:** ~2–3 hours.

---

## P2 — capability work, scheduled rather than urgent

### 8. DOM-1: shadow DOM and frames

This is a feature, not a patch. It needs:

- traversal of open shadow roots in the snapshot script;
- per-frame collection with references namespaced by frame;
- a generation per frame, so a navigation in one frame invalidates only its own
  references (the same rule `_Tab.generation` already applies one level up);
- an action path that resolves a reference in its owning frame;
- a decision about closed shadow roots (unreachable by design — say so rather
  than pretending).

**Tests:** `TC-08-01..04` flip from `UNSUPPORTED` to `PASS`; a new case where a
frame navigates and the parent's references stay valid, while the frame's are
invalidated.

**Estimate:** ~2–4 days.

---

### 9. Native dialogs

Register a `dialog` handler that **records** type and message and asks the same
approval question a form submit asks; do not let the driver auto-answer. The
current behaviour answers `false`/`null` on the user's behalf, which is the wrong
direction for `confirm` ("delete this?") and for `beforeunload`.

**Tests:** `TC-10-01` and `TC-10-03` become observable; add a scenario where the
answer is "no" and the page's action does not happen.

**Estimate:** ~1 day, including the consent copy.

---

### 10. File upload

Add a verb that sets a file input from an **allowlisted folder by resolved path**,
reusing the `agent/tools/messaging/whatsapp_files.py` shape (resolve first, compare
resolved roots, extension allowlist, no URL downloads). Never accept a
model-supplied path without that check; a file input is a way to send a local file
to a page.

**Tests:** an allowlisted file uploads; `..`, an absolute path outside the roots,
and a symlink out are refused; the approval card names the file, not the
directory layout.

**Estimate:** ~1 day.

---

### 11. Invisible text is read

`TC-16-07` measured `zero-size text read=True` and `aria-hidden text read=True`.
It stays inside the fence, so it is data rather than instruction, but a page can
influence the model with text the user cannot see. Decide deliberately:

- exclude zero-size and `aria-hidden` text, or
- read it but mark it as invisible in the payload.

Either is defensible; leaving it unmarked is the only option that hides a fact
from the reader. This is a small change with a security review, not a bug fix.

**Estimate:** ~2 hours plus review.

---

## What not to do

| Temptation | Why not |
| --- | --- |
| Strip the fence markers from page text | It deletes the page's words, and it fails against a leaked constant. Neutralise or use a nonce. |
| Add a retry to hide NAV-1 | The retry is the symptom. The next navigation must work first time. |
| Teach the tests to ask by placeholder | The user says "the full name field". Fix the name. |
| Auto-answer native dialogs | That is the current defect, not the fix. |
| Relax `LUMINE_BROWSER_ALLOW_LOCAL` everywhere | It is the documented fixture flag and is already scoped. The guard must stay on in production. |
| Add `agent/tests/harness/__init__.py` back | It makes 68 browser scenarios part of the unit suite. The namespace-package layout is deliberate. |
| Fix DISC-1 before PWD-1 | It changes the strings the password heuristic reads. Type first. |

---

## Suggested sequence

```text
Week 1   PWD-1, FENCE-1            → re-run TC-06-02, TC-16-04, TC-16-09
Week 1   DISC-2, PTR-1             → cheap, independent, unblocks diagnosis
Week 2   DISC-1                    → re-run phases 3, 5, 6
Week 2   NAV-1, NAV-2              → re-run phases 1 and 16
Week 3   Invisible-text decision    → phase 16 review
Later    DOM-1, dialogs, upload     → phase 8, 10, 11
```

---

## Exit criteria for re-certification

A production claim should require all of the following:

1. **Zero `FAIL`.** All six current failures carry a passing scenario, and no new
   `FAIL` appears.
2. **PWD-1 is structurally closed:** 5/5 password variants refused, **0**
   approval cards raised, and the four refusal paths covered by unit tests.
3. **FENCE-1 is structurally closed:** exactly one open and one close marker,
   with every page phrase inside, including the injected instruction.
4. **No `UNRELIABLE` in phases 1 or 16.** The post-failure recovery and the
   page-initiated navigation are deterministic.
5. **No silent capability gap in phases 3–6.** Any remaining `UNSUPPORTED` in
   those phases is explicitly accepted in this document, not merely recorded.
6. **The full Python suite is green** with the harness still outside discovery:
   `2,488` tests, `0` harness scenarios.
7. **A second run** of the seven groups and the public campaign reproduces the
   verdict, with the same environment report or a documented change to it.
8. If engine parity is claimed, a Chromium run as well as Brave.

Re-certification is the same commands as [README.md](README.md#re-running). The
report should be regenerated from the new JSON, not edited by hand; the numbers in
these documents are a snapshot of commit `e36860a` plus the harness.
