# 01 — Environment

This is the measured environment for every record in
[04-results.md](04-results.md). It is written down because a browser
certification is only meaningful against a named browser, a named driver and a
named profile, and because the question "did the harness have to change anything
to make this pass?" has a specific answer: **no.**

---

## Machine and runtime

| Item | Value |
| --- | --- |
| OS | Windows 11, version `10.0.26300` |
| Architecture | AMD64 |
| Python | CPython `3.14.0` (`tags/v3.14.0:ebf955d`, Oct 7 2025, MSC v.1944 64-bit) |
| Interpreter | `.venv\Scripts\python.exe` inside the repository |
| pip | `26.2.1` |
| Virtual environment | repository `.venv`, `in_virtualenv: true` |
| Git | branch `beta`, commit `e36860a32acd89f7c3e750846d79f55aa7ce6e90` |
| Working tree at check time | 3 dirty entries (the harness and docs added by this stage) |

Python packages that the browser path actually loads:

| Package | Version |
| --- | --- |
| `playwright` | `1.63.0` |
| `greenlet` | `3.5.6` |
| `pyee` | `13.0.1` |
| `httpx` | `0.28.1` |
| `beautifulsoup4` | `4.15.0` |
| `pyyaml` | `6.0.3` |
| `livekit-agents` | `1.8.3` |
| `livekit` | `1.1.18` |

The full record is `agent/tests/harness/environment_report.json`, produced by
`check_environment.py`.

---

## The browser under test

**Lumine's own browser, in a visible window, on her persistent profile.** Not a
throwaway profile, not headless.

| Item | Value |
| --- | --- |
| Engine | **Brave** (`auto` resolved to brave because it is installed; `browser_debug.log` records `started brave … (port 11246)`) |
| Brave build | `157.1.99.55` |
| Profile | `%LOCALAPPDATA%\lumine\browser-profile-brave` |
| Window | headed (`LUMINE_BROWSER_HEADLESS=0`) |
| Session | launched once, then **joined** over CDP by later group processes; the window survives between groups |

The last row matters for the evidence. The seven phase groups are seven
processes. The first one launches her browser; the rest attach to it through the
published endpoint (`browser-cdp-brave.txt`). Every group therefore measured the
same browser process and the same profile, rather than seven fresh browsers that
would each have had a different startup state.

The Playwright bundled Chromium (`chromium-1248`, reported by the launch probe as
`153.0.8010.12`) is present and healthy — the probe navigates, reads a title and
text, and speaks CDP 1.3 in `5,859 ms` — but **it is not the certified browser.**
The probe exists to prove the driver and the install work. The campaign itself
runs Brave, because that is the configuration a conversation uses.

`LUMINE_CERT_PROFILE` remains the deliberate escape hatch for certifying a
different profile. It was not used.

---

## Environment variables

At the time of the environment check, every browser variable was empty:

```
LUMINE_BROWSER_HEADLESS    ""
LUMINE_BROWSER_ALLOW_LOCAL ""
LUMINE_BROWSER_PROFILE     ""
LUMINE_BROWSER_ENGINE      ""
PLAYWRIGHT_BROWSERS_PATH   ""
PYTHONPATH                 ""
```

The harness then sets exactly three:

| Variable | Value | Why |
| --- | --- | --- |
| `LUMINE_BROWSER_HEADLESS` | `0` | Certify the visible configuration. |
| `LUMINE_BROWSER_ALLOW_LOCAL` | `1` | The deterministic fixtures are served on `127.0.0.1`. The route guard honours this documented flag; the guard itself is tested with the flag **off** in `TC-16-01` and `TC-16-08`. |
| `LUMINE_BROWSER_PROFILE` | *removed* | An inherited profile override would silently certify a different directory. |

`LUMINE_CERT_PROFILE` is honoured if set, and was not.

---

## Fixture servers

The local campaign binds **`127.0.0.1` on ephemeral ports** — observed
`14825`, `10334`, `4245`, `5289` across different runs — and starts **two**
servers when a test needs a genuine cross-origin pair. Nothing binds a fixed
port, so the campaign cannot collide with Vite's strict `15173`, with the
`1024–15000` range Windows excludes on this machine, or with a running app.

Server behaviour is deterministic: fixed HTML fixtures, fixed JSON endpoints,
and named failure endpoints (`/api/abort`, `/api/redirect-loop`, `/api/delay`,
`/api/hang`) so a failure is *requested* rather than waited for.

---

## Modifications made

**None.** `modifications_made` in the environment report is `[]`.

Specifically:

- no `pip install`, no `playwright install`, no package upgrade;
- no change to `requirements.txt` or any lockfile;
- no change to `agent/tools/browser/`, `agent/runtime/` or `agent/tools/tool_results.py`;
- no change to the stored settings document, the keyring, or any credential;
- no browser flag added to the product's launch path.

The only additions are the files listed in
[03-harness-architecture.md](03-harness-architecture.md), plus the docs in this
directory. `agent/tests/harness/__init__.py` was *removed* at the end of the
stage: its presence made the normal unit-test run discover and launch all 68
browser scenarios. See "Discovery" in that document.

---

## Data hygiene

- Every local fixture is synthetic and purpose-built. Names such as
  `SYNTHETIC-PROBE-VALUE` and `SuperSecret123` are literals in the fixtures, not
  captured credentials.
- The public campaign opens unauthenticated pages, observes them, and performs
  one semantic heading lookup. It does not sign in, submit a form, accept
  consent, send a message, add to a cart, or change account state.
- No fixture is served outside loopback; no real site is used as a form target.
- The 3 `BLOCKED` public results are anti-automation interstitials. The campaign
  records them and stops; it does not attempt to solve or bypass them.

---

## How to re-check the environment

```powershell
.\.venv\Scripts\python.exe agent\tests\harness\check_environment.py
```

It writes `environment_report.json` beside itself and prints a summary. A record
that disagrees with this document means the environment changed, and the
certification should be read as historical rather than current.
