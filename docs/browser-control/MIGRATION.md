# Browser Control Subsystem — Migration Plan

Status: the page-awareness slice is additive and requires no caller migration.
The coordinate-click and download-guard supersessions below remain proposals;
neither has shipped as part of that slice. This track renames nothing, removes
nothing, and moves no caller. Any future conflict with a recorded decision must
still be superseded explicitly, in writing, with a reason — not deleted.

## Delivered page-awareness change

`browse` and `web_control` now consume the shared visible snapshot described in
`ARCHITECTURE.md`. It adds bounded video/product items and carries the title,
price, destination, and grounded primary-link reference where the live page
exposes them. Native select options and the current selection travel with the
control observation so the existing `browse act` select operation can make and
verify a choice.

This does not add a tool id or verb, weaken the route or permission guards, change
the shared WhatsApp context, or alter the download policy. The public-site smoke
and its exact limitations are in `TEST_PLAN.md`.

## Why there is no caller migration

The user-facing surface is frozen, by decision:

| Surface | Status |
| --- | --- |
| `search_web` | unchanged — DuckDuckGo HTML endpoint, `httpx`, no keyed API (user decision: an external search API is optional, not required) |
| `browse` | same eight verbs (`open`/`observe`/`act`/`back`/`forward`/`scroll`/`tab`/`close`); new capability arrives as new verbs or new `text` operations, never as a changed meaning |
| `web_control` | same (`read`/`state`/`act`/`pagetabs`/`youtube` plus `frame=`) |
| `run_task` / `cancel_task` | same tools; the task model gains a state, not a new tool |
| Tool ids, registration order, `STEP_ACTION_CLASS`, `SUPPRESSED_BY_DESKTOP_INPUT` membership | unchanged |

Consequences: no frontend caller changes for Phases 1–5; Phase 6 is the only phase
touching `lumine-ui/`, and only by rendering a new additive record type the old
frontend already ignores (it type-checks `type === "browser_preview"`).

## Supersession 1 — the coordinate-click refusal

**Record:** `agent/tools/browser/locators.py` module docstring refuses coordinates
as a fallback, with reasons: coordinates are stale-prone, unverifiable after the
fact, and worse than a refusal when the target is a password field.

