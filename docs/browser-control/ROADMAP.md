# Browser Control Subsystem — Roadmap

Status: page-awareness slice delivered; the six phases below remain proposed.
Each proposed phase is gated on the full suite staying green and its
`TEST_PLAN.md` rows passing. One phase at a time
([ADR-0011](../decisions/0011-no-overbuild.md)) — the roadmap preserves
direction, it does not authorise skipping a gate.

The phases order by dependency, not by ambition: the taxonomy (1) is what later
phases classify failures with; the schema (2) is what later verbs validate
against; intervention (3) is what a crashed browser (1) escalates *into*;
coordinates (4) and transfers (5) are independent of each other but both depend on
the classified errors of (1); the activity panel (6) renders what (1)–(5) emit.

## Delivered slice — page awareness

- `state.py` provides the shared visible-control and page-item snapshot used by
  `browse` and `web_control`, including grounded video/product cards, prices,
  destination links, and native select choices.
- Item-specific selectors precede generic list selectors, preventing navigation
  rows on a large shop from filling the bounded card report. A Chromium fixture
  places 24 navigation entries before product cards to pin this behavior.
- `locators.py` and `engine.py` ground semantic targets to those observed card
  titles; actions still use fresh refs and report the observed post-action state.
- Verification: 498 browser tests passed; `agent/tool_catalog.py --check` passed.
  Public YouTube and Amazon journeys through Lumine's browser code are recorded
  in `TEST_PLAN.md`.
- `AUDIT.md` remains the measured baseline for the earlier clean tree; its counts
  are historical, not the current suite size.

## Phase 0 — audit and design ✅

The original audit and design deliverables are the five documents in this folder.
The user's later-approved Observe → Ground → Act → Verify implementation is
recorded above and in `ARCHITECTURE.md`; it did not authorize implementing the
unrelated future phases below or weakening the preserved security boundaries.

## Phase 1 — engine completion

**Builds:** `errors.py` (one `ErrorKind` taxonomy, every raise site mapped);
shadow-DOM piercing (open roots, depth-capped, closed roots refused as a consent
boundary); frame-aware refs for `browse` (`frame=` parity with `web_control`);
crash/restart recovery (classify as `crashed`, rejoin by default, relaunch only as
the `_OwnerLock` owner, all refs stale after recovery).

**Depends on:** nothing — Phase 0's review.

**Gate:** `TEST_PLAN.md` §Phase 1 green; the 457-test baseline unchanged and green;
a shadow-free-page snapshot diff test proving no regression in existing snapshots;
the guest-never-launches assertion green.

**Deliberately not built:** no replan (that is Phase 3 consuming these `crashed`
signals); no new verbs; no context-flag change.

## Phase 2 — contract and schema hardening

**Builds:** a single verb enum in `actions.py` as the one vocabulary authority
(derived from the current parsers' accepted sets, so nothing that parses today
breaks); DDG parser hardening in `search.py` with a loopback fixture corpus and a
fallback chain, run over the same corpus as `web_search.py`; a model-generated
tool-call corpus replayed through every argument parser.

**Depends on:** Phase 1 (classified refusals are the parse-failure shape).

**Gate:** `TEST_PLAN.md` §Phase 2 green; `tool_catalog.py --check` passing; the
Tools-page accent minority test still passing; no verb added, so no policy-text
change.

**Deliberately not built:** no external search API (user decision — DuckDuckGo
HTML endpoint stands); no parser rewrite for other engines.

## Phase 3 — intervention and replan

**Builds:** `awaiting_intervention` task state with a total transition table;
bounded hold (same 60 s default as the gate) publishing one `lumine.tool` step
record; resume detection via the authenticated-transcript channel with a mandatory
re-observe before resuming; a `plan_for` replan scenario for `crashed`/`refused`
step failures, bounded to one replan per task by default.

