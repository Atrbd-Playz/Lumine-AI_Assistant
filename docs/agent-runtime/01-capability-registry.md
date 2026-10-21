# Phase 1 — `CapabilityRegistry`

- **Status:** ✅ **Approved for implementation**
- **Nature:** refactoring and authority consolidation. **No feature development.**
- **Baseline that must not regress:** `2232 tests passing, 3 skipped`
- **Blocks:** Phase 2 must not begin automatically

**Success criterion:** a small, clean, well-tested diff that removes duplicated
authority. A new abstraction nothing reads is a failed phase, not progress.

---

## 1. Goal

Establish `agent/tools/capabilities.py` as the **canonical vocabulary of what
Lumine can do**, and make every existing metadata source a *view* over it —
without changing anything a user or the model can observe.

```text
BEFORE                                  AFTER

desktop/ops.py ────┐                    ┌───────────────────┐
whatsapp_ops.py ───┤                    │                   │
TOOL_ACTION_CLASS ─┼──→ divergence      │  CapabilityRegistry│
STEP_ACTION_CLASS ─┤                    │                   │
tool_catalog.py ───┤                    └───┬───────────┬───┘
permission cfg ────┘                          │           │
                                        catalog│           │permission
                                        cards  │           │policy keys
```

---

## 2. Why the registry exists

Capability, authority and verification metadata is declared in **four** places,
written at different times by different fixes.

Two of them already *compute* rather than declare — `desktop_ops.tool_action_class()`
and `whatsapp_ops.tool_action_class()` — and both docstrings explain why:
*"a badge that restates the strictest of nine rows is a ninth thing to keep in
step."* **The consolidation finishes a migration those two modules already began.**

The cost of not doing it is concrete. A verb added to `ops.OPERATIONS` but not
reflected in the badge is a card that promises less than the gate enforces, and
the codebase's own comment calls that **"a security bug rather than a typo."**

---

## 3. Why the function tools are NOT split

`browse` / `desktop_control` keep `action=`. This is deliberate and measured.

- `tools/tools_policy.py` records instructions + tool schemas at **~4.7k tokens
  per completion** — *"more than half the minute"* of Groq's free 8,000 TPM.
- The same file encodes *"one job is one tool: a `browse` job of open/observe/act
  is one job."* Splitting by verb removes that affordance.
- `parallel_tool_calls` is cascade-only. Gemini Live has no backstop.

```text
Canonical capability:   computer.click
        ↓
Existing surface:       desktop_control(action="click")
        ↓
Implementation:         tools/desktop/ops.py
```

Full rationale: [ADR-0014](../decisions/0014-canonical-capability-namespace.md).

---

## 4. Why `browser.page.*` and `browser.control.*` are separate

They are two existing surfaces with different addressing and different failure
modes:

| | `browse` → `browser.page.*` | `web_control` → `browser.control.*` |
|---|---|---|
| Addressing | `data-lumine-ref` stamped into the document | role + name, optionally position |
| Scope | **page generation**; a ref is refused after navigation | **snapshot**; a `web_control` ref cannot be used by `browse` |
| Cost | cheap | costlier; resolves ambiguity into candidates |
| Model policy | *"One control you can already see in an `observe` result"* | *"One control you would have to hunt for"* |

Collapsing them into one `browser.click` would erase a distinction the tool
policy actively relies on, and would make two incompatible ref scopes look
interchangeable. `AGENTS.md` already records that they are *"split by
difficulty, not by feature."*

---

## 5. Why legacy names remain supported

Phase 1 must be backwards compatible. Existing users have:

- `tools.<flat_id>` switches in `lumine.config.json`
- `permissions.tools["desktop_control.click"]` policy entries
- `LUMINE_DISABLED_TOOLS`, `LUMINE_PERMISSIONS` environment values
- possibly hand-edited settings

None of it may break, and no user may be asked to migrate. Resolution is
**additive**, never replacing:

```text
canonical capability      capabilities.computer.click   ← wins if present
        ↓
legacy verb               desktop_control.click          ← kept working
        ↓
tool-level fallback       desktop_control
```

Dotted wins so a future migration can tighten one capability without touching
legacy keys. Legacy keeps working so nobody's existing config silently changes
meaning.

---

## 6. The capability model

Prefer existing names and types. `ops.Operation` and `whatsapp_ops.Operation`
already use `verb`, `action_class`, `element_dependent`, `verify`,
`hold_seconds`, `summary`, `irreversible`. Those names are kept.