**Superseded by:** the coordinate path in `ARCHITECTURE.md` §Phase 4, approved by
the user in the Phase 0 review ("coordinate clicks behind a freshness token,
accounting for viewport, scroll and layout changes — not just navigation
generations").

**Why the original reasons no longer hold, one by one:**

- *Stale-prone* — true of a bare coordinate; the viewport signature (tab,
  generation, url, viewport size + DPR, scroll offset, layout digest, age) is
  re-verified in the same tick as the click, and any mismatch is a refusal. A
  screenshot's coordinates cannot outlive the pixels they were measured on.
- *Unverifiable after the fact* — the dispatch resolves the element under the
  coordinates in the *live* DOM and reports its role+name, so the result and the
  audit record both name what was pressed, not just where.
- *Password fields* — the structural password refusal is checked before the token
  and is unchanged; no approval or coordinate can reach one.

**What survives of the refusal:** coordinates remain a *fallback*. The role+name
ladder still runs first and a confident ref still wins. The refusal of coordinates
as a *first* strategy is not superseded and its docstring reasoning stays, with the
supersession appended rather than replacing it.

## Supersession 2 — the `accept_downloads=False` guard

**Record:** the browser context is created with `accept_downloads=False`,
asserted by `agent/tests/test_browser_lifecycle.py:324`, and
`agent/tools/messaging/whatsapp_files.py` refuses URL attachments citing that flag
("downloading would mean lifting `accept_downloads=False`").

**Superseded by:** the transfer interceptor in `ARCHITECTURE.md` §Phase 5, approved
by the user in the Phase 0 review ("policy-bounded downloads, preserving WhatsApp's
shared-browser security guarantees").

**Why, in one sentence:** the flag was an expression of "no file lands on this
machine unless Lumine deliberately put it there", and the interceptor says the same
thing with a mechanism that can also *allow* a deliberate, allowlisted,
destination-validated save — which the flag cannot, because it refuses all
downloads including the ones the user explicitly asked for.

**The migration steps, in order, so no intermediate commit weakens the guarantee:**

1. `transfers.py` lands first with the allowlist logic and its tests — nothing
   consumes it yet, the context flag is untouched.
2. The context-level download interceptor lands and is **registered while
   `accept_downloads` is still False** — a no-op interceptor, proven by test to
   discard nothing because nothing can arrive, and proven to be *registered*.
3. `accept_downloads` flips to True. The guarantee now rests on the interceptor,
   which is already proven registered and tested with a synthetic `Download`.
4. `whatsapp_files.py`'s refusal **stays**, its reason text rewritten to cite the
   interceptor instead of the flag. The refusal itself is not migrated: a URL
   attachment would still need a download that no token ever claimed, which the
   interceptor discards — so refusing at the boundary remains correct and now has
   two independent layers behind it.
5. `test_browser_lifecycle.py:324`'s assertion is rewritten to assert
   `accept_downloads is True` **and** the interceptor registered — the test's job
   (pin the download posture) is preserved; its expression changes because the
   posture's mechanism changed. The change is annotated with this document's name.
6. `test_whatsapp_safety.py` gains: a download triggered from the WhatsApp tab is
   discarded, never saved; the WhatsApp attach path still refuses URLs.

**Rollback:** steps 3–6 are one commit and revert as one; steps 1–2 are harmless
without it and can stay.

## Compatibility risks and their mitigations

| Risk | Phase | Mitigation |
| --- | --- | --- |
| Context flag change is visible to the WhatsApp path and every other room sharing the browser | 5 | Interceptor-first ordering above; `test_whatsapp_safety.py` case; guest-never-restarts-host invariant untouched |
| Restart-after-crash could kill the host browser another room is using | 1 | Recovery is *rejoin* by default; relaunch only when this process holds `_OwnerLock`; the existing `_close_remote(shared=True)` contract is asserted by new tests, not re-derived |
| New verbs cost tokens on every completion (~4.7k budget) | 4, 5 | New verbs reuse the existing `text`-operation shape where possible (`download`/`upload` as `act` operations); docstring additions are one line each; the policy text in `tools_policy.py` is not touched |
| An old frontend meets a new record type | 6 | `voice-manager.ts` type-checks before use; `browser_activity` is additive and ignored by construction |
| Shadow-DOM traversal could recurse on hostile markup | 1 | Depth cap constant; the collection already bounds element count (`MAX_ELEMENTS`), the descent shares that budget |
| The single verb enum could reject a call that used to parse | 2 | The enum is *derived from the current parsers' accepted sets* — no call that parses today is rejected; the model-generated-call corpus (recorded, sanitized) proves it |
| `desktop.input` off suppresses `browse`/`web_control` — new verbs must be suppressed with them | 4, 5 | New verbs live inside the existing tools, so `SUPPRESSED_BY_DESKTOP_INPUT` covers them without modification; a test asserts a new verb is unreachable when the switch is off |

## Removal strategy

**Nothing is removed in this track.** The audit found no obsolete module: the
smallest (`preview.py`, 174 lines) is the frontend's only screenshot path, and
`whatsapp_desktop.py` is the unreachable-but-authoritative adapter record the
messaging docs explicitly keep.

Should a future track want to remove something, the procedure this repository has
used and this track adopts:

1. Grep the whole tree for the symbol — agent, tests, frontend, docs — and list
   every reference with its line.
2. Check `docs/gap-audit.md`, the relevant ADR, and the area `AGENTS.md` for prose
   that names it as a contract (prose can be a contract; `whatsapp_desktop.py`'s
   docstring is exactly that).
3. Write the removal reason into the owning doc (ADR supersession or area file)
   *before* deleting the file, per the repository rule: supersede explicitly, do
   not delete decision records.

No step 3 is reachable today — this is recorded so the procedure exists when it is.

## Per-phase rollback

Each phase in `ROADMAP.md` is one revertible commit series. The invariant every
rollback must restore: the full suite green at the phase's entry baseline, and the
WhatsApp shared-browser contract tests passing unmodified except where this
document names the change.
