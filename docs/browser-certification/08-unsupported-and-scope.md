# 08 — Unsupported capabilities and scope gaps

This document exists so the report cannot be read as broader than it is. A
`PASS` in [04-results.md](04-results.md) applies only to the scenario named.

---

## Capabilities that do not exist

Seven `UNSUPPORTED` records. None is a crash; each is a thing a user may ask for
and not get.

| Capability | Test | What happens | User impact | Workaround today |
| --- | --- | --- | --- | --- |
| Open shadow-root content | TC-08-01 | The control is in the DOM and absent from `observe`; it cannot be addressed | Components built with shadow DOM are invisible to the tool | None inside the browser tools; `desktop_control` can click by coordinate, but it ships off and suppresses both browser tools when on |
| Same-origin iframe discovery | TC-08-02 | The iframe's controls are absent from the top-level read | Embedded forms, editors and widget panels cannot be read | None |
| Same-origin iframe action | TC-08-03 | The frame is resolved, but the semantic preflight finds nothing and refuses | A named frame control cannot be clicked or filled | None |
| Cross-origin iframe content | TC-08-04 | Parent and child are isolated; the child action does not act | Embedded third-party content (payments, maps, video, support widgets) is unreachable | None |
| Native `alert` observation | TC-10-01 | The driver auto-dismisses it; Lumine never sees the message | A page can show a message the user is told nothing about | None |
| Native `confirm` / `prompt` / `beforeunload` | TC-10-03 | The driver answers `false` / `null` and navigation proceeds | A page's "are you sure?" is answered for the user, not by them | None |
| File upload | TC-11-02 | No verb can set a file input | "Upload this file" is impossible | None |

The first four are one underlying gap (`DOM-1`): the snapshot is main-frame only
and does not descend into shadow roots. The dialog and upload gaps are separate
capability decisions that have not been made.

---

## Refused by design

| Capability | Test | Behaviour | Why it is refused |
| --- | --- | --- | --- |
| Download delivery | TC-11-01 | The download is cancelled and the file deleted | A downloaded file is outside every boundary the product has; keeping it would be a way to write arbitrary content to disk from a page |

`INTENTIONALLY_REFUSED` is a **policy** result, not a security proof. The
campaign verified that the cancel-and-delete path runs; it did not verify what a
user is told, because that is a voice-copy question outside the browser.

---

## Blocked by the environment

| Site | What was observed | Why it is not a defect |
| --- | --- | --- |
| eBay public category page | Redirect to `https://www.ebay.com/splashui/challenge?…` | An automated-access interstitial. The browser is working; the site declined to serve the page. |
| Reddit public landing page | `net::ERR_ABORTED` | The site aborted the navigation. Repeated in two separate campaigns with the same result. |
| Stack Overflow public questions | "Just a moment…" challenge page | The same class of interstitial. |

No attempt was made to solve, bypass or disguise these challenges. They reduce
the real-site sample from 15 to 12 usable observations and are recorded as such.

---

## What this campaign did not answer

These are not failures. They are questions the evidence cannot speak to, listed
so a reader knows where the report stops.

### Security and consent

| Gap | Why it was not tested | What closing it needs |
| --- | --- | --- |
| A human answering **no** to an approval card, by voice | A browser harness cannot speak into the LiveKit room; the approval recorder substitutes the answer | A live room test that asks, hears "no", and asserts the action did not happen |
| Whether the model follows an instruction that escaped the fence | This campaign proves the *boundary* is broken, not the model's behaviour | A live model turn with a hostile page and a scored outcome |
| Prompt injection beyond the delimiter (tool-name spoofing, role confusion) | Out of scope for a browser capability campaign | A dedicated adversarial evaluation |
| Permission prompts (geolocation, notifications, camera, clipboard) | Not exercised | A fixture per permission plus a consent-surface design |
| Download *content* inspection | Downloads are refused before content matters | Only relevant if the policy changes |

### Platforms and engines

| Gap | Why | What closing it needs |
| --- | --- | --- |
| macOS, Linux | One machine | A run per platform; window and permission behaviour differs |
| Chromium (non-Brave), Edge, Firefox | `auto` resolved to Brave; Firefox is not a supported engine for this path | A run per engine; the persistent-profile rules are engine-specific |
| High-DPI, zoom, small windows | Not varied | A viewport/zoom matrix |
| Multi-monitor and window placement | Browser tools do not own window geometry | Out of scope for this tool; relevant to the desktop shell |
| Screen readers, high contrast, reduced motion | Not exercised | An accessibility pass over the *app*, not the page |

### Page types

| Gap | Why |
| --- | --- |
| Authenticated sites and real account state | Synthetic data only; WhatsApp messaging has its own consent path and is not exercised here |
| PDFs, DRM video, WebGL/canvas-only apps | Not in scope for DOM-based control |
| Live collaborative editors (Google Docs, Figma) | Canvas-based; would need a different observation model |
| Infinite/very large pages beyond the 2,400-character read cap | The cap is a deliberate token budget; the campaign verified scrolling, not exhaustive reads |
| Slow networks, offline, flaky connections | Only synthesised delays/hangs were used |

### Endurance

| Gap | Why |
| --- | --- |
| Long-duration soak (hours, thousands of actions) | The campaign proves functional stability over 3 navigation cycles and 5 tab cycles, not resource stability |
| Browser crash and session restore | Not exercised |
| Two rooms / two agents sharing the browser concurrently | Not exercised; the tab registry is per process, and the sharing rules are unit-tested, not soak-tested |
| Memory growth per observe | Not measured |

---

## How to read a capability gap

`UNSUPPORTED` means **the product cannot do this today**. It does not mean:

- the capability is broken (that would be `FAIL`);
- the capability is refused for safety (that would be
  `INTENTIONALLY_REFUSED`);
- the environment prevented the test (that would be `BLOCKED`).

The distinction matters for planning: `FAIL` is a bug to fix, `UNSUPPORTED` is a
feature to schedule, `BLOCKED` is a fact about the world.
