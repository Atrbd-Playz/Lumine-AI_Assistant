# 0001 — Lumine is a companion, not an application

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

Lumine is a working voice session with a real presentation around it: a Tauri
window, a React dashboard, a settings AI Control Center, seventeen gated tools,
an Activity feed, a Focus workspace (removed since this decision was written).
Every one of those is good work, and together
they produce a shape the product does not want: an application the user opens in
order to talk to a companion.

The gap is not quality, it is **lifetime**. Today "Lumine is available" and "the
app is open" are the same statement, because Rust stops the worker on
`ExitRequested` and nothing else ever starts it.

## Decision

**Lumine's product definition is a companion that is present without being opened.
The full UI is her home, not her existence.**

Concretely, and permanently:

1. A resident runtime starts with the OS and stays hidden.
2. She is reachable by voice with no window open.
3. The full UI opens only on an explicit request — spoken, from the tray, or from
   a desktop shortcut.
4. No surface may be a prerequisite for another. Voice must work with the island
   alone; the island must work with no dashboard bundle loaded.

## Why not the alternative

The alternative is the current architecture: the app is the product, and the
companion runs inside it.

That shape is not *wrong* — it is what most desktop AI apps are — but it makes
three of the twelve goals in the decision rule unreachable: invisible background
operation (2: the app occupies the desktop), fast wake (3: nothing is listening
until launch), and full UI as optional (5: the UI is the only way in). Retrofitting
them later means moving the audio path, the window model and the lifecycle out of a
process that already owns all three, which is far more expensive than building them
separately now.

## Consequences

- The app's lifetime stops being the runtime's lifetime. This is Phase 1's first
  real task and it is not small.
- Anything that only works while a window is open is a defect in the target
  architecture, not a limitation.
- The dashboard must not become load-bearing. If a capability can only be reached
  from the full UI, it is misfiled — see [0006](0006-island-as-primary-surface.md).

## Revisit when

Never. If the product ever needs to be an application the user opens, that is a
different product and it should be a different repository.