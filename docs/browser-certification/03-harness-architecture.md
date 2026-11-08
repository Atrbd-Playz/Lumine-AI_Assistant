# 03 — Harness architecture

The campaign is a certification harness, not a unit-test suite. It drives the
real browser through the real tool functions and checks the page afterwards. No
browser call is mocked; the only substitution is the **approval recorder**
described under "Guardrails".

```text
run_certification.py --group N
        │
        ├── test_certification_phases.py   (68 scenarios, one shared event loop)
        │        ├── browse()          ── agent.tools.browser.tools
        │        ├── web_control()     ── agent.tools.browser.control
        │        ├── browser_session   ── the one real browser (CDP)
        │        └── test_server.py    ── local HTTP server(s), 127.0.0.1, ephemeral ports
        │
        └── certification_results.groupN.json

run_certification.py --merge group{1..7}.json
        ├── folds public_compatibility_results.json
        └── certification_results.json + .md
```

---

## Files

| Path | Role |
| --- | --- |
| `check_environment.py` | Records interpreter, packages, browser builds, lockfile hashes, git state. Writes `environment_report.json`. |
| `test_server.py` | Deterministic local HTTP server. Two instances give a genuine cross-origin pair. |
| `fixtures/` | 21 HTML fixtures plus a React bundle built offline with the project's own esbuild. |
| `test_certification_phases.py` | The 68 scenarios. |
| `run_certification.py` | Grouped runner, merge, capability matrix, Markdown report. |
| `run_public_compatibility.py` | The 15-site campaign. |
| `repro/reproduce_defects.py` | Standalone reproduction of the five open findings. |
| `repro/probes/` | The ad-hoc probes used to localise defects while the harness was being built. Kept because each one answers one question more cheaply than a full run. |
| `repro/critical_bugs.py`, `repro/hang_probe.py` | Earlier diagnostics: a defect-focused probe and a faulthandler dump of the group-run hang. Kept as the record of how the hang was found. |
| `evidence/` | Per-group logs, the public log, and `repro_defects.log`. |

The fixtures cover: static elements, forms, keyboard, pointer, scrolling,
overlays, dialogs, downloads/uploads, tabs/popups, dynamic/debounced app, SPA,
React app, failure injection, shadow DOM and iframes (`complex_dom.html`,
`iframes.html`), security, security-advanced (password matrix and dangerous
links), and delimiter injection.

The server exposes `/api/status`, `/api/delay`, `/api/search`, `/api/cors-echo`,
`/api/redirect`, `/api/redirect-chain`, `/api/redirect-loop`, `/api/abort`,
`/api/hang`, `/api/download`, `/api/stream-slow`, `/api/cross-origin-parent`, and
serves `/fixtures/*` from a path it validates.

---

## One browser, joined, not replaced

The certification target is **Lumine's own browser**: the Brave engine, her
persistent profile, a visible window. The first group process launches it; later
groups attach over CDP to the same process. This is deliberate:

- a throwaway profile would answer a different question (no saved settings, no
  extension state, a cold cache);
- a headless run would answer a different question (no visible window, different
  occlusion/rendering behaviour);
- relaunching per group would make seven configurations and one report.

`browser_session.release()` detaches this process's client at the end of a group
and **leaves the browser running**. A window that vanished between groups would
be a different configuration than the one a conversation uses.

The escape hatch is `LUMINE_CERT_PROFILE`; it was not used.

---

## One event loop for the class

This is the single most important harness decision, and it was a bug first.

The first version subclassed `unittest.IsolatedAsyncioTestCase`, which creates a
**new event loop per test**. Playwright objects belong to the loop that created
them. Reusing the browser across tests therefore meant handing a session owned by
loop *N* to loop *N+1*. The visible symptom was a group run that never finished:
the first few scenarios passed, then one await never returned, and the process sat
there with no output.

The fix is `_SharedLoopTestCase`:

