# Phase 1.5 — Capability discovery (deferred tool exposure)

- **Status:** specified, **not approved.** Consumes Phase 1
  ([CapabilityRegistry](01-capability-registry.md)) and cannot start before it.
- **The loader is session-owned** ([decision, 2026-10-30](10-consistency-audit.md#6-trace-identity)):
  one loader per session, injected into `AgentRuntime` in Phase 2, owning
  *schema state* and nothing else. It never owns lifecycle, never restarts a
  session, never answers a "what is running?" question. When the session ends
  the loader dies with it — unloaded families are gone, and that is correct:
  schema state is ephemeral, task state ([Phase 3](03-persistent-task-store.md))
  is what survives.
- **Depends on Phase 1 only.** No Phase 2, no Phase 5 — it emits `toolset_loaded`
  through the existing publisher and mints no trace ids
  ([§8 below](#8-eviction--observability--event-driven-never-task-aware));
  trace identity arrives with the bus.
- **Origin:** user request, 2026-10-30 — *"Don't expose every tool with full
  context right away. She has a list of every tool; when she needs one she gets
  only that tool and its related context."*
- [ADR-0014](../decisions/0014-canonical-capability-namespace.md) stands
  unchanged: **tools are not split**. This phase changes *when* a schema is
  present, never how many tools exist.

---

## 1. The measured problem

Everything below was measured on 2026-10-30 against the live registry, and an
implementing agent should re-measure the same way (via
`runtime/context_trim.py::estimate_tokens`, the estimator this codebase already
counts budgets with — not with a different one):

| Always-sent component | Chars | est. tokens |
| --- | --- | --- |
| `tools/tools_policy.py::TOOL_USAGE_POLICY` | 11,712 | ~2,900 |
| 17 tool docstrings (the schemas) | 22,223 | ~5,550 |
| **Total, per completion** | | **~8,500** |

`tools_policy.py`'s docstring claims "**~4.7k tokens**" for instructions plus
schemas. **That number is stale** — it predates the registry growing to 17
tools (WhatsApp verbs, the music library). Under `test_llm_runtime.py` the file
pins its size, so the fix is precise, not editorial.

Three facts from the installed source (LiveKit Agents **1.8.3**) decide the
design:

1. **The gate lives inside each tool.** `permission_gate.require_approval` is
   called by every tool implementation (`tools/apps.py:34`,
   `tools/desktop/control.py:71`, `tools/browser/control.py:51`,
   `tools/messaging/whatsapp.py:65`, …). Dispatching through a wrapper
   therefore cannot lower or bypass a class — the tool's own gate call still
   runs. This is why a gateway is safe.
2. **`Agent.update_tools()` is real**, but the Google realtime plugin applies
   it by closing and reopening the session (`plugins/google/realtime/realtime_api.py:761`
   → `_mark_restart_needed()`), while `update_instructions` can be mid-session.
   Schema swaps on `gemini_live` cost a **reconnect**; on `legacy_cascade` they
   are free (the schema rides in every completion request anyway).
3. **Instructions are one string re-sent whole** (`agent.py::sync_vision_instructions`
   documents this). So dynamic tool guidance must never ride instructions; the
   always-sent catalog must be small and *stable*.

Also load-bearing: `context_trim.fixed_prompt_tokens()` counts the fixed prompt
from `get_tools()` per request. Once the set is dynamic, the budget measures the
**currently loaded set** — otherwise the history budget assumes tools it never
registered, or worse, forgets ones it did.

---

## 2. The three tiers

| Tier | What | When present | Target cost |
| --- | --- | --- | --- |
| **0 — Catalog** | For every tool: `id — one line — key params — authority badge`. Generated from the registry, a *view* over it — never a second table. | always | ≤ ~400 tokens |
| **1 — Detail** | Full schema + usage note + relevant live state (`session.tab_context()`, the music folder, …) | first use, or on demand | the loaded tools only |
| **2 — Hot set** | Loaded families stay for the conversation | while warm | bounded ≤ 4 families |

The operating rule is existing house style: a card that restates the registry is
a promise that can be broken once. The catalog is **generated** from
`CapabilityRegistry` / `TOOL_CARDS`, and a test asserts catalog line ↔
`tool.info` agreement (name, params, authority), so it cannot drift.

---

## 3. Stage A — the gateway (the approved path)

One always-present meta-tool plus the catalog. Works on **both** pipelines, no
LiveKit churn, no reconnect:

```python
@function_tool(description="...")
def use_tool(tool: str, action: str = "", args: str = "") -> str:
    """Runs one capability by id..."""
```

- `args` is a JSON object validated in Python against the tool's own
  `info.params` (required fields, types, choices) **before** dispatch. A miss
  returns a refusal naming the exact schema fragment — a retry the model can
  act on, never a silent wrong action.
- Dispatch goes through `tools_registry.SURFACES` — the registry keeps a
  `surface id → the plain callable` map built when `ALL_TOOLS` is constructed.
  The gateway calls the **same function object** the model would, so every
  tool's own `require_approval`, refusal code and verification vocabulary are
  preserved untouched. Inventing a second dispatch path is a stop condition.
- Reference/newline classes identical: the tool id is validated against the
  registry before anything runs; a `use_tool("delete…")` that ids nothing
  refuses.

Catalog one-liners carry enough signature (`browse — action: open|observe|act|…
url for open, ref for act`) that the **common case stays one turn**: no
`load_tools` round trip for a search, a weather check, a track.

`load_tools(scope)` (the second always-present meta-tool) returns the full
schema text + the per-tool guidance for the requested family or id. It is what
makes Stage A survive the complex tools: the model that needs `web_control`'s
eleven parameters and the ref-scope rules reads them exactly once.

---

## 4. Preloading — router-predicted requests carry **zero additional metadata turns**

**Decision (2026-10-30).** "Zero extra metadata turns" is not an aspiration, it
is the acceptance criterion, with a definition tight enough to test:

> A **metadata turn** is a model turn whose only tool call is schema discovery
> (`load_tools`, or a `use_tool` call made *only* to learn a signature) — never
> the action the user asked for.

For a request where **`router.py`'s prediction is confident**, the family is
made available for the very next completion and the metadata-turn count is
**0** — same turn, zero extra latency:

- The router already predicts the environment and action *before* the model
  replies; its prediction loads the family for the next completion.
- The catalog one-liners (§2) carry enough signature that the *usual* single
  call needs no `load_tools` round trip for itself — that is the common case
  the criterion protects (a search, a weather check, a track).
- `run_task` steps declare their class (`STEP_ACTION_CLASS`), so the plan loads
  per step instead of loading everything up front.
- Interim STT transcription (`user_input_transcribed`) may trigger preload
  while the user is still speaking. Optimisation, not a requirement, and never
  a substitute for the router prediction.
- A small, configurable set stays fully registered at all times — cheap, asked
  constantly: `search_web`, `get_weather`, `get_news` (~120 tokens each).

**When the router is not confident** (ambiguous environment, unknown address
shape):
- at most **1** `load_tools` turn for that family, then the request completes
  normally;
- never more than **1** `load_tools` turn *per family per session* — a second
  one is a catalog bug, not a model choice;
- the turn is visible on `lumine.tool` — a hidden load is an invisible cost.

**Measured how:** a 20-request corpus with known routes; count `load_tools`
calls in the event record. Router-predicted requests: 0, every time. A
first-use complex family: ≤ 1, every time. "Mostly zero" is a failure of the
criterion, not a middle grade.

---

## 5. The loader — owns schema state, never lifecycle

**Decision (2026-10-30).** Recorded here because it is what Phase 2 receives
and what nothing else may become:

| Property | Rule |
|---|---|
| Ownership | **The session owns it.** One loader per session; Phase 2 injects the instance the runtime uses, and never constructs, caches, or restarts one |
| Scope | Schema state only: which families are loaded, since when, which are pending |
| Events | It **subscribes** to `lumine.*` (see Eviction §8). It is *told* what happened; it does not ask the task engine, the permission gate, or any task state |
| Lifecycle | It dies at session end, with any pending load cancelled and nothing persisted — hot sets are conversational, work is not |
| Trace | It mints no `trace_id`, no `event_id` — those arrive with [Phase 5](04-recovery-and-events.md#7-trace-identity--phase-5-owns-it-nobody-else-mints-it); until then `toolset_loaded` carries no trace ids, and that is correct |

A loader is not a service, a manager, or a resolver. It is a schema cache with
a turn boundary, and the mandate is line-for-line
[ADR-0010's](../decisions/0010-layer-ownership.md) "one owner per
responsibility".

---

## 6. Stage B — schema injection (optional, later)

Registering real schemas on demand via `Agent.update_tools()`:

- **Cascade:** free per completion. This is the natural target.
- **Gemini Live:** one reconnect per change. Acceptable **only if** measured
  (reconnect latency, any dropped utterance) and batched by family at natural
  boundaries, never per verb. Default **off** until those numbers exist.
- **Turn boundary only.** Stage B changes schemas *between* turns — never
  mid-generation, never mid-tool, never while an approval is pending. The
  realtime reconnect is a correctness hazard as well as a UX cost.

Stage B needs no new phase — it is Stage A's loader with a different backend
behind the same `tools.exposure` keys, gated on measurement.

---

## 7. Policy split (part of this phase, not a side effect)

`TOOL_USAGE_POLICY` currently *duplicates* guidance that also sits in the
docstrings — so today the model pays for it twice. The split:

- **Stays in the policy** (cross-tool, always sent): routing rules, one-job-one-tool,
  "never invent a web address", approvals/consent, uncertainty, injection guard,
  short spoken replies.
- **Moves into the docstring** (per tool): everything the policy says about one
  tool — `play_media` folder rules, `desktop_control` observe-first,
  screenshot `for_model` cost, WhatsApp message-id staleness.
- `POLICY_VERSION` bumps (5 → 6). `test_llm_runtime.py` pins are updated: the
  per-tool single claims move to docstring pins, the cross-tool pins stay.

The policy's *character budget* remains pinned — smaller, not free-form.

---

## 8. Eviction & observability — event-driven, never task-aware

**Decision (2026-10-30): eviction is event-driven.** The loader learns the
world by **subscribing to `lumine.*` events** — task/step events, gate approval
pending/resolved events, turn boundaries. It **never imports `task_model`,
`task_engine`, `task_runner`, `task_runner._TASKS`, `permission_gate`, or any
task state — it does not read upward, and it does not poll.**
[ADR-0010](../decisions/0010-layer-ownership.md) gets a strict reading here
because it is the only rule that scales: a tool-layer component that imports
task state makes *every* loader change a Phase 2-5 change, and turns the loader
into the kind of subsystem the audit listed precisely to avoid.

Conditions held from the events it already receives:

- unused for `tools.loadWindow` turns (default **5** = turns seen on
  turn-boundary events);
- no **pending approval** event outstanding for that family (the gate's
  `permission.requested` / `granted` / `denied` are what tell it so — the gate
  stays the authority; the loader is a listener);
- no **task step** event whose remaining steps reference the family (releases
  on `task.finished`, `task.cancelled`, `task.failed`);
- the conversation implies continuation ("run the next step").

Every eviction and load publishes `toolset_loaded` on
[`lumine.tool`](../../agent/AGENTS.md) — never on `agent_state_changed`
([ADR-0005](../decisions/0005-single-lifecycle-authority.md): the lifecycle
machine stays the only companion-state authority). The budget is recomputed
from the event, never assumed.

Never evict mid-approval for the same reason the send ledger exists: an
approval
answered into an evicted gate is an answer to nothing.

**Missed events are conservative, not clever.** If the loader's subscribe
misses what it needs, it holds the family **longer** — a wrong *early* eviction
is the failure mode the "answer to nothing" rule names. A stale hot set is
bounded (≤ 4 families) and costs tokens; a wrongly evicted family costs a
denial someone has to re-earn.

---

## 9. Tests (`agent/tests/test_capability_discovery.py`)

- Catalog is a view: every registry id has a line; every line matches
  `tool.info` (name, params, authority) — same discipline as `tool_catalog --check`.
- Gateway dispatch reaches the real callable: a sensitive act through
  `use_tool` still raises an approval card; a disabled tool refuses unchanged;
  an unknown id or malformed args refuses with the schema fragment.
- `use_tool` keeps ADR-0014's promise: the gateway id namespace is the dotted
  registry namespace — one taxonomy, not a second one — and a test asserts the
  catalog cards remain byte-identical.
- `load_tools` registers what it promises; `update_tools` diff carries exactly
  the family; the hot set caps at 4 and the next load reports what it evicted.
- Eviction is **event-fed**: the test feeds `lumine.*` events (a pending
  approval, a step that references the family, a turn boundary) and asserts
  eviction follows the events, not task state. The loader's import list is
  asserted — no `task_model`, no `task_engine`, no `task_runner`, and no
  `permission_gate` import anywhere in the module ([ADR-0010](../decisions/0010-layer-ownership.md)).
- Missed events are conservative: when the event feed is silent, eviction
  holds **longer**, never shorter.
- A turn-boundary rule: a schema change requested mid-turn is deferred to the
  next boundary, and the deferral is visible on the event stream.
- `fixed_prompt_tokens` in catalogue mode counts catalog + meta-tools + loaded
  set, and the ceiling test asserts **≤ 1,200 tokens** for the always-sent
  block (persona excluded).
- Fewer tokens is not *fewer tools*: `tool_ids(get_tools())` and the 17
  catalog cards are byte-identical to today; the Tools page does not change.

---

## 10. Exit criteria

- [ ] Always-sent block ≤ 1,200 tokens, measured, in a test — *provisional:*
      the definition is [`estimate_tokens`, persona excluded, measured at turn
      start over 50 turns](10-consistency-audit.md#2-phase-acceptance-matrix);
      the number is a gate once that feasibility note is accepted.
- [ ] **Router-predicted requests complete with 0 extra metadata turns** —
      20-request corpus, counted from the event record, every run.
- [ ] A first-use browser request: ≤ 1 `load_tools` turn, then behaves exactly
  as it does today, including its own approvals and refusals.
- [ ] Every action class reachable, testable and explained from the catalogue
  mode; the gate is unchanged (pass the entire existing permission suite).
- [ ] `fixed_prompt_tokens` reflects the loaded set; history budget matches it.
- [ ] Stage A reconnect count on `gemini_live` = **0** (counted from session
  events); Stage B stays off until its numbers exist.
- [ ] Full suite: 2232+ passing, 3 or fewer skipped.

## 11. Negative scope — must not implement

- Splitting any function tool (ADR-0014 stands) and no *second* dispatch
  surface beside `use_tool` — the model-facing surface count does not grow.
- Adding, removing, or re-classing a capability — the registry's contents are
  Phase 1's authority; this phase only changes *when a schema is present*.
- Owning lifecycle: no session restart on the loader's behalf, no process
  management, no reconnect policy of its own (Stage B's reconnect belongs to
  the session, measured and gated).
- Subagents, task persistence, or anything from Phases 2–10 — including a
  `tools_policy.py` rewrite that grows rather than shrinks.
- Instruction *editing* as a transport for tool state (the one-string rule).
- Trace ids, event ids, or any Phase 5 identity minted anywhere in the loader
  ([§5](#5-the-loader--owns-schema-state-never-lifecycle): they arrive with the
  bus, or it does not arrive).
- A second, hand-written catalog table, or changes to card text, the Tools
  page, or the permission floor — the point of this phase is a smaller
  always-sent block, not a different user experience.
- A network source for the catalog; the catalog is generated from the
  registry, locally, always.

## 12. Stop conditions

- A dispatch path that cannot reach each tool's own `require_approval`.
- Catalogue mode measurably increasing wrong-tool calls or extra turns on the
  common cases (router-preloaded requests).
- The catalogue drifting from the registry (that is what the tests refuse).
- Any eviction firing while an approval is pending — stop, this is the exact
  failure the send ledger exists to prevent.

## 13. Report on completion

1. Files changed · 2. measured always-sent tokens, before/after · 3. pins moved
   policy → docstring · 4. tests added · 5. full suite result · 6. any gate
   behaviour discovered to differ through the gateway · 7. whether Stage B is
   measurable and worth enabling.
