# Runtime track — dependency graph, acceptance matrix, and consistency audit

- **Status:** documentation reconciliation record. **This document authorises no
  work** ([ADR-0011](../decisions/0011-no-overbuild.md)); the only approved
  runtime phase remains Phase 1.
- **Origin:** the accepted architecture audit plus the 2026-10-30 decisions
  (loader ownership, trace identity, cancellation scopes, embedded effect
  ledger, recovery matrix, delegate reaping, retention, `filesystem.write`,
  replay redaction, event-driven eviction, provisional thresholds, negative
  scope).
- **Rule for every number below:** thresholds are **provisional** until their
  measurement definition *and* a feasibility note exist and are accepted. A
  threshold without both is a target, not a gate.

---

## 1. Final dependency graph

Hard edges only; sequence is shown separately and binds nothing.

```text
Phase 0 ✅ audit
└── Phase 1 ✅ approved (CapabilityRegistry)
    ├── Phase 1.5 (capability discovery)
    │     └── Phase 2 (AgentRuntime; the loader is injected)
    │           └── Phase 3 (persistent task store)
    │                 ├── Phase 4 (RecoveryEngine)
    │                 └── Phase 5 (EventBus + trace identity)
    ├── Phase 6 (read-only filesystem)
    └── Phase 11 (coding delegate)
          needs 1, 2, 3, 4, 5, 6 + ADR-0015 + ADR-0016

Phase 1.5 ──> Phase 7 (selective context; the budget counts the loaded set)
Phase 5   ──> Phase 7 (tool/runtime state events)
Phase 7   ──> Phase 8 (LLM planner fallback)
Phase 5   ──> Phase 9 (research events)
Phase 7   ──> Phase 9 (selective context)
Phase 8  -.-> Phase 9 (sequence, not a hard dependency)
Phase 3   ──> Phase 10 (deferred; also needs companion Phase 3)
Phase 5   ──> Phase 10
```

### Edges and their reasons