```python
@dataclass(frozen=True)
class Capability:
    """One thing Lumine can do, identified canonically."""

    id: str                    # "computer.click"
    surface: str               # "desktop_control"
    verb: str                  # "click"
    category: str              # "computer"

    # -- authority (enforced by permission_gate) --------------------------
    action_class: str          # what the call site passes to the gate
    strict_class: str = ""     # ceiling, when action_class is computed at the
                              # call site; empty means action_class is the
                              # whole truth
    element_dependent: bool = False

    # -- risk: a DIFFERENT axis from authority ----------------------------
    # `action_class` answers "may this run without asking?" and is enforced.
    # `risk` answers "how bad is it if it goes wrong?" and is never enforced.
    # Not interchangeable: media.play is `act` authority and `medium` risk;
    # browser.page.observe is `read` authority and `low` risk. The case a
    # single axis cannot express is *low authority, high risk* — an ordinary
    # act on something fragile.
    risk: str = "low"          # low | medium | high | critical

    # -- addressing --------------------------------------------------------
    addressing: str = "none"   # none | coordinate | element | ref | semantic
    tiers: tuple[str, ...] = ()

    # -- verification ------------------------------------------------------
    verify: str = ""
    hold_seconds: float = 0.0  # >0 ⇒ an unconfirmed outcome blocks a retry
    irreversible: bool = False # property, from hold_seconds

    reads_third_party_text: bool = False
    selectable: bool = True    # False ⇒ must not raise the tool's badge
    summary: str = ""
```

### 6.1 Risk values

| risk | meaning | examples |
|---|---|---|
| `low` | wrong answer at worst; nothing outside the process changes | observe, read, list, search, find |
| `medium` | the machine changes in a recoverable way | open app, focus window, play media, type |
| `high` | something left the process and cannot be taken back | click (unconfirmed), navigate |
| `critical` | money, credentials, or third parties affected | send message, delete, unlink device |

**Declared, never computed, never enforced.** It exists so a future planner can
reason about consequences without a permission change. Phase 1 adds the axis;
nothing reads it beyond the catalog.

---

## 7. Class resolution — the three models

