# Reconciliation — the 2026-10-30 expansion proposal

- **Status:** mapping record, **not an authorisation.** [ADR-0011 no-overbuild](../decisions/0011-no-overbuild.md) still governs: a phase preserves direction, it does not authorise work. Every item below plugs into a phase that must itself be approved before it is built.
- **Origin:** a fourteen-item feature proposal (context & intelligence, reliability, voice, developer infrastructure, security and autonomy), plus a "Unified Execution Runtime" layer diagram and five prioritisation recommendations.
- **Reconciled against:** the accepted architecture audit and the twelve
  decisions of 2026-10-30 — loader ownership, trace identity, cancellation
  scopes, embedded effect ledger, recovery matrix, delegate reaping,
  retention, `filesystem.write`, replay redaction, event-driven eviction,
  provisional thresholds, negative scope. Authoritative resolution of every
  one: [`10-consistency-audit.md`](10-consistency-audit.md).
- **The proposal's own governing sentence**, adopted verbatim as this document's test:

> *Don't make Lumine more autonomous by simply giving her more tools. Make her better at knowing what to do, when to do it, whether it worked, and when to stop.*

And its second, equally load-bearing rule:

> *You already have permission gate, audit logging, seven-state action outcomes and a browser task engine. Don't rebuild these. Extend them into shared runtime services.*

---

## 1. The founding finding, restated

The runtime track's Phase 0 audit found that most of a proposal like this one is
**already built and tested** — that is why the track's Phases 1–5 are
*consolidations*, not constructions. The proposal above is the same shape of
request that founded this track, and most of it receives the same answer.

The shared runtime services the proposal calls for, with their current homes:

| Shared service | Lives in | Since |
| --- | --- | --- |
| Authority + approval + audit | `runtime/permission_gate.py`, `runtime/audit_log.py` (`JSONL` + SHA-256 digests) | built |
| Verification ladder (7 states: `requested → dispatched → accepted → observed` / `failed → timed_out → uncertain`) | `runtime/verification.py` — *the ladder only goes up on evidence; nothing implies `observed`* | built |
| Task model & engine (deps, retry, checkpoints, cancellation, bounded concurrency) | `runtime/task_model.py`, `runtime/task_engine.py`, `tools/task_runner.py` | built |
| Request classification | `runtime/router.py` — pure (request + machine facts → `Route`) | built |
| Event stream & tool narration | `runtime/runtime_events.py`, `runtime/progress.py`, `lumine.tool` / `lumine.permission` / `lumine.notice` | built |
| Result budgeting + injection guard | `tools/tool_results.py` (`MAX_RESULT_CHARS`, key structure, `wrap_untrusted`, `LUMINE_TOOL_DATA`) | built |
| Context budget measurement + trim | `runtime/context_trim.py` (`fixed_prompt_tokens`, history budget) | built |
| Latency tracking | `runtime/latency.py` | built |
| Failure circuit | `runtime/failure_gate.py` + `runtime/llm_errors.py` | built |
| Idempotency precedents | `apps.py` launch dedup (`LUMINE_LAUNCH_DEDUP_SECONDS`), the WhatsApp send ledger, gate collapsing identical pending cards | built |
| Freshness / stale-state | `tools/browser/state.py` snapshot generation, `desktop/capture.confirm_target` | built |
| Failure classification per turn | `runtime/llm_errors.py`; per-session candidate failover: `llm/realtime_fallback_adapter.py` (LiveKit 1.8.3) | built / available |
| Model catalog (single authority on models, modalities, per-model params) | `agent/settings/providers.py` + `settings/validation.py` | built |

The proposal's layer diagram maps onto [ARCHITECTURE.md](../../ARCHITECTURE.md)'s
nine layers **without adopting a second diagram**:

| Proposal layer | ARCHITECTURE layer |
| --- | --- |
| Lumine Intelligence (conversation · router · planner · memory) | 4 Voice Agent + 8 Memory/Context |
| Execution Runtime (context · gateway · tasks · cancellation) | 5 Tool/Action + 7 Task/Planner |
| Safety Layer (authority · approval · audit) | 6 Permission System |
| Reliability Layer (verification · recovery · replay) | 7 Task/Planner + the verification ladder + Phase 4 |
| Capabilities (browser · desktop · messaging · media · coding) | 5 Tool/Action (and Phase 11 for coding) |

### One distinction adopted as-is

> *"Context Manager and Long-term Memory should be separate systems. Conversation history is not the same thing as persistent memory."*

