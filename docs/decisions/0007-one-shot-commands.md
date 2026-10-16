# 0007 — "Hey Lumine, what's the weather?" must work as one interaction

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

A one-shot command is not a convenience. It is the difference between a companion
and a voice assistant that you operate.

```text
WANTED                                  THE SHAPE WE REFUSE
"Hey Lumine, what's the weather?"       "Hey Lumine."
                                        Lumine: "Yes, Master?"
                                        "What's the weather?"
                                        Lumine: "It's 24 degrees."
```

The refused shape costs a second of latency, a turn of patience, and the illusion
that a person is on the other end.

The naive implementation of the wanted shape is the dangerous one: detect the wake
phrase, then start a room and listen. LiveKit room connect plus agent dispatch is
measured in seconds — token mint, `room.connect`, `setMicrophoneEnabled`, dispatch,
and the agent's own pipeline build. The user finishes saying "what's the weather?"
long before the room is listening. **The request is lost, and the user's second
attempt is the one that works.** That is the exact bug class this repository has
already paid for twice: the WhatsApp pre-warm raced the tool it was meant to help,
and the composer shortcut typed into the previous chat's composer.

## Decision

**One-shot is an acceptance criterion, and it is satisfied by capturing the
utterance locally and handing it to the session — not by listening to a room that is
still connecting.**

1. **The wake service keeps a ring buffer.** Long enough to cover the wake phrase
   and a short continuation window.
2. **On wake, the utterance is extracted locally** from that buffer — pre-roll plus
   speech until an endpoint.
3. **The utterance is handed to the session as its first turn.** The agent starts
   whenever it is ready; the words were already safe.
4. **No greeting.** There was already a request to answer — see
   [0013](0013-greeting-conditional.md).
5. **The island acknowledges immediately**, before the utterance is even
   transcribed. Feedback must not wait on the agent.
6. **A wake with no usable utterance degrades to a normal wake**: the island shows
   listening, and she waits for the rest of the sentence.

The two acceptable ways to produce the utterance text:

1. **Local STT on the buffer** — the target. No network, no privacy cost, no
   provider latency.
2. **One cloud STT call on the captured utterance only** — after wake, never
   before. This is materially narrower than streaming silence to a provider and is
   permitted by the privacy rule, but it is billable and adds latency, so it is a
   fallback.

**Rejected:** replaying the buffered audio into the room. It is more faithful
(acoustic context, prosody) and it complicates turn detection for no user-visible
gain over local transcription. Recorded as considered.

## Why not the alternative

Rejected: "buffer enough audio to cover the worst-case connect time." It converts
a one-second delay into a multi-second audio blob per wake, fights the room's
endpointing, and makes every wake pay for the slowest possible startup.

Rejected: "start the worker and the room at Windows boot so wake is instant." That
is a permanently open room — continuous provider audio, a session that never ends,
and it contradicts [0003](0003-wake-layer-separation.md) on privacy as well as
cost.

## Consequences

- Phase 1 gains a real dependency: a local STT engine. It is the first thing that
  is expensive to add later, because the whole one-shot path is built on it, so it
  gets evaluated in the same written comparison as the wake engine.
- Latency accounting splits into two numbers to report separately: **wake → visible
  acknowledgement** (milliseconds) and **utterance → first spoken word** (seconds).
  Optimising the second while regressing the first is a trade, not a win.
- The captured utterance is third-party-shaped input: it is text a human said, but
  it also carries whatever was in the room. It enters the same untrusted-wrap
  discipline as a tool result.
- If local STT proves too heavy for the target hardware, the fallback is one
  cloud call on the utterance — not a permanent room — so the architecture survives
  that failure without changing.

## Revisit when

A wake engine reports the wake **and** a transcript in one step with better
accuracy than a separate STT pass; then the two-stage design is redundant and can
be collapsed.