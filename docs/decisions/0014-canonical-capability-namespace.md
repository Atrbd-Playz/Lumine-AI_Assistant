# 0014 — Canonical capability ids do not split the model-facing tools

- **Status:** Accepted
- **Date:** 2026-10-07

## Context

Capability, authority and verification metadata is declared in **four** places,
written at different times by different fixes:

- `tools/desktop/ops.py::OPERATIONS` — written for the desktop safety work
- `tools/messaging/whatsapp_ops.py::OPERATIONS` — written for the send ledger
- `tools/tools_permission.py::TOOL_ACTION_CLASS` — the Tools-page badge
- `tools/task_runner.py::STEP_ACTION_CLASS` — plan validation

A verb added to one and not the others produces a card that promises less than
the gate enforces. The codebase's own comment on that class of bug calls it
**"a security bug rather than a typo."**

Two of the four already *compute* rather than declare —
`desktop_ops.tool_action_class()` and `whatsapp_ops.tool_action_class()` — and both
docstrings give the same reason: *"a badge that restates the strictest of nine
rows is a ninth thing to keep in step."* So the consolidation finishes a migration
those modules already began.

The open question is the **shape**. A natural answer is a dotted namespace —
`computer.click`, `filesystem.read`, `browser.open` — and then one function tool
per capability.

## Decision

**Dotted capability ids are the canonical authority. The existing verb-parameterised
function tool remains the model-facing interface.**

```text
Canonical capability:   computer.click
        ↓
Existing surface:       desktop_control(action="click")
        ↓
Implementation:         tools/desktop/ops.py
```

Three namespaces are kept distinct, and two of them are the *same category*:

| Namespace | Surface | Addressing |
| --- | --- | --- |
| `computer.*` | `desktop_control` | element, coordinate |
| `browser.page.*` | `browse` | ref |
| `browser.control.*` | `web_control` | semantic (role + name) |

## Why not the alternative

**Rejected: one function tool per capability.** Splitting
`browse(action=open|observe|act|back|forward|scroll|tab|close)` into eight tools and
`desktop_control(action=click|type|press|move|observe)` into five was considered
and rejected on three measured grounds:

1. **Token cost.** `tools/tools_policy.py` records that instructions plus tool
   schemas are **~4.7k tokens per completion**, which the module itself calls
   *"more than half the minute"* of Groq's free 8,000 TPM tier. Seventeen tools
   become thirty, and the schemas are re-sent on *every* completion.
2. **The one-job rule.** The same file encodes: *"at most one tool per reply —
   where one job is one tool: a `browse` job of open/observe/act is one job."*
   Narrow per-verb tools make it *more* likely, not less, that a voice turn fires
   several.
3. **No provider-side backstop.** `parallel_tool_calls: False` is cascade-only.
   The Gemini Live realtime models expose no such switch, and they are the
   default pipeline.

**Rejected: a single `browser.*` namespace.** `browse` and `web_control` are two
surfaces over one category with different addressing *and incompatible reference
scopes*. `browse` refs are stamped with a **page generation** and are refused after
a navigation; `web_control` refs are **snapshot**-scoped and its own tool policy
forbids reading with one and acting with the other. Collapsing them into one
`browser.click` makes two incompatible ref scopes look interchangeable, and erases
a distinction `AGENTS.md` already records: they are *"split by difficulty, not by
feature."*

**Rejected: renaming the tools and the config keys.** `tools.<flat_id>`,
`permissions.tools["desktop_control.click"]`, `LUMINE_DISABLED_TOOLS`,
`LUMINE_PERMISSIONS`, the Tools page, and `DEFAULT_OFF_TOOLS` all key off the flat
name. A migration with no user benefit and a broken-settings risk was not worth a
tidy namespace.

## Consequences

- **The registry becomes the single authority** for capability, authority, and
  verification metadata, and the four sources become views over it.
- **Two axes, not one.** `action_class` (`read` / `act` / `sensitive` /
  `destructive`) is *authority* — enforced by `permission_gate`. `risk` (`low` /
  `medium` / `high` / `critical`) is *consequence* — declared, never enforced. They
  are not interchangeable, and the case a single axis cannot express is **low
  authority, high risk**: an ordinary act on something fragile.
- **Three class-resolution models coexist and stay honest.** A static per-verb
  class (`computer.press`), a class computed at the call site from the target
  (clicking *Send* vs *Next*), and a declared floor that is underivable
  (`run_task` is `destructive` because an unread plan may contain any step).
  Collapsing them into one field would have been a silent regression.
- **Resolution is additive:** `capabilities.<dotted>` → legacy `<surface>.<verb>`
  → tool-level. Dotted wins so a future migration can tighten one capability
  without touching legacy keys; legacy keeps working so nobody's existing
  configuration silently changes meaning.
- **Fail-open and fail-closed stay opposite.** `tools.<id>` reduces authority so an
  unrecognised value reads as `on`; `desktop.input` and `media.enabled` grant a
  capability so theirs reads as `off`. Unifying them would make a typo silently
  arm a capability.
- **The Tools page is unaffected.** `permission_floor()` behaves identically, and
  the cards must stay byte-identical.
- A capability registry nothing reads is not progress. Phase 1's exit criterion is
  that the four old sources are now *views*, asserted by test.

## Revisit when

The schema cost stops being the binding constraint — for example if a voice
profile arrives with a context window large enough that thirty narrow tools are
affordable, **and** measured tool-selection accuracy improves. Neither has been
measured. Until then the flat surface is the correct trade for a per-minute token
budget on a voice turn.

See [`../agent-runtime/01-capability-registry.md`](../agent-runtime/01-capability-registry.md).
