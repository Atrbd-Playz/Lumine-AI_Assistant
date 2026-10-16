# 0011 — Build vertically; the roadmap preserves direction, not work

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

The vision is large: ten phases, a local AI stack, proactive behaviour, memory,
offline operation. The realistic risk is not that the work is too hard, it is that
the visible surface grows faster than the product underneath it — which produces
exactly the outcome the vision forbids: a dashboard-heavy app, a chatbot window, and
a pile of unrelated demos.

The repository already carried the counter-example. The Focus workspace *was* a
substantial feature — three implementations of the same statistics kept in agreement
by a contract test, a layout engine, a timer, a player, a note renderer — and it was
the one part of the product marked "a work in progress" in the README. It was
valuable, it was unconnected to the companion experience, and it was also the largest
thing in the tree. This decision has since been acted on directly: the entire Focus
workspace has been removed — page, Rust stores and commands, Python readers and its
documentation — so the counter-example now exists as history rather than as weight.

## Decision

**Phases preserve direction; they do not authorise implementation. Build one
vertical slice at a time, and a phase is not finished because its features landed —
it is finished when the slice works end to end for a real person.**

1. **A vertical slice, not a layer.** The first slice is: *wake detection → island
   appears → one-shot command → answer → island retracts.* Not "the wake service",
   not "the island".
2. **A later-phase feature is out of scope until its phase.** Not forbidden —
   refused unless its absence blocks the slice in front of you.
3. **No new subsystem, dependency or page before the slice needs it.** A dependency
   that carries its own runtime or model weights is a product decision with a
   licence and a size budget.
4. **Every slice is measured against the vision's success criteria**, not against
   "the feature is implemented".
5. **Anything not needed by the current slice is recorded, not built.** That is
   what `ROADMAP.md` is for.

Concretely, the order this forbids:

- Proactive notifications before the idle/sleep lifecycle exists (there is nothing
  to be respectful *relative* to).
- Local LLM inference before the wake path is honest (offline is only a product
  story if wake is local, and wake is local first anyway).
- Persistent memory before the lifecycle state machine (memory without lifecycle
  makes answers depend on restart count).
- A second Settings page section for anything not yet configurable.
- Anything that would make the full UI more load-bearing.

## Why not the alternative

Rejected: implement the layer independently, in dependency order. That produces a
wake service nobody can hear, an island with no lifecycle to render, and a lifecycle
with no user — three correct components that do not make a companion. The dependency
order looks safer and delivers nothing observable until the end.

Rejected: treat the roadmap as a backlog and pull the easiest item forward. The
easiest item is almost always in the full UI, and that is the trap.

## Consequences

- The Phase 1 plan is written as a slice with a measurable exit, not as a list of
  components. See [`docs/plans/wave-1-always-on-runtime.md`](../plans/wave-1-always-on-runtime.md).
- Phases are allowed to change. A phase list written before the first slice exists
  is a guess; the rule that survives is *vertical and incremental*.
- Saying no to a good idea is a normal outcome, and the correct way to handle it is
  to write down where it fits — not to quietly start it.
- The existing rule from the repository applies: **do not turn Lumine into a
  dashboard-heavy productivity app or a collection of unrelated AI demos.** A
  feature that cannot be reached from one spoken sentence a person would actually
  say, and cannot be justified from the North Star, is out regardless of how
  finished it is.

## Revisit when

A later-phase feature becomes a genuine blocker for the current slice — and then it
is promoted into the slice with its reasons written down, not pulled in silently.