| Edge | Kind | Why |
|---|---|---|
| 1 → 1.5 | hard | the catalog is a *view* over the registry |
| 1.5 → 2 | hard | Phase 2 **injects** the session-owned loader; the loader never owns lifecycle |
| 2 → 3 | hard | persistence is written by the runtime, not beside it |
| 3 → 4 | hard | "resume from the last verified checkpoint" reads Phase 3; no checkpoint, no resume rung |
| 3 → 5 | hard | the bus records task events; Phase 4 and Phase 5 are **independent consumers of Phase 3** |
| 4 ↔ 5 | **no edge** | neither imports the other; Phase 4 publishes through the existing publisher until the bus exists |
| 1 → 6 | hard | `filesystem.*` rows are registry rows |
| 1.5 → 7 | hard | once the tool set is dynamic, the context budget must count the *loaded* set |
| 5 → 7 | hard | layer 5 (tool/runtime state) consumes the event contract |
| 7 → 8 | hard | the planner fallback inherits the context discipline |
| 5 → 9, 7 → 9 | hard | research events, selective context |
| 8 -.-> 9 | sequence | the working order, not a dependency |
| 11 → 1, 2, 3, 4, 5, 6 | hard | see [§7](#7-phase-11-dependency-audit) |
| 11 → ADR-0015, ADR-0016 | gate | the phase may not be approved without both records |

**No cycle exists among 1.5, 2, 3, 4 and 5** — the only path is
`1.5 → 2 → 3 → {4, 5}`, and nothing points back.

Topological build order: **1 → 1.5 → 2 → 3 → {4, 5, 6} → 7 → 8 → 9 → 11**,
with Phase 10 deferred. Phase 6 may be built any time after Phase 1; its
position in the number line is ordering, not dependency.

---

## 2. Phase acceptance matrix

Every threshold here is **provisional** per the rule above. "Measurable" means
the definition and the corpus are written here — a number without them is not
accepted even if it is written elsewhere.

| Phase | Measurable exit | Definition / method |
|---|---|---|
| **0** | audit accepted | complete; no implementation |
| **1** | 2232+ tests; all Phase 1 tests pass; 17 cards byte-identical | existing criteria in [01](01-capability-registry.md#9-acceptance-criteria) |
| **1.5** | p95 always-sent block ≤ 1,200 tokens; **0 extra metadata turns** on router-predicted requests; ≤ 1 load turn on a first-use complex family; 0 reconnects in Stage A | `estimate_tokens` at turn start, persona excluded, 50 turns; a 20-request corpus with known routes — count `load_tools` calls (definition: a *metadata turn* is a model turn whose only tool call is schema discovery, never the requested action); 10 families × 3 runs; reconnect count from session events |
| **2** | cancel **ack** p95 ≤ 250 ms; 0 implicit task cancels across 20 barge-ins; a cancel while an approval is pending resolves as a denial 20/20; 0 tasks resume after cancel | ack measured from `cancel()` call to the acknowledgement event; barge-in test asserts no `task.cancelled`; the denial is asserted, not implied; a resume attempt on a cancelled task is refused |
| **3** | 100% restart listing; 0 auto-resumes; 0 torn files in 20 forced-kill trials; step-write p95 ≤ 5 ms; 100% of external effects pre-recorded; 0 secrets under `.lumine/` | atomic temp+rename; kill at random step boundaries; fault injection asserts the `prepared` effect write happens **before** the invocation; filesystem scan |
| **4** | 0 retries when `may_have_happened()` is true; ladder terminates on 100% of inputs; detector fires at exactly 3 identical failures; 100% of rungs reported | property tests over ladder inputs; identical-failure fixture; event assertion |
| **5** | 100% of enumerated event types carry `trace_id` and `event_id`; 0 subscriber failures propagate; 100% of replay fields inside the allowlist; recording bounded | closed-set event test; raising subscriber; secret fixture; retention test |
| **6** | 0 escapes via `..`, absolute path, or symlink; contents wrapped untrusted; oversized file clipped; 0 write verbs | the `whatsapp_files` boundary parity test; a static capability test asserts the absence of write-family rows |
| **7** | layers 2/4/5 p95 ≤ 800 tokens combined; summaries ≤ 200 tokens; 0 memory reads inside a permission decision; ≥ 20% token reduction on long sessions; task success non-inferior | per-turn token measurement; a permission-decision source check; A/B against the baseline corpus — the *provisionalist* row in this matrix |
| **8** | 100% deterministic plans byte-identical; 100% of malformed LLM plans refused; `MAX_STEPS` enforced; no authority-class downgrade | the existing scenario corpus as golden output; validation tests |
| **9** | worker receives 0 conversation history; artefact written once, read many; sources preserved verbatim; 0 permission requests from the worker; retention bound holds | fixtures; artefact read-count; source diff |
| **10** | deferred | no acceptance until scheduled |
| **11** | 0 writes outside the worktree; 0 credential variables in the child env; delegate start idempotent 100%; timeout kill ≤ 2 s past deadline; ≤ 2 repair attempts; orphan reaping 10/10 owned, 0/10 unowned; cancel ack and confirmed termination reported separately | path-escape tests; env scan; duplicate-start test; forced timeout; a fake lease/process table |

---

## 3. Cancellation — one owner per scope

**Decision (2026-10-30): turn interruption and work cancellation are separate
scopes.** They share one vocabulary, not one meaning:

| Scope | What it stops | Owner |
|---|---|---|
| **turn** | the response being generated, TTS, barge-in | the LiveKit session |
| **work** | the task, its steps, its tools, the delegate | Phase 2 `AgentRuntime` — the single work-cancellation owner |

- **Barge-in never implicitly cancels a running task.** An interruption during a
  tool run is handled deliberately (companion Phase 4 exit 3); it is never read
  as `cancel`.
- **Acknowledgement is not termination.** `cancel()` returns a `CancelAck`
  carrying `termination: pending`. Termination is confirmed separately, by
  evidence — the engine's terminal state, a process exit, a delegate's lease
  going dead. Nothing speaks "stopped" on an ack alone, and the audit records
  both the ack and the confirmation.
- A cancel arriving while an approval is pending resolves the approval as a
  **denial** — never a race, never a grant after death.
- The loader changes schemas **only at a turn boundary**; never mid-generation,
  never mid-tool, never while an approval is pending.

### Owners of every async operation

| Operation | Owner | Confirmed-termination evidence |
|---|---|---|
| LLM generation / TTS | LiveKit session (turn scope) | generation stopped / audio ended |
| Tool call / task step | `AgentRuntime` + `task_engine` | engine terminal state |
| Browser navigation / click | Playwright action + `_Browser` release | release returns; page state re-observed |
| WhatsApp send | gate timeout + the domain send ledger | delivery observation **after** the attempt |
| Media playback | the OS player | not hers to stop; documented |
| Prewarm / fire-and-forget | none, by design | failures dropped; no task exists |
| Capability loader | the session boundary | no pending load across the boundary |
| Memory summarisation (Phase 7) | Phase 7, via the same work-cancel intent | summary written or abandoned |
| Delegate process (Phase 11) | `code.cancel` → the process tree | lease + process-identity evidence |
| Event recording / replay (Phase 5) | subscriber drop | bounded, never propagated |

---

## 4. Effects — idempotency and reconciliation

**Decision (2026-10-30): the effect ledger is embedded in the persistent task
record** — one file, one atomic write per transition, no torn pair.

| Effect | Strategy | Ledger |
|---|---|---|
| Task-scoped side effects | `prepared` persisted **before** external invocation; crash renders `prepared`/`dispatched` as `uncertain`; `may_have_happened` survives | embedded in `task_<id>.json` (Phase 3) |
| WhatsApp send | confirmed 10 s / uncertain 5 min hold | domain ledger stays authoritative; the task record links its outcome |
| WhatsApp destructive verbs (delete/block/unlink/call) | verify clause + destructive gate; **re-read before any retry** | domain ledger, extended with the uncertain hold |
| App launch | launch dedup window, released on failure | domain ledger |
| Desktop click/type/press | per-op `hold_seconds` | `ops.OPERATIONS` |
| Browser form submit | `sensitive` class; page state re-observable | — |
| Media play / queue | hash-named queue (idempotent by body) | `tools/media/queue.py` |
| Delegate start | key = task id + prompt hash + project + adapter; a duplicate returns the existing handle, never a second process Phase 11 |
| Worktree create | idempotent by task id; an existing owned tree is a resume candidate, never a second one Phase 11 |
| Event recording | `event_id` dedupe Phase 5 |

`may_have_happened()` is checked **before** the recovery ladder is consulted,
and a `prepared` effect that never got past the write is conservatively
`uncertain` — the safe direction, because it blocks a retry that might have
been harmless but cannot un-send.

---

## 5. Persistent state — restart, crash, and retention

| Store | Owner | Crash semantics | Retention |
|---|---|---|---|
| `.lumine/tasks/*.json` | Phase 3 | atomic temp+rename; `active/` load = `interrupted`; no auto-resume; corrupt file quarantined and reported | **retention service** |
| Effect records | Phase 3, embedded | same write, same file | same as the task |
| Audit JSONL | `permission_gate` | append; a partial last line is skipped; never rewritten | retention service |
| Event recordings | Phase 5 | append with `event_id`; partial line skipped; replay reads the allowlist only | retention service |
| Worktrees | Phase 11 | retained on `uncertain`/timeout/cancel; pruned only after the artefact is written and completion confirmed; orphan reaping at worker start | retention service |
| Artefacts / research digests | Phase 9 / 11 | written once, read many | retention service |
| Browser profile + CDP endpoint | existing | stale lock/endpoint already handled | n/a |

**Decision (2026-10-30): retention is centralised, configurable, and bounded.**
`runtime/retention.py` is the one sweep; stores **register descriptors**, they
do not invent their own policies or their own sweeper.

Proposed defaults — **provisional, pending your approval**:

| Key | Default | Note |
|---|---|---|
| `retention.tasks.completedDays` | 30 | |
| `retention.tasks.failedDays` | 90 | |
| `retention.tasks.uncertainDays` | 180 | an uncertain task is evidence, not garbage |
| `retention.tasks.maxCount` | 500 | |
| `retention.tasks.maxBytes` | 50 MB | |
| `retention.events.days` | 14 | |
| `retention.events.maxBytes` | 100 MB | |
| `retention.artifacts.days` | 30 | |
| `retention.artifacts.maxBytes` | 200 MB | |
| `retention.worktrees.days` | 14 | after the terminal state |
| `retention.worktrees.maxCount` | 20 | |
| `retention.reviewedHoldDays` | 30 | "actively reviewed" |

**Never auto-deleted:** an `uncertain` task, a task under active review
(`reviewed_at` inside the hold window), anything containing an unresolved
effect. Deletion is a user act or a bounded sweep — never a voice-path timer.

---

## 6. Trace identity

**Decision (2026-10-30): Phase 5 owns trace identity.** Nobody else mints
identity, and no phase documents a second vocabulary:

| Id | Lifetime | Minted by |
|---|---|---|
| `task_id` | stable for the whole task; persisted; names worktrees, effect records, reports | Phase 2/3 at task creation |
| `trace_id` | unique per **execution turn**; spans router → planner → tools → permission → result | Phase 5, at the turn boundary |
| `parent_trace_id` | optional; set when work is spawned by another trace (research worker, delegate) | Phase 5 |
| `event_id` | unique per event; the dedupe key for recording and replay | Phase 5 |

Before Phase 5 exists, phases publish through the existing publisher and carry
no trace ids; when Phase 5 lands its ids attach at the boundary. The rule is
that **no module other than Phase 5's may mint trace identity** — a test
asserts it, because a second minter is a second lifecycle of the same failure
kind as a second authority.

---

## 7. Phase 11 dependency audit

| Needs | Provided by | Verdict |
|---|---|---|
| `code.*` rows | Phase 1 | ✅ registry rows |
| cancellation | Phase 2 | ✅ the single work-cancel owner; ack/termination rules |
| task + effect persistence | Phase 3 | ✅ tasks **and** the embedded effect ledger |
| crash recovery | Phase 4 | ✅ checkpoints + restart semantics |
| progress events / trace | Phase 5 | ✅ delegate event names ride the same contract |
| path discipline | Phase 6 | ✅ **path discipline only** — Phase 6 is read-only and cannot satisfy a write need |
| write authority | **ADR-0016** | ✅ `filesystem.write` scoped to worktrees, non-selectable, never a model-facing tool |
| delegation authority | **ADR-0015** | ✅ propose-only, human-merge-only, worktree-isolated |

The island (companion Phase 2) is a *surface* for delegate progress, not a
dependency.

---

## 8. ADR audit — what the reconciliation preserves

| ADR | Finding |
|---|---|
| 0005 lifecycle authority | **Preserved, clarified** — `toolset_loaded` rides `lumine.tool`, never `agent_state_changed`; the lifecycle machine stays the only companion-state authority |
| 0010 layer ownership | **Preserved, clarified** — the loader is session-owned and event-driven; it never imports or polls task state upward |
| 0011 no overbuild | Preserved — documents do not authorise work |
| 0012 process vs companion state | Preserved — a crash is `interrupted`, never `SLEEPING` |
| 0014 canonical namespace | **Preserved, clarified** — `use_tool` is a dispatch surface, not a second user-facing taxonomy; ids stay dotted; cards unchanged |
| 0001–0004, 0006–0009, 0013 | untouched |
| **0015 coding delegate** | **new draft, proposed** — [file](../decisions/0015-coding-delegate.md) |
| **0016 filesystem.write** | **new draft, proposed** — [file](../decisions/0016-filesystem-write-scoped-worktrees.md) |

No existing ADR is contradicted. Where the expansion proposal revisited a
recorded position, the proposal *supplicates*, not amends: the ADR is the
amendment, at approval time.

---

## 9. Blockers — decisions still requiring approval

1. **Phase 1.5 approval** and the `2 → 1.5` dependency (loader injection).
2. **Cancellation thresholds** — 250 ms ack, 2 s termination, 5 s delegate.
3. **Effect-ledger schema** — field names, the `prepared` state, and the ≤ 5 ms
   pre-invocation write budget.
4. **Retention defaults** — the table in §5; these are policy numbers, not
   engineering defaults.
5. **Stuck-loop threshold** — N = 3 identical failures, and the definition of
   "unchanged state."
6. **Trace schema** — `trace_id` / `parent_trace_id` / `event_id` names; Phase 5
   as the sole minter.
7. **ADR-0015 and ADR-0016** — approve as proposed; confirm `filesystem.write`
   is `sensitive`, non-selectable, and `code.delegate` is `destructive`.
8. **Phase 7 graph edges** — hard dependency on 1.5 and 5, with 6 as sequence.
9. **Phase 11 unverified-orphan policy** — if process identity cannot be proven,
   leave it alive, mark `uncertain`, and ask the user; never kill by pid alone.
10. **Negative-scope canon** — confirm the exact heading and that renaming
    Phase 1's "Explicit non-goals" is a heading-only documentation change.

---

## 10. The negative-scope canon

The heading, spelled exactly, with the rule of assembly:

```text
## N. Negative scope — must not implement
```

Rule: one section per **phase section** (Phase 4 and Phase 5 get one each; so
do Phases 6/7/8 and 9/10 inside their shared files), naming the adjacent-phase
features by phase number **and by name**, so a scope creep has to walk past
the list to happen. A "Non-goals"/"Do not" heading may exist beside it; the
canonical section is the one every future grep finds.

Current index — 11 sections, every phase 1–11:

| Phase | Where |
|---|---|
| 1 | [01-capability-registry.md §10](01-capability-registry.md) |
| 2 | [02-agent-runtime.md §10](02-agent-runtime.md) |
| 3 | [03-persistent-task-store.md §11](03-persistent-task-store.md) |
| 4 | [04-recovery-and-events.md §9](04-recovery-and-events.md) — Phase 4 half |
| 5 | [04-recovery-and-events.md §10](04-recovery-and-events.md) — Phase 5 half |
| 6 | [05-capabilities-and-context.md](05-capabilities-and-context.md) — Phase 6 half |
| 7 | [05-capabilities-and-context.md](05-capabilities-and-context.md) — Phase 7 half |
| 8 | [05-capabilities-and-context.md](05-capabilities-and-context.md) — Phase 8 half |
| 9 | [06-research-and-proactive.md §6](06-research-and-proactive.md) |
| 10 | [06-research-and-proactive.md §5](06-research-and-proactive.md) — binding **even deferred** |
| 11 | [08-coding-delegate.md §10](08-coding-delegate.md) |
