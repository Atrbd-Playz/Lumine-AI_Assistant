# Browser certification harness

Certification evidence for `browse` and `web_control` against Lumine's own
browser. The report lives in [`../../../docs/browser-certification/`](../../../docs/browser-certification/README.md);
this file describes how to run the harness.

Run from the repository root with the repository Python environment:

```powershell
.\.venv\Scripts\python.exe agent\tests\harness\check_environment.py
.\.venv\Scripts\python.exe agent\tests\harness\run_certification.py --group 1   # ... through 7
.\.venv\Scripts\python.exe agent\tests\harness\run_certification.py --merge `
    agent\tests\harness\certification_results.group1.json `
    agent\tests\harness\certification_results.group2.json `
    agent\tests\harness\certification_results.group3.json `
    agent\tests\harness\certification_results.group4.json `
    agent\tests\harness\certification_results.group5.json `
    agent\tests\harness\certification_results.group6.json `
    agent\tests\harness\certification_results.group7.json
.\.venv\Scripts\python.exe agent\tests\harness\run_public_compatibility.py
.\.venv\Scripts\python.exe docs\browser-certification\repro\reproduce_defects.py
```

## What runs

- **The local campaign** (`run_certification.py`) drives the real Playwright/CDP
  path against deterministic fixtures served on `127.0.0.1`. It uses **her own
  browser** — the configured engine, her persistent profile, a **visible**
  window — and joins the browser over CDP rather than launching a throwaway.
  `LUMINE_CERT_PROFILE` is the deliberate escape hatch for certifying another
  profile. It closes only the tabs the run itself opened.
- **The public campaign** (`run_public_compatibility.py`) uses the same browser
  and profile for unauthenticated public pages. It performs GET navigation,
  observation, and one semantic heading lookup. It does not sign in, submit a
  form, accept consent, send a message, purchase, or change account state.
  Automated-access challenges and sign-in boundaries are recorded as `BLOCKED`,
  never treated as successful access.

## Statuses

`PASS` implemented and verified · `UNRELIABLE` implemented but not deterministic
· `UNSUPPORTED` capability absent · `INTENTIONALLY_REFUSED` refused by design ·
`FAIL` implementation defect · `BLOCKED` environment prevented the observation ·
`NOT_TESTED` out of scope for the run.

`UNSUPPORTED`, `INTENTIONALLY_REFUSED` and `BLOCKED` are **not** passes. A
`FAIL` is a finding, not runner state: the process exits non-zero only when the
suite could not be executed.

## Files

| Path | Role |
| --- | --- |
| `check_environment.py` | Environment record; writes `environment_report.json`. |
| `test_server.py` | Deterministic local HTTP server, including named failure endpoints. |
| `fixtures/` | 21 HTML fixtures plus an offline React bundle. |
| `test_certification_phases.py` | The 68 scenarios. |
| `run_certification.py` | Grouped runner, merge, capability matrix, Markdown report. |
| `run_public_compatibility.py` | The 15-site public campaign. |
| `repro/` | Standalone reproductions and the probes used to localise defects. |
| `evidence/` | Per-group logs and the reproduction log. |

## Two rules worth keeping

- **No `__init__.py` here.** This directory is a namespace subpackage on purpose:
  with an `__init__.py`, `python -m unittest discover -s agent\tests -p "test_*.py"`
  recurses into it and discovers all 68 browser scenarios, which launch a visible
  browser. The harness imports still work without it.
- **One event loop per class.** Playwright objects belong to the loop that created
  them. `_SharedLoopTestCase` creates one loop in `setUpClass` and releases the
  browser client in `tearDownClass`; per-test loops are what made a group run
  hang.

These are focused runtime certification campaigns, not a long-duration soak test
or a substitute for the Python unit suite. A passing result applies only to the
cases and environment recorded in `environment_report.json`.
