# Browser Control Subsystem — Phase 0 Audit

Status: historical baseline, complete and read-only at `e5adcb7`. The audit counts
and gaps below describe that clean tree, not the current implementation. The
page-awareness slice delivered afterward is summarized in `ARCHITECTURE.md` and
verified in `TEST_PLAN.md`; proposed gaps not addressed by that slice remain open.

## Scope and method

Every module in `agent/tools/browser/` was read end to end, plus the modules it
reaches into: `runtime/router.py`, `runtime/task_model.py`, `runtime/task_engine.py`,
`runtime/permission_gate.py`, `runtime/progress.py`, `runtime/verification.py`,
`tools/task_runner.py`, `tools/tool_results.py`, `tools/web_search.py`,
`tools/messaging/whatsapp_web.py`, `tools/messaging/whatsapp_files.py`,
`tools_policy.py`, and the frontend chain `voice-manager.ts` →
`useLumineVoice.ts` → `Home.tsx` → `VisionDeck.tsx` → `BrowserPreview.tsx`.
Baselines were measured, not assumed:

| Measurement | Result |
| --- | --- |
| `agent/tools/browser/` | 12 modules, **8,609 lines** |
| Browser test files | 13 files, **457 tests, OK** |
| Full suite | **2,426 tests, OK, 3 skipped**, ~204 s |
| Dependencies | `playwright>=1.49`, `pyyaml>=6.0`, `httpx`, `beautifulsoup4` — all already present |
| Working tree at audit | clean, at `e5adcb7` |

## Verified inventory

| Module | Lines | Owns |
| --- | --- | --- |
| `session.py` | 2,645 | The shared browser: CDP endpoint publication/join, profile lock, tab registry, route guard, `_hid_window`/`_reveal`, ref minting, navigation, `MAX_WORK_TABS = 6`, WhatsApp bridge (`wa_lock`, `_reserve`, `_adoptable_blank_page`) |
| `state.py` | 923 | `SnapshotStore`: per-tab document generations, element registry, `MAX_PREVIEW_CHARS = 1_400_000`, `MAX_PREVIEW_BYTES = 900 KB`, `MAX_MODEL_IMAGE_BYTES = 320 KB`, the url+scroll+text read digest |
| `actions.py` | 969 | `parse_action` (the one verb parser), `TAB_ACTION_CLASS`, ref validation, click/type/press/scroll/select/setvalue/back/forward/zoom/wait, `read` = `observe` + `tab_context` |
| `control.py` | 982 | `web_control`: semantic snapshot-scoped sibling, `frame=` argument, `pagetabs`, `read`, `state`, `youtube` |
| `tools.py` | 802 | Tool functions and docstrings the model reads: `browse` (open/observe/act/back/forward/scroll/tab/close), argument parsing, `tool_results` wrapping |
| `locators.py` | 560 | Role+name ladder, ref minting into the live DOM, candidate resolution, **the deliberate refusal of coordinate clicks** |
| `engine.py` | 509 | `Engine` protocol + `PlaywrightEngine`: `NAV_TIMEOUT_MS = 15000`, `ACTION_TIMEOUT_MS = 6000`, per-operation retries |
| `youtube.py` | 327 | Watch verdict (assembled vs read), duration parsing |
| `search.py` | 323 | In-browser `browse search`: DuckDuckGo `html` endpoint, `_parse_results`, `SEARCH_PAYLOAD_CHARS = 1700`, `MAX_RESULTS = 4` |
| `site_map.py` | 236 | `sites_map.yaml` loader, `MAX_HINTS = 12`, `MAX_HINT_CHARS = 200` |
| `sites.py` | 159 | Per-site search-box table (`input`/`submit`/`results`), semantic fallback |
| `preview.py` | 174 | `publish_preview` / `show_to_model` on topic `lumine.browser`, `ImageContent`, the chat-context attribute fallback |

Surrounding runtime, all already built and load-bearing:

- **Router** — `runtime/router.py`: evidence-ordered rules, `Route` with an `action`
  field, `TAB_WORDS` returning before the window rule, `DESKTOP_SITE_APPS` vs
  `WEB_FIRST_SITE_APPS`, `MEDIA_ENVIRONMENT` at step 4b, ask-on-tie, LLM fallback
  for background environments only.
