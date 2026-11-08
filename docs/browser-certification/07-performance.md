# 07 — Performance

Two scenarios measure latency directly (`TC-17-01`, `TC-17-02`); the rest of the
campaign supplies context. All figures are wall-clock around the tool call, in
her visible Brave window, against loopback fixtures, on a warm session.

---

## Core operations

| Operation | Measurement | Notes |
| --- | ---: | --- |
| Navigation (`open`) | **187.1 ms** | Single warm call, local fixture |
| Observe (`observe`) | **48.2 ms** | Single warm call |
| Act (`click`) | **169.7 ms** | Single warm call |

`TC-17-01` records these as one sample each. `TC-17-02` repeats them four times
and reports the median, which is the more honest number for a budget:

| Operation | Samples (ms) | Median |
| --- | --- | ---: |
| Observe | 41.36, 27.76, 18.83, 33.5 | **30.6 ms** |
| Click | 143.23, 73.86, 69.98, 65.14 | **71.9 ms** |
| Preview screenshot | — | **79.8 ms** |

The single-sample 48.2 ms observe and 169.7 ms click are both above their
medians, which is expected: the first call after a navigation pays for the page's
own settling. The medians are what a conversation feels.

---

## Context from the wider campaign

These are whole-scenario durations, not per-operation latency, and they include
everything the scenario did:

| Scenario | Duration | What it contains |
| --- | ---: | --- |
| `TC-01-01` first navigation | 3,124 ms | Browser launch/warm-up plus navigation |
| `TC-01-02` three redirects | 259 ms | One `open` through `/api/redirect-chain` |
| `TC-01-03` 404 | 227 ms | One `open` |
| `TC-01-05` back/forward | 478 ms | Two history moves plus reads |
| `TC-02-03` read after scroll | 179 ms | Scroll plus two reads |
| `TC-03-01` ordinal ranking | 333 ms | Open, observe, find, act |
| `TC-07-02` below-the-fold read | 194 ms | Scroll plus two reads |
| `TC-07-03` infinite scroll | 1,011 ms | Two bottom scrolls with the page's own fetch |
| `TC-09-01` tab lifecycle | 537 ms | New, list, switch, close |
| `TC-12-01` two captures | 533 ms | Preview JPEG plus full PNG |
| `TC-18-02` five tab cycles | 1,719 ms | Open/close ×5 |
| `TC-19-01` two public pages | 1,843 ms | Two real navigations |

The 3.1 s first navigation is the only number that is large, and it is a
once-per-browser cost (launch plus first paint). It is not a per-turn cost.

---

## Against a voice turn

A spoken sentence is roughly 2–3 seconds. The measured control loop is:

```text
observe 30 ms  +  click 72 ms  +  observe 30 ms   ≈ 132 ms
```

That is negligible against the turn. Even the slowest ordinary operation — a
screenshot at 80 ms, or a full page read at 48 ms — does not add perceptible
latency. The expensive parts of a real interaction are elsewhere: the model's
own turn, a page's network fetch (Google's public search took 6.5 s of scenario
time), and the browser's first launch.

The practical budget conclusion: **the tool is not the bottleneck.** A latency
regression would have to be an order of magnitude before it changed how a
conversation feels.

---

## Stability under repetition

| Test | Result |
| --- | --- |
| `TC-18-01` three dual-page navigation/observation cycles | Completed, no leak, no error |
| `TC-18-02` five tab open/close cycles | **5/5**, registry returned `2 → 2`, no errors, page still usable |
| `TC-07-03` infinite scroll | Rows `40 → 80` |
| `TC-09-04` self-closing popup | Registry returned to its previous size |
| `TC-16-08` five refused navigations | No forbidden load; one error page (NAV-2) |

No memory or process growth was measured — the campaign asserts functional
stability, not resource usage. A long-duration soak is a scope gap.

---

## Caveats

- **One machine, one engine.** Windows 11, Brave 157, this CPU. No claim is made
  for macOS, Linux, Chromium, or a low-power device.
- **Loopback fixtures.** There is no network RTT in the core numbers. Public-site
  durations include the site's own latency and are reported separately.
- **Warm session.** The browser and profile were already running; the 3.1 s
  first-navigation figure is the cold case.
- **One to four samples.** Enough to catch an order-of-magnitude problem, not
  enough for a percentile distribution. A p95 would need a longer run.
- **Scenario durations are not additive.** They overlap with waits for the page's
  own behaviour (debounce, fetch, animation) and should not be summed into a
  throughput figure.