- `setUpClass` creates one event loop and keeps it on the class;
- every scenario runs through `_bounded_test`, which is synchronous and calls
  `loop.run_until_complete(asyncio.wait_for(coro, timeout))`;
- `tearDownClass` releases the browser client, shuts down async generators, and
  closes the loop.

A per-test timeout is part of the same rule. A wedged interaction is recorded as
`UNRELIABLE`/`FAIL` with "did not return within N seconds" instead of stopping the
campaign. That is how `drag` was measured in 178 ms rather than hanging the run.

---

## Tab discipline: the run owns only what it created

Her profile can already have tabs open, including the consent-gated WhatsApp tab
and pages the user is reading. The harness enforces four rules:

1. **Baseline first.** Every test records the tabs that existed before it ran.
2. **Work tab.** If any tab pre-existed, the test opens its own blank work tab and
   navigates *that*; it never navigates a pre-existing page.
3. **Teardown by difference.** Everything not in the test's baseline is closed,
   then the original active tab is restored. Late popups are swept in three
   passes, because a popup can register after the first sweep.
4. **`close_others` is conditional.** A scenario that closes every other tab is
   destructive to anything the user had open, so `TC-09-03` records `NOT_TESTED`
   when more than one tab pre-existed. The campaign's run satisfied the
   precondition (it recorded a verified `tabs 4 → 1`).

No test closes the WhatsApp tab and no test navigates a page it did not create.

---

## Determinism and failure injection

- All local scenarios run against `127.0.0.1`. No external service is required
  for phases 1–18.
- Two `LocalTestServer` instances on different ports produce the cross-origin
  case without relying on a public site.
- Failures are requested, not waited for: `/api/abort` (empty response),
  `/api/redirect-loop` (endless redirect), `/api/delay`, `/api/hang`,
  `/api/download`.
- A bounded retry is used where the product has a documented recovery path (a
  navigation after an error page). The retry count is **evidence**, not a hidden
  pass: `TC-01-06` and `TC-01-07` are `UNRELIABLE` precisely because the recovery
  needed a second attempt.
- The public campaign is a separate process and a separate file. It is
  non-destructive by construction: GET navigation, observation, one semantic
  heading lookup.
- Anti-automation interstitials are recorded as `BLOCKED`. The campaign never
  attempts to solve a challenge.

---

## Status mapping

`test_certification_phases.py` declares exactly the seven required statuses:

| Constant | Wire value | Meaning |
| --- | --- | --- |
| `STATUS_VERIFIED` | `PASS` | implemented and verified |
| `STATUS_UNRELIABLE` | `UNRELIABLE` | implemented but not deterministic |
| `STATUS_UNSUPPORTED` | `UNSUPPORTED` | capability absent |
| `STATUS_POLICY_BLOCKED` | `INTENTIONALLY_REFUSED` | refused on purpose |
| `STATUS_DEFECT` | `FAIL` | implementation defect |
| `STATUS_ENVIRONMENT` | `BLOCKED` | environment prevented the observation |
| `STATUS_NOT_TESTED` | `NOT_TESTED` | out of scope for this run |

The public campaign has its own vocabulary and is folded into the same seven
(`UNSTABLE → UNRELIABLE`, `BLOCKED → BLOCKED`, `FAIL → FAIL`, `PASS → PASS`) by
`_fold_public_results`, used by both the run path and the merge path. A merge that
ignored the public file would report 68 scenarios while the machine held 15 more
observations, and phase 19 would look untested on a campaign that had measured it.

---

## Groups and merge

| Group | Phases | Scenarios | In-test seconds |
| ---: | --- | ---: | ---: |
| 1 | 1–3 | 15 | 11.95 |
| 2 | 4–6 | 11 | 24.62 |
| 3 | 7–9 | 11 | 14.61 |
| 4 | 10–12 | 7 | 11.05 |
| 5 | 13–15 | 10 | 12.40 |
| 6 | 16–17 | 11 | 36.76 |
| 7 | 18–19 | 3 | 10.39 |
| | | **68** | **121.78** |

