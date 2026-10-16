# 0009 — Self-audio protection ships before wake does

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

The microphone is a speaker and the speaker is a microphone:

```text
TTS output → speaker → air → microphone → detector → wake → TTS output → …
```

Without protection, a companion with an always-on keyword detector interrupts
herself, answers herself, and eventually holds a conversation with nobody. On a
desk with speakers at full volume this happens within minutes.

The repository already contains two pieces of the answer, which is useful:

- `permission_gate._announce` speaks with `allow_interruptions=False`, because an
  approval cut off mid-sentence is an approval that is sometimes never heard.
- `agent.py` already publishes `false_interruption` records, so a wrong turn
  interrupt is measurable today.

Neither is wake protection. They are turn-level policy inside a live session.

## Decision

**Self-audio protection is a first-class mechanism, and it ships with the first
wake detector — not after it.**

Minimum viable set, in order:

1. **Suppression during synthesis.** While Lumine is speaking, the detector is
   muted. Not "less sensitive" — muted. This is what prevents the direct loop.
2. **A cooldown after speech ends.** The room is still loud, and the tail of her
   own voice plus the room's ringing must not re-arm detection instantly.
3. **A confidence threshold**, configurable, because a machine in a noisy room is
   a machine that will eventually trigger on something else.
4. **A suppression signal on the record.** `suppression` events carry the reason, so
   a false trigger that still happened is debuggable rather than mysterious.

Deferred, explicitly: acoustic echo cancellation, and any reference-channel
approach. They are the right answer eventually and the wrong answer for a first
slice, because they are a second subsystem with their own failure modes. A wake
system that works without AEC is strictly better than no wake system.

## Why not the alternative

Rejected: ship wake first and add protection when it becomes annoying. It becomes
annoying on first contact, it looks like a ghost, and it is the kind of defect that
gets blamed on "AI".

Rejected: rely on AEC alone. AEC is a solution to a narrower problem than
suppression, is harder to get right across devices and sample rates, and does
nothing about a false trigger from a television or a colleague saying Lumine's
name.

## Consequences

- Suppression state must be driven by something reliable. The runtime has to know
  "she is speaking" from the lifecycle machine — which is another reason
  [0005](0005-single-lifecycle-authority.md) exists before the island does.
- There is an unavoidable trade-off in the first slice: suppression during synthesis
  means a user who says "Lumine" *while she is answering* is not heard. That is the
  correct trade for a first slice, it must be *documented* rather than discovered,
  and it is a barge-in problem for Phase 4 — not something to solve by weakening
  suppression.
- Phase 1 must measure the false-trigger rate, not just the false-reject rate. A
  detector with 99% recall that fires on a fan is unusable, and only counting misses
  would hide it.
- The whole test must be staged so that nothing speaks and nothing is clicked before
  the read-only assertions pass — the ordering *is* the safety argument, exactly as
  `desktop_smoke.py` already does it.

## Revisit when

Barge-in (Phase 4) needs suppression to be *interruptible* rather than absolute,
which is the natural place to replace mute with a ducked threshold and AEC.