- **Task engine** — `task_model.py` / `task_engine.py` / `task_runner.py`:
  `Task`/`Step`, dependencies, retry, cancellation, checkpoint,
  `STEP_ACTION_CLASS` (`desktop_act = sensitive`), `plan_for`'s deterministic
  scenario table, `runtime/verification.py`'s seven-state ladder.
- **Permission gate** — `runtime/permission_gate.py`: session-bound approvals,
  60 s default timeout, `CLASS_FLOOR`, duplicate suppression, JSONL audit,
  voice announcement plus UI card on `lumine.permission`.
- **Progress** — `runtime/progress.py`: step narration on `lumine.tool`, must-never-raise.
- **Frontend** — `BrowserPreview.tsx` renders `browser_preview` records arriving on
  `TOPIC_BROWSER = "lumine.browser"` (`voice-manager.ts:276`), forwarded through
  `onBrowserPreview` → `Home.tsx` → `VisionDeck.tsx`. It exists and works.
- **WhatsApp path** — `whatsapp_web.py` drives the *same* `_Browser` singleton over
  CDP; `whatsapp_files.py` refuses URL attachments *because* `accept_downloads=False`
  on the shared context; `set_input_files` at `whatsapp_web.py:2993` is the only
  file-input use in the tree; `_hid_window`/`_reveal` guard user-visible windows.
- **Suppression** — `SUPPRESSED_BY_DESKTOP_INPUT = ("browse", "web_control")`: the
  Tools-page switches revoke reachability without deregistering.

## The brief, checked against the code

The starting brief was written from incomplete knowledge. Corrected here, because
the redesign must not be scoped against a fiction:

| Brief claim | Reality |
| --- | --- |
| "no structured event/telemetry for browse actions" | `progress.py` narrates steps on `lumine.tool`; `preview.py` publishes `browser_preview` on `lumine.browser`; both work today |
| "no intervention/resume state" | True for the *task model* (`task_model.py` has failed/cancelled but no `awaiting_user`); false for the browser itself, which already refuses-and-reports per operation |
| "no shadow DOM handling" | True — zero `shadowRoot` traversal in `locators.py` or `state.py`; only a passing mention in a `test_browser_engine.py` comment |
| "no replan triggers" | True at the task-engine level; the router's LLM fallback covers background environments only, never deterministic plan regeneration |
| "no coordinate actions" | True, and **deliberate**: `locators.py` documents why coordinates are not a fallback |
| "no browser-level error taxonomy" | `EngineError` exists with a `kind` and message, but the model receives prose, not a classified reason it could act on deterministically |
| "no file upload/download" | True for the browser tools; `set_input_files` exists only on the WhatsApp path, scoped to its allowlist |

## The ten genuine gaps