Groups exist because a 68-scenario run in a visible browser is long enough that a
failure near the end costs the whole sample, and because each group's JSON
answers one subject on its own. `--group N` runs one; `--merge` combines them.

The runner exits non-zero only when the suite could not be executed. A `FAIL` is
a **finding**, not runner state: a campaign that stopped at the first defect would
report one defect and hide the rest.

---

## Report generation

`_write_report` writes the Markdown companion. It reads every field with a
default, because a merged summary has no `tests_run` and no single
`elapsed_seconds` — the first version assumed the shape of one run and crashed on
the only document worth reading. The Markdown carries the status counts, the
19-phase capability matrix, an "open defects" section, every scenario with its
classification, and the scope-gap table.

---

## Guardrails

The campaign was required to prove security properties without weakening them.

| Guardrail | How it was respected |
| --- | --- |
| Do not weaken route guards | Loopback is reachable only because `LUMINE_BROWSER_ALLOW_LOCAL=1` is the documented flag. `TC-16-01` and `TC-16-08` set it to `0` and assert the production refusal. No guard code was edited. |
| Do not bypass password restrictions | The password scenarios *measure* the current refusal. They do not patch `_is_password` or skip the check. `TC-16-04` is a `FAIL` because the product typed; the harness did not help it. |
| Do not auto-approve sensitive actions | See below. |
| Do not let page text override instructions | The injection fixtures contain instructions; the harness asserts they stay inside the fence. It does not interpret them. |
| Synthetic data only | Fixtures use literals such as `SYNTHETIC-PROBE-VALUE`; no credential is captured or replayed. |
| No destructive act on a real site | The public campaign is read-only. |

### The approval recorder

`require_approval` is replaced, per scenario, with a recorder that keeps the
`(tool, action_class, summary)` of every card and returns "allowed". Without it a
voice approval question would block every form test forever.

What this does and does not prove:

- **Proves:** the class (`act` / `sensitive` / `destructive`), the verb, the
  summary, and **how many cards** an operation raises. `TC-16-06` and `TC-06-02`
  use the recorder for exactly that, and PWD-1 is visible because the recorder
  counted **3 cards** for password fields that should have raised zero.
- **Does not prove:** the human answer path — that "no" spoken into the room
  denies the action. That path is covered by the permission-gate unit tests and
  is outside a browser campaign. It is listed as a scope gap in
  [08-unsupported-and-scope.md](08-unsupported-and-scope.md).

---

## Discovery

`agent/tests/harness/__init__.py` used to exist. It made the harness a package, so
`python -m unittest discover -s agent\tests -p "test_*.py"` recursed into it and
discovered all 68 scenarios — which launch a visible browser, take several
minutes, and are not part of the unit suite.

The file is **removed**. `agent/tests/harness` is now a namespace subpackage, so
the harness's own imports still work (`run_certification.py` does
`from agent.tests.harness.test_certification_phases import …`), but discovery
skips the directory. Measured before and after:

| | Tests discovered |
| --- | ---: |
| With `harness/__init__.py` | 2,556 (68 of them harness scenarios) |
| Without it | **2,488**, **0** harness scenarios |

The normal suite is green at 2,488 tests (`OK (skipped=1)`).

---

## Known harness limitations

- One machine, one OS, one engine (Windows 11, Brave 157). Cross-platform claims
  would need a second run.
- The approval recorder substitutes the human gate, as described above.
- Native dialogs are answered by the driver's default, so the harness can observe
  the *page's* outcome only; it cannot observe what Lumine would have said.
- Public-site results are time-dependent; markup, geolocation and anti-bot
  behaviour can change between runs. The public JSON records the date.
- The cross-origin iframe fixture uses a second loopback port, which is a genuine
  origin difference but still the same machine and the same browser process.
