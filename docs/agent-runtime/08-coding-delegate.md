# Phase 11 — Coding delegate

- **Status:** specified, **not approved.** Blocked on Phases **1** (the
  `code.*` registry rows), **2** ([cancellation ownership](02-agent-runtime.md#6-cancellation--two-scopes-and-acknowledgement-is-not-termination)),
  **3** (persistent tasks **and** the embedded effect ledger), **4**
  (recovery/`interrupted` semantics), **5** (events and trace), and **6**
  (*path discipline only* — Phase 6 is read-only and cannot satisfy a write
  need), and on filing **both** ADRs:
  [0015](../decisions/0015-coding-delegate.md) and the `filesystem.write`
  authority, [0016](../decisions/0016-filesystem-write-scoped-worktrees.md) —
  both filed as drafts, status *Proposed — not accepted*, with the reasoning
  carried in [§9](#9-adr-draft-note--0015-and-0016).
  The island ([companion Phase 2](../../ROADMAP.md#phase-2--lumine-island))
  is the surface where work measured in minutes gets progress — a *consumer*,
  not a dependency.
- **Origin:** user request, 2026-10-30 — *"Lumine should have access to control
  coding agents (opencode, Claude, Codex, Hermes etc). She should be able to
  make her own features and test, debug them."*

This amends two recorded positions. [§9](#9-adr-draft-note--0015-and-0016) owns
that decision; this section only
names it: *"Lumine is not a coding agent"* (README, GSD table) and *"do not
extend to subagents"* (same table). Both are amended, not deleted.

---

## 1. Why

Lumine has hands (`desktop_control`) and a browser, but building a feature —
reading a codebase, editing, running tests, reading failures, iterating — is
minutes-to-hours of work that no voice turn is shaped for. External coding
agents already exist and are good at exactly this. Lumine's contribution is the
companion layer around them:

- She receives a request from a voice sentence and turns it into a written
  **spec**, because speech is not a valid input to an agent that edits code.
- She delegates, watches, verifies, and reports — `verification.py`'s ladder all
  the way up.
- **She proposes; a human merges.** Nothing lands without the user, and the
  running worker never reloads itself.

She does not become a coding agent. She becomes the one that drives one.

---

## 2. The family — `code.*`

| id | verb | class | what it does |
| --- | --- | --- | --- |
| `code.delegate` | `start` | `destructive` | create the worktree, launch the delegate, register the task |
| `code.status` | `status` | `read` | progress, last event, recent log (capped, artefact-backed) |
| `code.review` | `review` | `read` | diff stat, changed files, test result — of the worktree, applying nothing |
| `code.cancel` | `cancel` | `act` | stop the process, keep the worktree and the report |

No verb in this family merges, pushes, deletes a worktree, or touches the
worker's own process. `code.cancel` rides the existing `cancel_task` intent —
one cancellation path, not two — and it obeys
[Phase 2's](02-agent-runtime.md#6-cancellation--two-scopes-and-acknowledgement-is-not-termination)
ack/termination rule: an ack is returned immediately, the process death is
confirmed separately by evidence, and nothing reports *"stopped"* on an ack
alone. `code.cancel` is `act`; the *start* is the destructive verb, exactly once.

**Projects are granted, not discovered.** A project is registered in Settings
→ **Coding** with: repo path, allowed adapters, timeout, *network on/off*, and
the test command. Each registration is a one-time explicit grant; a voice
request whose project is not registered is "which project?" — the same
act-or-refuse shape the music folder uses, never an inferred path.

**Start is idempotent by key.** `(task_id, prompt hash, project, adapter)` is
the key; a second start against the same key returns the existing handle and
never spawns a second process — the same shape `apps.py`'s launch-dedup
window already proved, with a lifetime matching a delegate run instead of
seconds. Duplicate starts are logged, audited, and answered with the running
handle, not asked again.

**Worktree creation is idempotent by task id.** An existing tree whose
ownership marker matches is a resume candidate; a tree whose marker does not
match is refused, not adopted. No second worktree is ever created for the
same task id.

---

## 3. The adapter protocol

One protocol; the vendor list is not the contract, the protocol is. An adapter
is listed only when `available()` returns a verdict from evidence — running a
probe, not seeing a name on disk:

```python
class CodingAgent(Protocol):
    id: str
    async def available(self) -> AdapterState          # found / missing / broken + version
    async def start(self, spec: RunSpec) -> RunHandle  # cwd, prompt file, env, timeout
    async def poll(self, handle: RunHandle) -> Progress
    async def result(self, handle: RunHandle) -> RunResult
    async def stop(self, handle: RunHandle) -> None    # kills the tree, leaves no orphan
```

| Adapter | Mechanism | Note for the implementer |
| --- | --- | --- |
| **OpenCode** | V2 is client–server: the HTTP API documented by the running server's `/openapi.json`, reachable locally via `opencode api get /api/info`, or the `@opencode/client` SDK. A shared background service owns sessions and permissions. | Deployed on this machine today — drive it once manually before writing the adapter. Its own permission model still applies on top of Lumine's gate; **two gates are not a loophole, they are additive refusals.** |
| **Claude Code** | headless/print mode (`claude -p …`, JSON output). | Probe the exact invocation live before wiring. |
| **Codex** | non-interactive execution subcommand with JSON output. | Probe live. |
| **Hermes / generic** | a user-configured binary + argument template + result marker, configured per project in settings. | An agent that cannot show its output format refuses to be listed. |

The prompt reaches the delegate as a **file**, not a shell argument — the spec
is text the user approved, never re-formed from her transcript's wording.

---

## 4. Isolation — non-negotiable

Every item below is structural. None of it is a prompt instruction.

1. **One git worktree + branch per task** —
   `<repo>/../.lumine-worktrees/<task-id>/`, created before the delegate
   starts. The main checkout stays clean; nothing outside a worktree is
   writable — the write authority *is*
   [ADR-0016](../decisions/0016-filesystem-write-scoped-worktrees.md), scoped
   resolved-then-compared to exactly this directory.
2. **Credentials never cross.** The child environment is built from the parent's
   reduced for **slot names** — the delegate inherits *no* `LUMINE_*` value
   (including keyring-injected credentials and `LUMINE_CONFIG_PATH`, which
   would hand the repo's own keys over) and no `KeySlot` variable.
   The slot list is **derived from `agent/settings/providers.py`**, not
   hardcoded a second time — the same one-authority rule every other table in
   this codebase follows. A test asserts slot-name absence from the child env.
3. **Untracked files do not enter a worktree.** `agent/.env` and
   `lumine.config.json` are ignored, so they cannot ride the `worktree add`.
   The start step still asserts no credential-named file exists in the tree and
   **refuses to start** if one does — the check is cheap and the failure it
   prevents is not reversible.
4. **A lease is written before the process starts.** `.lumine-delegate.json`
   inside the worktree records `task_id`, `run_id`, `pid`, **process creation
   time**, `executable`, and `worktree path`. Reaping (§6) decides from the
   lease, never from a pid alone.
5. **Timeout + output caps.** A delegate past its deadline is killed; the task
   becomes `uncertain`; the worktree and everything written so far are kept.
   Kill happens ≤ 2 s past the deadline *— provisional* — measured against a
   schedule that ignores the term signal. Delegate output never enters the
   conversation uncapped — large logs go to a task **artefact** reviewed on
   demand, so the cost is paid once
   ([same lesson as Phase 9](06-research-and-proactive.md)).
6. **Network is per-project, default off** in the saved document (installs and
   fetches need it; silence about it is the failure, so the switch is explicit).
7. **The running worker is never its own build target.** The checked-out repo
   is read-only to everything in this phase. A feature becomes live only after
   a human merges and the worker restarts — the restart path that already
   exists, not a hot reload invented for this.

---

## 5. The boundary that makes self-improvement safe

> **Everything is propose-only; the merge is the whole boundary.**

- The delegate's diff is a **proposal** — reviewable, testable, rejectable.
  She may propose anything, including edits to her own permission gate,
  because a proposal changes nothing until a person acts. The Tools page and
  the full UI already exist as the places a person acts.
- The worker never hot-reloads. There is no reload path in this phase; the
  merge-and-restart is the existing restart path.
- A **protected set** is honest about itself rather than a refusal: worktree
  changes that touch the permission gate and its policy keys, the audit log,
  or `personality` files are flagged `PROTECTED` in the proposal card and
  *demand a heavier look* — the card says so; it does not pretend a flagged
  diff is ordinary.

Cards carry exactly: **(project, adapter, prompt summary, files touched,
protected flag, estimated duration)**. A spoken question accompanies the card —
a proposal that arrives only in a corner of a window is a question a person
talking to Lumine will not notice ([permission_gate precedent](../../agent/runtime/permission_gate.py)).

---

## 6. Orphan reaping — identity and ownership, never a pid

**Decision (2026-10-30).** A worker crash can leave a delegate process running
with nobody watching. Someone at worker start must decide *who owned it*,
**without relying on the pid alone** — Windows reuses pids aggressively, and a
stale pid can point at an unrelated process.

The lease (§4 item 4) is the authority. A reaper runs **at worker start, owned
by Phase 11**, and:

1. Scans **only** tasks the store says are `active` carrying delegate
   metadata — nothing else in the path is a candidate.
2. For each candidate, checks the lease in its worktree: does `task_id` and
   `run_id` match the task record? Does `worktree path`?
3. Re-resolves the process by `pid` **and verifies identity**: creation time
   matches, executable matches. A pid reused by another program fails this and
   is never touched.
4. Only on a full match kills the **process tree** — never the parent job —
   marks the task `interrupted`/`uncertain` as evidence dictates, and keeps
   the worktree.

**An unverified orphan is left alive.** The task is marked `uncertain` with the
lease's evidence, a card says what was found (and was *not* killed), and the
resolution is the user's — "end it", "wait", "open it". Killing a process whose
identity cannot be proven is the same class of mistake as clicking a control
whose name is ambiguous, and the refusal is the safer answer by orders of
magnitude.

`code.cancel` (a live run) goes through the adapter's `stop()`, which kills the
tree and confirms via the lease going dead plus an exit observation — ack and
terminated are reported separately, per
[Phase 2 §6](02-agent-runtime.md#6-cancellation--two-scopes-and-acknowledgement-is-not-termination).

---

## 7. What Lumine says

| Moment | Who speaks |
| --- | --- |
| After writing the spec | She reads back **exactly the text the delegate will receive** — two wordings is two events to keep in step. |
| While running | Product lines in the `progress.py` rhythm — *minutes* are audible; silence that long reads as a broken system, the same lesson the approval card teaches with "awaiting your answer". |
| At completion | A spoken summary in her voice + a pointer to the diff/artefact. The diff itself stays in the artefact — putting it in the conversation is how a proposal gets re-billed on every later turn (Phase 9's reason for the same rule). |

---

## 8. Tests

- Gate: `code.delegate` asks before every start; a timeout answers as a
  **denial**; the un-run proposal is audited, matches the existing gate's
  refusal path. A wrong project, an unregistered project, missing test command —
  each its own sentence through `code.status`.
- **Start idempotency:** a second start on the same key returns the running
  handle — 0 extra processes.
- Worktree lifecycle: created before start; pruned on completion **only** after
  the artefact is written and the result confirmed; retained on `uncertain`,
  timeout and cancel; recovery finds it again after a worker crash (Phase 4);
  worktree retention is registered with the central retention service, never
  a sweeper of its own.
- Environment: no `LUMINE_*`, no catalog slot name; no credential-named file in
  the tree (start refuses on one); `.env` absent from a fresh worktree even if
  gitignore rules drift.
- Bounded loop: two repair attempts inside the same worktree, then a report —
  not another delegation. Report carries the failed attempts' evidence.
- Cancel: ack is immediate, termination is confirmed separately; forward to the
  delegate; process tree dead; no orphan; worktree intact with the cancel noted
  in the artefact.
- **Reaping:** a fake lease/process table verifies identity match kills, pid
  -reuse mismatch leaves alive; killed-tree children are gone; an unverified
  orphan is left running with a card asking, and `"leave it alone"` works.
- Path discipline: a write path that resolves outside the worktree refuses via
  the ADR-0016 rule (`..`, absolute, symlink — the `whatsapp_files` shape); the
  main checkout is untouched in every fixture.
- The existing suite is untouched: **every other permission, tool and gate
  property must hold unchanged** — the new family must appear in
  `capabilities`/`permission` tables through the normal door and change nothing
  else. `test_tool_catalog` disagreements count as this phase's failures.

---

## 9. ADR draft note — [0015](../decisions/0015-coding-delegate.md) and [0016](../decisions/0016-filesystem-write-scoped-worktrees.md)

Both are **filed as drafts, status "Proposed — not accepted"**, on this date.
This section carries the reasoning, and the decisions directory carries the
records; whatever they say at approval time is the authority.

**0015 — Lumine delegates engineering work to external coding agents.**

**0015, reasoning.** Two recorded positions bear directly:

- *"Lumine is not a coding agent"* ([agent-runtime README](README.md), GSD
  table) — still intended true. She delegates; she does not become one. The
  distinction the original record was protecting — her *shape* as a voice
  companion, not a repo tool — is preserved by everything in §4–5.
- *"Thin orchestrator + specialists … do not extend to subagents"* (same
  table), and [Phase 9's](06-research-and-proactive.md) *"do not add subagents
  as a general mechanism"* — that refusal was about **fresh-context workers
  inside the agent process**. A phase-11 delegate is a different creature: an
  external process the user granted, watched on the task's events, never
  holding a permission request from construction.

**0016, the write authority.** `filesystem.write` exists only as a
non-selectable capability row, granted by the `code.delegate` approval, scoped
resolved-then-compared to one task's worktree ([the same shape the attachment
check proved](../../agent/tools/messaging/whatsapp_files.py)). It is never a
model-facing verb. Full record: [0016](../decisions/0016-filesystem-write-scoped-worktrees.md).

**0015, decision.** Lumine delegates to user-approved external coding agents:
worktree-isolated, network per project and off by default, `destructive`
authority, **propose-only** — nothing merges without the user, the worker never
reloads itself, credential sanitization derived from the provider catalog.

**0015, alternatives refused.**

- *She edits her own source directly.* The running worker rewriting its own
  authority — the gate, its policy keys, the audit log — is the exact structure
  the "own window" refusal prevents in desktop control. A code edit is that,
  but slower and quieter.
- *Gate on green tests: auto-merge when the test run passes.* Tests measure
  code, not authority. The merge is where a human sees the intent, and it
  stays a human act. If evidence eventually shows a measurable pattern of
  merge-only-by-hand for *small, no-protected-file* diffs, that
  **person-controlled** lane can be revisited in a later revision — never the
  protected set, and never without its own measurement section like this one.

**0015, revisit conditions.** If the delegate pattern is shown unsafe (credential
leak, out-of-worktree write, or a merge without evidence in the audit trail),
0015 is reversed by stopping `code.*` at the registry — no data migration, no
user-visible permission state to undo.

---

## 10. Negative scope — must not implement

- No general subagent mechanism (Phase 9's note stands; this is a tool, not a
  spawn) — and no second delegate process for the same task id, ever.
- No merge, push, deploy, or release — by this phase's tools or by a workflow
  that talks about them.
- No offline equivalently-capable agent under a different name — if it cannot
  be probed live, it is not listed.
- No "she writes code in chat" — her spec is prose she read back, delivered as
  a file, and her loop is with the delegate, not with the keyboard.
- No `filesystem.write` outside the worktree, in any form — no "writable
  scratch" beside it, no second permission wording that mints one
  ([ADR-0016](../decisions/0016-filesystem-write-scoped-worktrees.md)).
- No second credential store, no per-adapter key handling — slot names come
  from the one provider catalog.
- No CI ownership, no scheduled repo work, no unattended run — every start has
  a user-approved spec, and every return a user-visible report.
- No auto-approve mode, no "small diff" bypass of the gate; the protected set
  makes the card heavier, never skippable.
- No IDE surface — Lumine reads the diff, she does not become an editor.

## 11. Stop conditions

- An adapter that cannot be probed at approval time.
- Any write outside a task's worktree, detected in a test or a live run.
- Any credential value in a child environment, detected in a test or a live run.
- Any merge executed without the user's action in the audit trail.
- Any reaping that kills a process whose identity the lease cannot fully
  verify — a pid match alone is never a kill.
- The delegate becoming a way to bypass the gate — if its actions are not
  classed, gated and audited by the same rules as every other tool, stop.

## 12. Report on completion

1. Files changed · 2. adapters verified live (which, and how) · 3. projects
   registered · 4. tests added · 5. full suite result · 6. the exact wording of
   the proposal card and its spoken line · 7. worktree lifecycle under crash
   recovery (Phase 4) and restart.

---

## Companion-track interplay

- **Phase 2 (island)** is where delegate progress is *owed a surface*, the way
  tool status already gets one (`lumine.tool`).
- **Companion Phase 6** (Agent planning, "trusted multi-step work") is where
  the language a user reaches it from would live — this phase supplies the
  machinery only, much like the runtime supplies machinery to the companion in
  both directions.