Already encoded by [ARCHITECTURE.md](../../ARCHITECTURE.md) layer 8 — "owns: chat
context budget, **and later: persistent memory**" — and hardened by companion
Phase 7's exit criteria: memory is durable, viewable, deletable, retrieval is
bounded, and **memory never influences a permission decision** (a token-saver
that could change what she is allowed to do would be the worst trade this
codebase could make).

---

## 2. Item by item

**A. Context manager → extends Phase 7 + `context_trim` + Phase 1.5.**

- Relevance-tagged trimming and "important facts vs temporary task context" is
  exactly Phase 7's layers 2/4/5; `context_trim.py` is today's layer 1 backstop.
- Summarising old turns must be **measured** before it is ranked, against the
  Phase 9 finding that GSD's context-rot cures were refused for voice turns —
  *"voice turns are short; there is no 200k window to protect."* What *is*
  expensive here is measured on the money: tool results re-billed per turn, and
  the ~8.5k-token always-sent block that [Phase 1.5](07-capability-discovery.md)
  removes. Summarisation is bounded, local, and charged against the session —
  never a subagent.
- "Remove redundant tool outputs" should reuse `tool_results`' existing
  truncation shape, not invent a second one.
- "Detect when the model needs more information" is Phase 8's
  (LLM-planner-fallback) trigger, expressed through the verification ladder —
  `uncertain` is the vocabulary for "I cannot decide", not a new one.

**B. Intelligent request router → exists; extends Phase 1.5 preloading + Phase 2 routing.**

- `router.py` already classifies by ordered rules, resolves ambiguity by asking
  rather than guessing, and carries `Route.action` so the model is told which
  verb to use. Phase 2's complexity routing adds simple/medium/complex —
  and its constraint stands: *a classifier that costs a model call to answer
  "is this simple?" is worse than the ceremony it avoids.*
- Selecting the tool family is Phase 1.5's preloader (router prediction loads
  the family before the first completion).
- "Escalate to a stronger model when confidence is low" must respect the
  realtime shape: a Gemini Live session cannot swap models mid-flight —
  escalation is a *next-session* decision via handoff (`session.update_agent`),
  or the existing `realtime_fallback_adapter` for turn-level failover. A
  mid-session silent upgrade is the outage that is styled as a feature.

**C. Long-term memory → companion Phase 7 + runtime Phase 7. No new phase.**

Personal memory, project knowledge, past decisions, relevance-based retrieval,
correction and expiry — all phasable within the existing two documents; the A/B
separation above prevents the one architectural mistake here.

**D. Action verification engine → exists as the 7-value ladder; extends per-capability verify predicates.**