See [`00-architecture-audit.md` §3](00-architecture-audit.md#3-three-class-resolution-models-the-finding-that-drives-phase-1).
Collapsing them would be a regression, so each row states which it uses.

| Tool | Badge source after Phase 1 |
|---|---|
| `desktop_control` | **derived** — `max(action_class)` over its capabilities (already computed today) |
| `whatsapp_read` / `_chats` / `_account` / `send_whatsapp` | **derived** — `max` over **selectable** capabilities (already computed today) |
| `get_weather`, `get_news`, `search_web`, `recall_persona`, `find_app` | single-capability; derived trivially |
| `open_app`, `cancel_task`, `play_media` | single-capability; derived trivially |
| `manage_window` | **declared** `destructive` — `terminate` force-kills, but the row must also describe the mild verbs |
| `browse`, `web_control` | **declared ceiling** `sensitive` — every verb is computed from the DOM |
| `run_task` | **declared floor** `destructive` — a plan may contain an unread step |

A declared entry is not a duplicate truth. It is a value with no derivation,
stored once, where the gate and the badge both read.

---

## 8. Implementation

### Step 1 — baseline

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s agent\tests -p "test_*.py"
```

Confirm **2232 passing, 3 skipped** before changing anything.

### Step 2 — `agent/tools/capabilities.py`

- Module docstring covering §2, §3, §4, §5.
- `Capability` per §6.
- The capability rows.
- A **small** registry. Decision 15 forbids `CapabilityManager` / `Resolver` /
  `Service` / `Factory` / `Provider`:

```python
class CapabilityRegistry:
    def get(self, capability_id: str) -> Capability | None
    def require(self, capability_id: str) -> Capability       # raises
    def for_surface(self, surface: str) -> tuple[Capability, ...]
    def resolve(self, surface: str, verb: str) -> Capability | None
    def surface_class(self, surface: str) -> str
    def ids(self) -> tuple[str, ...]
    def categories(self) -> tuple[str, ...]
    def to_dict(self) -> list[dict]
```

`resolve(surface, verb)` is the deterministic mapping. Duplicate `(surface, verb)`
raises **at import**, not lazily.

### Step 3 — the rows

**`computer.*`** — from `desktop/ops.py::OPERATIONS`, preserving class, tiers,
`element_dependent`, `reads_third_party_text`, `verify`, `hold_seconds`, summary.

| id | surface | verb | authority | addressing | risk |
|---|---|---|---|---|---|
| `computer.observe` | `desktop_control` | `observe` | `read` | none | `low` |
| `computer.move` | `desktop_control` | `move` | `act` | element, coordinate | `low` |
| `computer.click` | `desktop_control` | `click` | `act` ⇧ `sensitive` | element, coordinate | `high` |
| `computer.type` | `desktop_control` | `type` | `act` ⇧ `sensitive` | element, coordinate | `medium` |
| `computer.press` | `desktop_control` | `press` | `sensitive` | none | `medium` |

`INTEGRATED_WINDOWS`, `Integration` and every refusal **stay in `ops.py`** —
only the common authority metadata moves.

**`browser.page.*`** — `open`, `observe`, `act`, `back`, `forward`, `scroll`,
`tab`, `close`. All `read` ⇧ ceiling `sensitive`; `act` and `close` carry `high`
risk, the rest `low`/`medium`.

`TAB_ACTION_CLASS` stays in `browser/actions.py` as the **implementation**; the
registry records the ceiling. The two must not disagree — assert it.

**`browser.control.*`** — `read`, `find`, `search`, `click`, `fill`, `type`,
`press`, `select`, `screenshot`, `state`, `extract`, `back`, `forward`, `reload`,
`close`, `tab`/`pagetabs`, `hover`, `check`, `uncheck`, `drag`, `double_click`,
`right_click`, `focus`, `clear`. All `read` ⇧ ceiling `sensitive`.

**`process.*`, `window.*`, `web.*`, `media.*`, `runtime.*`, `internal.*`** — from
`TOOL_ACTION_CLASS` and each tool's docstring. `manage_window`'s per-verb classes
must be read from its implementation, **not guessed**: `list`/`focus`/`minimize`/
`maximize`/`restore` are `read`/`act`, `close` is `act` (the app may still veto),
`close_all` is `sensitive`, `terminate` is `destructive`.

**`communication.*`** — from `whatsapp_ops.py`, one-to-one:

| id | surface | verb | authority | risk |
|---|---|---|---|---|
| `communication.read_chats` | `whatsapp_read` | `chats` | `read` | `low` |
| `communication.read_unread` | `whatsapp_read` | `unread` | `read` | `low` |
| `communication.search_contact` | `whatsapp_read` | `find` | `read` | `low` |
| `communication.read_contact` | `whatsapp_read` | `contact` | `read` | `low` |
| `communication.read_messages` | `whatsapp_read` | `messages` | `sensitive` | `high` |
| `communication.prepare_message` | `send_whatsapp` | `prepare_draft` | `act` | `low` |
| `communication.send_message` | `send_whatsapp` | `send_text` | `destructive` | `critical` |
| `communication.reply_message` | `send_whatsapp` | `reply` | `destructive` | `high` |
| `communication.forward_message` | `send_whatsapp` | `forward` | `destructive` | `high` |
| `communication.send_attachment` | `send_whatsapp` | `attach` | `destructive` | `high` |
| `communication.react_to_message` | `send_whatsapp` | `react` | `act` | `low` |
| `communication.mark_chat_*` | `whatsapp_chats` | 6 verbs | `act` | `low` |
| `communication.delete_message` | `whatsapp_chats` | `delete` | `destructive` | `critical` |
| `communication.block_contact` | `whatsapp_chats` | `block` | `sensitive` | `high` |
| `communication.read_account` | `whatsapp_account` | `presence`/`devices`/`about` | `read` | `low` |
| `communication.unlink_device` | `whatsapp_account` | `unlink` | `destructive` | `high` |
| `communication.start_call` | `whatsapp_account` | `call` | `destructive` | `high` |

`whatsapp_ops.PATHS`, `ONLY_PATH`, `desktop`/`web` and `supports()` are **path
capability, not action authority** — they stay unchanged.

`filesystem.*` ids are **reserved and documented but not written**. They arrive
in Phase 6.

### Step 4 — convert the sources into views

One at a time, running the suite after each:

1. `desktop/ops.py::OPERATIONS` → rows. Keep `Operation` as a frozen projection so
   every `ops.operation(verb)` caller is untouched.
2. `whatsapp_ops.py::OPERATIONS` → rows. Same; `Operation.tool` maps to `surface`.
3. `tools_permission.TOOL_ACTION_CLASS` → derived/looked-up per §7.
4. `task_runner.STEP_ACTION_CLASS` → lookups. `desktop_act` stays `sensitive`.
5. `tools_registry.card["permission"]` — **unchanged**, still `permission_floor()`.
   Assert the value is identical before and after.

### Step 5 — permission resolution

In `permission_gate.level_for`:

```python
cap = registry.resolve(tool, action)
if cap is not None:
    for key in (f"capabilities.{cap.id}", f"{tool}.{action}"):
        if key in self.policy:
            return self.policy[key]
return self.policy.get(tool, "default")
```

`parse_level_map` already lower-cases keys; dotted keys contain no case-sensitive
characters, so that is safe.

### Step 6 — config document

`settings/validation.py` accepts a `capabilities` section with dotted keys and
**keeps accepting `tools.<flat>` unchanged**. Malformed values behave exactly as
today: warnings for `permissions`, errors for `desktop.input` and `media.enabled`.

### Step 7 — `tool_catalog.py`

`catalog_issues()` gains the new checks. Output keeps its shape and gains an
optional `--capabilities` view. `main()` returns the same codes. **Cards must be
byte-identical** — assert against the current output.

### Step 8 — tests

New `agent/tests/test_capabilities.py`:

- **Registry** — ids unique; `(surface, verb)` unique; required metadata
  non-empty; `action_class` ∈ `permission_gate.ACTION_CLASSES`; `risk` ∈ the
  declared set; `addressing` valid; `tiers ⊆ {element, coordinate}`;
  `hold_seconds ≥ 0`; `irreversible == (hold_seconds > 0)`.
- **Mapping** — `resolve("desktop_control","click").id == "computer.click"`;
  `resolve("browse","open").id == "browser.page.open"`;
  `resolve("web_control","click").id == "browser.control.click"`; no id is both
  `browser.page.*` and `browser.control.*`; every verb in `desktop_ops.VERBS` and
  both browser schemas resolves.
- **Legacy compatibility** — `permissions.tools["desktop_control.click"]` still
  resolves; `tools.desktop_control` still gates; `LUMINE_PERMISSIONS` unchanged.
- **Dotted configuration** — `capabilities.computer.click=ask` resolves and
  **overrides** the legacy key; `=off` refuses.
- **Fail-open/fail-closed preserved** — assert all three known behaviours
  explicitly: `tools.<id>` unknown → `on`; `desktop.input` unknown → `off`;
  `media.enabled` unknown → `off`.
- **Catalog** — 17 cards, unchanged, same `permission` values.
- **Complete path** — a surface invocation resolves to a capability whose
  `action_class` reaches `permission_gate.require`, whose handling matches what
  `permission_floor` promises. **Not** `registry.get()` in isolation.
- **No duplicate authority** — each of the four sources is either a projection or
  a declared floor with a documented reason; derived ones are asserted equal.

### Step 9 — verify

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s agent\tests -p "test_*.py"
python agent\tool_catalog.py --check
python agent\validate_config.py --describe
```

---

## 9. Acceptance criteria

### Architecture
- [ ] `CapabilityRegistry` exists in `agent/tools/capabilities.py`
- [ ] Canonical ids are dotted
- [ ] One authoritative source for capability metadata
- [ ] Domain-specific metadata retained where specialised
- [ ] No duplicate authority tables for the migrated metadata

### Compatibility
- [ ] All 17 function tools still work
- [ ] Tool schemas unchanged
- [ ] Tool cards unchanged
- [ ] Legacy config keys still valid
- [ ] Permission behaviour unchanged
- [ ] Voice behaviour unchanged

### Mapping
- [ ] Every supported surface verb → exactly one capability
- [ ] `browser.page.*` separate from `browser.control.*`
- [ ] Desktop uses `computer.*`; apps `process.*`; messaging `communication.*`

### Testing
- [ ] Full suite passes — 2232+, 3 or fewer skipped
- [ ] Registry, legacy-config, dotted-config and catalog tests pass
- [ ] No unjustified new skips

### Scope
- [ ] No persistent tasks · no filesystem · no LLM planner · no workers
- [ ] No UI redesign · no LiveKit changes · no new voice path

---

## 10. Explicit non-goals

- Splitting any function tool into per-verb tools
- Unifying the fail-open and fail-closed switches
- Changing any `action_class` value
- Rewriting `permission_gate` semantics to make metadata look cleaner
- Writing `filesystem.*` rows (Phase 6) — the ids are reserved, the rows are not
- Anything from Phases 2–10

---

## 11. Rollback

Delete `capabilities.py`, restore the four tables from git. **No data migration
is required**, because no persisted document changes shape — dotted keys are
additive and legacy keys are untouched.

---

## 12. Stop conditions

Stop and report rather than expanding scope if:

- Unifying a duplicate would change runtime behaviour
- A capability's class cannot be determined without reading code Phase 1 should
  not touch
- The catalog output changes for any of the 17 tools
- Anything requires a LiveKit import, a `session.say()`, or a new permission
- Any existing test needs its assertion weakened rather than its code fixed

---

## Report on completion

1. Files changed
2. Capability ids added
3. Legacy mappings preserved
4. Tests added
5. Full test result
6. Any architectural issue discovered
7. Any metadata that could not safely be unified
8. Whether Phase 2 can proceed without additional changes