1. **No shadow-DOM piercing.** `locators.py`'s role ladder and `state.py`'s element
   collection stop at a shadow root. Any site rendering controls inside one
   (web components, several large frameworks' defaults) is invisible: `observe`
   returns the host element with no children and `act` cannot address them.
2. **`browse` has no frame refs.** `web_control` takes `frame=`; `browse`'s refs are
   main-frame only. An iframe-hosted control is unreachable on the cheaper tool,
   so the model escalates to the expensive one or fails.
3. **No crash/restart recovery.** `engine.py` retries an *operation*; nothing
   restarts a *browser* that died mid-task, and the guest-vs-host distinction
   (`_close_remote(shared=True)` must never stop the host's browser) means a naive
   restart would kill the WhatsApp conversation in another room. Recovery must be
   a task-engine concern with a replan trigger, not an engine loop.
4. **No error taxonomy reaching the model.** Timeouts, navigation refusals (the
   route guard), missing refs, ambiguous candidates and crashes all arrive as
   prose. A model cannot choose between "retry", "re-observe", and "give up"
   deterministically without a classified reason.
5. **No browser activity UI beyond the screenshot.** `browser_preview` is one
   record type; step-level `lumine.tool` records exist but the panel draws no
   action history, no current-URL state, no tab list.
6. **No intervention/resume state.** A step that needs a human (a login, a CAPTCHA,
   a permission card declined-then-reconsidered) can only fail. The task model has
   no `awaiting_intervention` state and no resume detection.
7. **No replan triggers.** `plan_for` runs once. A deterministic engine step failure
   that a human would answer by *changing the plan* (not retrying the step) has no
   path back to the planner.
8. **No coordinate clicks.** Deliberate refusal, now superseded by approval — but
   only behind a freshness guarantee the current generation counter cannot give
   (see the safeguard below).
9. **DuckDuckGo parser fragility.** `_parse_results` in `search.py` is tested only
   against one saved real-page fixture (`test_browser_search.py`); no local
   loopback fixture exercises the fetch path, and no fallback exists if the
   markup shifts.
10. **Schema and cold start.** Verb vocabularies are parsed in four places
    (`parse_action`, `sites.py`, `control.py`, `tools.py`) with no single enum
    authority; the browser cold start is several seconds and is only pre-warmed
    for WhatsApp (`prewarm_whatsapp`), never for `browse`.

## Deliberate refusals that must survive, or be superseded in writing

This codebase records its refusals on purpose. Every one was checked:

| Refusal | Where | Verdict for this track |
| --- | --- | --- |
| No coordinate clicks as fallback | `locators.py` docstring | **Superseded in writing** (user approval), behind a freshness token — see `ARCHITECTURE.md` §Coordinate clicks |
| `accept_downloads=False`, context-wide | `session.py` context setup; asserted by `test_browser_lifecycle.py:324`; the stated reason `whatsapp_files.py` refuses URL attachments | **Superseded in writing** (user approval), only with the WhatsApp-preservation guard — see `ARCHITECTURE.md` §Transfers |
| No model-authored JS | `tools.py` docstring | **Survives.** `execute` is absent from `browse`; `act` cannot run arbitrary script |
| Route guard: http(s) only, no `user:pass@`, loopback blocked without `LUMINE_BROWSER_ALLOW_LOCAL` | `session.py` | **Survives unchanged** |
| Password-field structural refusal | `locators.py`, shared with desktop gate | **Survives** |
| `MAX_WORK_TABS = 6` with a refusal, never silent eviction | `session.py` | **Survives** |
| Guest never closes/relaunches the host browser | `session.py::_close_remote` | **Survives, and constrains any recovery work** |
| WhatsApp `PATH_PREFERENCE = ("web",)` | `messaging/ops.py` | Out of scope; untouched |

## Cross-subsystem couplings any change must respect

1. **One browser, many agents.** `_Browser` is a process-wide singleton joined over
   CDP. A context-level flag change (`accept_downloads`) is visible to the WhatsApp
   path in the same process and to every other LiveKit room. Changes here are not
   local.
2. **The `lumine.browser` topic** is consumed by `voice-manager.ts`; its record
   shape (`type: "browser_preview"`) is checked by the frontend before use. New
   record types are additive; changed shapes are breaking.
3. **`SUPPRESSED_BY_DESKTOP_INPUT`** means browser tool reachability already depends
   on an unrelated switch. New verbs must be added there or they will appear when
   desktop input is off.
4. **Tool policy token budget** is ~4.7k per completion (`tools_policy.py`); new
   verbs and refusal sentences pay rent on every model call.
5. **`STEP_ACTION_CLASS`** prices browser steps as `act`; any new step type that
   can touch a password field or send data outward must be classed at least
   `sensitive`, matching `desktop_act`.
6. **ADR-0014** governs the capability namespace; new settings keys for downloads or
   coordinates must follow it (`desktop.*`, `tools.*`, `browser.*` as published).

## Conclusion

The subsystem is not missing foundations — it has an engine protocol, a ref/generation
identity model, a permission gate, a task engine with a verification vocabulary,
progress telemetry, a frontend preview panel, and a 457-test suite. The ten gaps
above are real and worth closing, but every one is a completion of the existing
design, not a replacement of it. The two approved supersessions (coordinates,
downloads) are the only changes with cross-subsystem blast radius, and both carry
explicit guards in `ARCHITECTURE.md`.