**Depends on:** Phase 1 (`crashed` is the trigger's input).

**Gate:** `TEST_PLAN.md` §Phase 3 green; the login-wall fixture resumes on fresh
refs and never acts on a pre-hold snapshot; the planner call-count assertion
proving no loop.

**Deliberately not built:** no LLM planner (runtime-track Phase 8); no
cross-task intervention memory; no change to approval semantics.

## Phase 4 — coordinate clicks behind a freshness token

**Builds:** the viewport-signature token (`tab, generation, url, viewport+DPR,
scroll, layout digest, captured_at`); same-tick verification before dispatch;
`COORDINATE_MAX_AGE` (15 s default); role+name reporting of what the coordinates
actually hit; coordinate as fallback only — the ladder still wins, and the
password refusal still precedes everything. `MIGRATION.md` §Supersession 1 is
discharged here.

**Depends on:** Phase 1 (the `stale` kind).

**Gate:** the freshness matrix in `TEST_PLAN.md` §Phase 4 — all seven
perturbations refuse, the no-change case clicks the right element; the
ladder-precedence interception test green.

**Deliberately not built:** no coordinate *drag* as a first-class verb in this
phase; no screenshot-diff targeting; no ML object detection.

## Phase 5 — transfers

**Builds:** `transfers.py` (shared allowlist — resolved-then-compared, extension
allowlist, size cap — imported from the `whatsapp_files.py` shape, not
reimplemented); the download interceptor (discard-by-default, single-use token
bound to tab + URL pattern + destination, expiry); `accept_downloads` flip with
the interceptor-first ordering from `MIGRATION.md` §Supersession 2; `upload`
verbs on `browse` and `web_control` at `sensitive` class; the WhatsApp-path
reason-text rewrite and its new discard test.

**Depends on:** Phase 1 (`crashed`/`refused` classification for transfer
failures); Phase 0's supersession approval (already granted).

**Gate:** `TEST_PLAN.md` §Phase 5 green — the traversal trio, the hostile-page
auto-download discarded, the WhatsApp-tab download discarded with the conversation
untouched, `test_whatsapp_safety.py` strengthened and green,
`test_browser_lifecycle.py` rewritten to assert interceptor+flag together with the
`MIGRATION.md` citation.

**Deliberately not built:** no bundled downloader; no auto-open of a saved file;
no attachment of a downloaded file to any conversation; no relaxation of the route
guard (a download's *source* navigation still faces it).

## Phase 6 — activity records and panel

**Builds:** `browser_activity` records on the existing `lumine.browser` topic
(verb, target, outcome kind, duration; additive type the old frontend ignores);
`BrowserPreview.tsx` renders the activity list, current URL, and tab count beside
the screenshot; a record-volume cap test so the panel never floods.

**Depends on:** Phases 1–5 (it renders their emitted records).

**Gate:** `TEST_PLAN.md` §Phase 6 green; `npm run build` in `lumine-ui/` green; a
frontend fixture proving an unknown record type changes no state.

**Deliberately not built:** no new topic; no new permission surface; no
settings-page section (the browser has no filename to browse — same reasoning as
the music widget).

## Reporting contract

After each phase, the report to the user names, per the standing instruction:

1. Files inspected and changed, with reasons;
2. Tests added and their results, plus the full-suite count vs the 2,426 baseline;
3. Any remaining failure or skip, with cause;
4. Any decision needing approval *before* the next phase starts — a phase never
   begins on an unanswered question, and a supersession never ships without its
   `MIGRATION.md` entry being written first.

## What closes this track

All six phase gates green, the two supersessions discharged with their tests, and
a final full-suite run at or above the baseline count with zero failures. The
subsystem at that point has: one error taxonomy, shadow and frame reach, crash
recovery that cannot kill another room's browser, human-in-the-loop task resume,
bounded replan, coordinate targeting that cannot outlive its screenshot,
allowlisted up/downloads that cannot weaken the WhatsApp boundary, and an activity
panel — every gap from `AUDIT.md` closed, none of the recorded refusals lost that
was meant to survive.
