# 0003 — Wake is local, continuous, and separate from the agent

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

The vision requires that Lumine be reachable after Windows starts, without the user
opening anything. That means something must be listening from boot.

The obvious implementation is to start a conversation and let the existing pipeline
decide whether somebody spoke. It is also wrong in three ways at once:

1. **It cannot work while asleep.** The microphone today is a LiveKit publication
   created by the webview (`room.localParticipant.setMicrophoneEnabled(true)`). A
   room exists only during a conversation, so "listening while asleep" would mean
   holding a room open forever — paying for it in bandwidth, provider minutes, and
   a session that never ends.
2. **It streams silence to a provider.** Continuous audio to a cloud STT to decide
   whether someone said "Lumine" is the privacy and cost failure the vision names
   explicitly.
3. **It inverts the dependency.** Wake must work when the worker is crashed and the
   provider is down. Anything that needs the AI stack up cannot hear the user.

## Decision

**The wake/attention layer is a separate component with a local audio input, a
local detector, and exactly one output: *wake, plus the captured utterance*.**

It must satisfy all of:

- **Local capture.** It owns a microphone path of its own. It does not borrow
  LiveKit's.
- **Local detection.** No provider is contacted to decide whether the user spoke.
- **One output vocabulary.** A wake event carrying the phrase, a confidence, and
  the preserved utterance. Nothing else crosses the boundary.
- **No upstream dependency.** A dead worker, a bad config, an expired credential or
  a dropped network must not affect wake.
- **Continuous audio never leaves the process.** Only what follows the wake is
  eligible for a provider, and even then see
  [0007](0007-one-shot-commands.md) for the local-STT-first rule.

## Why not the alternative

Rejected: "just start the session and let the model decide it wasn't addressed."
Rejected: "send a low-rate transcript to Groq and match the name." Both make
wake depend on the thing wake is supposed to precede, and the second pays for
silence.

## Consequences

- A new dependency is unavoidable (a capture path and a keyword-spotting engine).
  Per [`VISION.md`](../../VISION.md) that is a product decision, not a convenience,
  so Phase 1 evaluates candidates against: local-only, no account, works offline,
  small enough to be boring, and no bundled runtime that competes with the
  worker's Python.
- The agent's audio path and the wake path are two different paths into two
  different processes. They must not be unified later for tidiness; a unified path
  is a continuous microphone.
- Silence becomes a measurable cost and must be, so idle CPU and idle memory are
  part of Phase 1's exit criteria, not an afterthought.

## Revisit when

A wake engine turns out to require a runtime the product is unwilling to ship, or
Phase 8 makes every stage local anyway and the two paths can finally be merged
safely.