The proposal's six-step shape (intended outcome → preconditions → execution →
independent observation → classification → recovery) maps 1:1 onto
`verification.py` + `task_engine` checkpoints + Phase 4's ladder (`may_have_happened`
checked *before* any retry is consulted). What is genuinely new: **domain
verify predicates** become a first-class extension point. The operation tables
already carry a `verify` clause each; the work is richer predicates —
*"clicking a YouTube link is not proof it plays"* — so the click verifies playback
state, not the dispatch. Belongs to Phase 4 alongside the ladder, consumed by
companion Phase 6. **Failure responsibility is one matrix, not one engine:** the
[recovery responsibility
matrix](04-recovery-and-events.md#6-the-recovery-responsibility-matrix)
assigns the provider circuit, the ladder, task restart, worker restart, delegate
reaping and stuck-loops to named owners — a proposal-shaped "Action Verification
Engine" never becomes a second verification system; its *predicates* land in
the existing one.

**E. Unified cancellation → extends Phase 2 (one owner) + already partly built.**

`task_engine` cancels steps; `cancel_task` cancels the current task; LiveKit
cancels a turn by interruption; the delegate stop is Phase 11. What is missing
is exactly the proposal's point — one cancellation intent propagating through
generation, tools, browser, background work, playback and delegation — resolved
by ownership: `AgentRuntime` (Phase 2) is the single **work-cancellation**
owner. With one correction the proposal itself needed and the audit named:

> **Barge-in is turn scope, not work scope, and it never cancels a task.**
> LiveKit's interruption and the runtime's `cancel()` are separate operations
> with separate vocabularies. A cancel *acknowledges*; termination is confirmed
> by evidence, later. [Phase 2 §6](02-agent-runtime.md#6-cancellation--two-scopes-and-acknowledgement-is-not-termination)
> is the canonical statement.

**F. Recovery & intervention → extends Phase 4 with a stuck-loop detector.**

The ladder (`retry → refresh → re-observe → re-plan → fallback → ask user →
abort safely`) already delivers "retry only when safe" and "ask for human
intervention when necessary". Genuinely new: **loop detection** as evidence the
engine consumes — N repeated identical failed actions, unchanged page state
(the browser's snapshot generation can measure it), stale capture generation —
feeding `re-plan` or `ask user`. "Resume from the last verified checkpoint" is
task_engine checkpoints + Phase 3 persistence.

**G. Idempotency & duplicate protection → the effect ledger is **embedded in the Phase 3 task record**; the precedents stay.**

The WhatsApp send ledger (confirmed 10 s / uncertain 5 min hold), the
launch-dedup window, and the gate's identical-pending-card collapse are three
proofs of one pattern — and the proposal's "assign an action id, track
execution status, prevent duplicate sends" is the same shape with one
correction the audit forced:

- the ledger is **embedded in `task_<id>.json`**, not a separate store — one
  write per transition, so a crash cannot separate the task from its effects;
- the `prepared` entry is written **before** the external invocation, and
  `may_have_happened` therefore survives the crash Phase 3 exists for;
- "[reconcile uncertain outcomes before retrying]" is already the law —
  `may_have_happened` → never `retry` — now with persisted state;
- the JSONL audit is already the trail;
- each existing ledger stays authoritative for its protocol and becomes a
  **consumer**, never deleted, never duplicated.

[Phase 3 §5](03-persistent-task-store.md#5-the-effect-ledger-is-embedded-in-the-task-record)
is the canonical statement.

**H. Conversation continuity → companion Phase 4 + companion Phase 3 lifecycle. Already specified.**

Follow-ups without a wake phrase, barge-in, "new request vs continuation", and
resume-after-disconnect are companion Phases 4 and 3, shaped by ADR-0007
(one-shot commands), ADR-0008 (idle ≠ sleeping) and ADR-0013 (conditional
greeting). "Remember the active task during interruptions" rides Phase 3
persistence — an in-flight task must not evaporate because someone barged in.

**I. Adaptive voice pipeline → mostly built; extends per-profile and per-turn knobs.**

`pipeline_factory` already builds per profile from the catalog; `latency.py`
measures; `emotion_voice.py` already controls TTS per turn; `providers.py`
carries per-model overrides (`max_completion_tokens`, `reasoning_effort` — with
the recorded lesson that reasoning tokens eat a capped budget). The genuinely
new parts are a **fast/high-quality dynamic choice** (bounded by the same
catalog, never a second preference list) and free-start TTS barge-in, which is
companion Phase 4's to answer.

**J. Companion presence manager → companion Phase 2. Already specified, two decisions own it.**

The state vocabulary (`IDLE, LISTENING, THINKING, SPEAKING, WORKING, WAITING,
SLEEPING, …`) is the island's total `Record` (a new state fails the build), with
ADR-0005 (single lifecycle authority — *every state a surface renders comes
from the lifecycle machine, never that surface's own inference*) and ADR-0008
(idle is not sleeping; process state and companion state stay separate —
ADR-0012). "Communicate whether she is waiting for approval or executing" is
`waiting` permission cards + `working` tool narration, already on `lumine.*`.

**K. Runtime observability → the measurement half is built; add a trace id now, the dashboard later.**

`latency.py`, `runtime_events`, `progress.py` and the notices already emit the
per-hop facts; what is missing is **one trace id threaded from voice session
through router → planner → tools → result**. Resolved by decision:

- **Phase 5 owns trace identity and it mints it nowhere else** — `task_id`
  stable per task, `trace_id` unique per execution turn, `parent_trace_id`
  optional, `event_id` unique per event ([§6 of the consistency
  audit](10-consistency-audit.md#6-trace-identity)). No phase or module ever
  mints a second copy.
- Until Phase 5 lands, phases publish as they publish today; they gain trace
  ids without code change because the ids attach at the boundary.
- The dashboard is a UI consumer of Phase 5's event contract — it appears on
  the full UI's roadmap, not as a runtime subsystem; no runtime phase will be
  held for it. "Tool selection accuracy" is L's replay product; "reconnection
  frequency" is lifecycle-level, measured where the reconnect happens.

**L. Simulation & replay → the enablement half is Phase 5; the practice is continuous.**

Record the Phase 5 event stream locally (the audit log and `LUMINE_EVENT` are
the two existing precedents), and replay reads it without calling providers —
**under an explicit field allowlist, redacted to the audit log's standard**
(no message bodies, no secrets, no third-party text; payload hashes and sizes
only; [Phase 5 §8](04-recovery-and-events.md#8-recording-replay-and-the-allowlist)).
This codebase already tests by fake: mocked LiveKit tools, `multisession_check.py`,
`broken_stdout_check.py`, fake UIA trees and fake snapshots in the unit suite.
Replay is the same discipline pointed at the event stream: reproduce a failed
tool execution, test interruption/cancellation *against the recorded stream*,
compare models on identical tasks, and catch regressions in tool selection.
Not a phase — a standing duty recorded here so a future maintainer knows where
it starts.

**M. Model capability registry → `providers.py` is already this; extend it. Do not build a second catalog.**

Modalities, per-stage params, validation refusing impossible pipelines, and a
test that keeps the catalog checked against the plugin's own known-models list
all exist. Add: **cost/latency profiles** and a rule that a model lacking a
required capability is *unselectable*, not silently degraded (validation already
refuses video on a text-only stack; generalise that shape). Turn-level fallback
is `realtime_fallback_adapter` territory; route-level is companion Phase 8.

**N. Runtime health supervisor → ARCHITECTURE layer 0 (resident runtime) + `agent_manager.rs`.**

Crashed-worker detection/recovery is companion Phase 3 exit 2; orphaned-task
recovery is Phase 3 + Phase 4; provider-outage detection is `llm_errors` +
`failure_gate` + the notice channel. A **bounded restart policy** must be
measured and published (CPU + memory budget), not assumed — Phase 0's exit
criteria for the companion track already demand published idle budgets, and a
supervisor is the same discipline around recovery counts. Two delegates of
this proposal are resolved by decisions rather than a new subsystem:

- the one "reap an orphan" the runtime will own is **Phase 11's delegate
  reaper**, and its rule is *identity and task ownership, never a pid alone*
  ([ADR-0015](../decisions/0015-coding-delegate.md);
  [Phase 11 §6](08-coding-delegate.md#6-orphan-reaping--identity-and-ownership-never-a-pid));
- retention for anything the supervisor might look at (task evidence, event
  recordings, worktrees) is the **central retention service** Phase 3 owns —
  there is no sweeper per subsystem.

**O. Unified authority model → phase 1 of this track *is* the unified authority model. Extend through it; never fork it.**

| Proposal authority | What exists / where it lands |
| --- | --- |
| Observe | `read` class verbs across `computer.*`, `browser.*`, `filesystem.read` (Phase 6) |
| Interact | `act` / `sensitive` on `computer.*`, `browser.page.*`, `browser.control.*` |
| Communicate | `communication.*` — floors computed from the operation tables (`send_message` arrives `destructive`) |
| Execute | `process.*` (`open_app`, shell-free), `run_task`, plus [Phase 11](08-coding-delegate.md)'s `code.*` |
| Modify | **absent by decision** — Phase 6 is read-only on purpose; scoped writes arrive as their own rows (filesystem.write) with their own classes, *never* as a side effect |
| Administrative | The permission policy keys and `tools.<id>` switches themselves — the config that changes authority, which only a person reaches |

The five-axis evaluation the proposal requests exists with the same five words:
**authority** (`action_class` ceiling per surface), **risk** (`low|medium|high|critical`,
declared, never enforced), **scope** (paths/addressing per family),
**approval** (the gate's classes, session-bound cards), **execution context**
(`element_dependent`, `reads_third_party_text`, the session the gate is bound to).
Anything a tool needs is a new dotted row with a computed class and a verify
clause — not a new policy engine. One proposal item is decided rather than
deferred: the **Modify** axis arrives as
[ADR-0016](../decisions/0016-filesystem-write-scoped-worktrees.md) — a
`filesystem.write` row scoped to Phase 11 worktrees, non-selectable, never a
model-facing verb — and Phase 6 stays read-only.

**P. Trust boundaries & prompt injection defence → built; extend the audit's "why".**

`wrap_untrusted` wraps every retrieved page and every WhatsApp read;
`LUMINE_TOOL_DATA` is guarded in policy; external text is information, never
instruction; password fields are refused structurally before any gate question;
the audit log exists. The extension the proposal names that does not exist yet
is one field, not a subsystem: **the audit record carries why an action was
permitted** (which key, which floor, which session) — added with Phase 5, on
the same audit-compatible discipline the replay allowlist uses.

**Q. Privacy controller → exists in pieces across both tracks; no new runtime layer.**

Local-only mode is companion Phase 8's offline run; per-tool data access is
today's allowlist pattern (WhatsApp history consent, media folder, filesystem
roots); conversation/task clearing and "control what gets remembered" are
companion Phase 7; microphone/screen indications are companion-track surfaces.
The runtime's obligation is the one it already has: nothing in the webview ever
holds a secret, a tool's data boundary is resolved-then-compared, and telemetry
never logs message bodies — the extension moments are the *surfaces*, not a new
authority.

---

## 3. The proposal's priority table, reconciled

The proposal placed its own priorities; corrected where the codebase evidence
moves them:

| Proposal | Placement (as proposed) | Reconciled placement |
| --- | --- | --- |
| Runtime observability | Phase 1.5 | trace id: [Phase 5](04-recovery-and-events.md#7-trace-identity--phase-5-owns-it-nobody-else-mints-it) mints it, sole owner; dashboard: a UI consumer, later |
| Context manager | Companion Phase 7–8 | [Runtime Phase 7](05-capabilities-and-context.md), with the *measured-first* rule above and token budgets in [§2.1](05-capabilities-and-context.md#21-budgets-provisional-pending-measurement) |
| Action verification (engine) | Runtime Phases 2–3 | **already built** (the 7-value ladder); residual work — per-capability verify predicates + loop detection — lands in [Phase 4](04-recovery-and-events.md) under the [recovery matrix](04-recovery-and-events.md#6-the-recovery-responsibility-matrix) |
| Unified cancellation | Runtime Phases 2–3 | [Phase 2](02-agent-runtime.md#6-cancellation--two-scopes-and-acknowledgement-is-not-termination) — one work owner, with **turn scope separate**; companion Phase 4 stays barge-in |
| Intelligent router | Companion Phase 8 | built (`router.py`); extensions in 1.5 + [Phase 2](02-agent-runtime.md) |
| Recovery manager | Phase 6 | [Phase 4](04-recovery-and-events.md) (ladder) — stuck-loop detector extends it |
| Idempotency | Runtime Phases 2–3 | effect ledger **embedded in [Phase 3](03-persistent-task-store.md#5-the-effect-ledger-is-embedded-in-the-task-record)'s task record**; existing ledgers stay until converted |
| Simulation/replay | Continuous infrastructure | recording + allowlist on [Phase 5](04-recovery-and-events.md#8-recording-replay-and-the-allowlist); replay grows from it |
| Unified authority model | Before coding delegation | **exists** as [Phase 1](01-capability-registry.md); [Phase 11](08-coding-delegate.md) adds its family through the same door — and the write axis is [ADR-0016](../decisions/0016-filesystem-write-scoped-worktrees.md) |
| Model capability registry | Companion Phase 8 | extension of `providers.py` (the one catalog); never a second one |
| Privacy controller | Runtime security | cross-track mapping above; surfaces own it, the runtime keeps its existing |
| Companion presence manager | UI/runtime integration | companion [Phase 2](../../ROADMAP.md#phase-2--lumine-island); nothing new |

The five "strongest recommendations" all land inside the existing order:
**Context manager (Phase 7) · Tool gateway (1.5) · Cancellation + recovery
(Phase 2 + 4) · Observability + replay (Phase 5 + continuous) · Coding
delegation (Phase 11)** — and the track's sequencing rules (see
[`ROADMAP.md`](../../ROADMAP.md) "Sequencing rationale") keep the *when*:
cheap-to-be-wrong first, nothing autonomous before the lifecycle is trustworthy.

---

## 4. What this record does not do

1. **Authorise work.** The only approved runtime phase remains Phase 1
   ([CapabilityRegistry](01-capability-registry.md)), and only it has a
   concrete build order.
2. **Create competing subsystems.** Where a proposal names something that
   exists, the existing one is the authority — `providers.py` stays the one
   model catalog, `verification.py` stays the one outcome vocabulary, the
   permission gate stays the one door.
3. **Reverse a decision by re-naming it.** The proposal's ideas that revisit
   recorded positions come with their own ADR at the moment their phase is
   approved — and the two that exist are **filed as drafts, status
   "Proposed — not accepted"**: [0015 coding
   delegate](../decisions/0015-coding-delegate.md) and [0016
   `filesystem.write`](../decisions/0016-filesystem-write-scoped-worktrees.md).
   Neither authorises anything before Phase 11 is approved.
4. **Ship anything new because it is in a document.** Each item ships only when
   its owning phase is approved and its exit criteria are met under that
   phase's tests.
