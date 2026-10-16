# 0013 — The greeting is conditional on having nothing to do

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

Every session today ends with a greeting, unconditionally, in
`agent/agent.py`:

```python
await session.generate_reply(
    instructions="Introduce yourself as Lumine. Greet the user briefly and ask how you can help."
)
```

That is correct for the session it was written for: the user pressed a button, a
room opened, and Lumine has nothing to do until they say something. Greeting is the
right behaviour when someone walks into a room.

It is wrong the moment she wakes to a request. "Hey Lumine, what's the weather?"
would produce *"Hello! I'm Lumine. How can I help?"* — and then the user would have
to ask again, because the weather question was consumed by the greeting turn or
interrupted by it. The one-shot flow is preserved by the ring buffer and destroyed
by this.

The symptom is recognisable from the product's own history: the WhatsApp send
confirmation was heard **twice** because two facts about one event reached the model
and it reported on both. Two turns about one wake is the same mistake.

## Decision

**A session greets only when it has no work. If the wake carried an utterance, the
utterance is the first turn and there is no greeting.**

1. The greeting is skipped when the session was opened **with a captured
   utterance**. That is the one-shot path, and it is the common path.
2. The greeting is used when the session was opened with **no** request — a bare
   wake, an explicit "open your interface", or the current button-press flow, which
   stays exactly as it is.
3. **The condition lives in the session**, not in the persona prompt. A prompt
   instruction is a request; a branch is a guarantee. The greeting is a side effect
   of session construction and belongs beside it.
4. The skip is **recorded** — a `greeting` field on the `session_started` record —
   so "she did not answer me" and "she greeted me instead" are distinguishable in
   the log.

## Why not the alternative

Rejected: put "do not greet when the user already asked something" in
`persona_core.md`. It is a soft instruction competing with the model's own impulse
to be welcoming, and it costs tokens on every request forever to govern a situation
that should never reach the model.

Rejected: keep the greeting and rely on barge-in to cut it off. That makes the user
race her greeting to get their own sentence in — a worse version of the two-step
problem.

Rejected: shorten the greeting. The problem is that a greeting happened, not how
long it was.

## Consequences

- One branch at session construction, plus a record field. Nothing else in the
  codebase changes.
- The existing behaviour for the button-press flow is preserved, so nothing
  currently verified regresses.
- Directly testable: with a captured utterance the greeting is not requested;
  without one it is. Both are unit-testable without a model, because the decision
  is a branch and not a sentence.
- The related rule generalises, and is worth stating because it has been learned
  the hard way: **the product speaks the confirmation, not the model.** The WhatsApp
  send confirmation already follows this via `voice_line.speak_line` and
  `allow_model=False`. A greeting is the same class of line — a fact about the
  session, not something the model should be asked to improvise.

## Revisit when

The greeting becomes state-aware in a way that needs the model's judgement, which
would be a different feature (an awareness of how long someone has been away) rather
than a change to this rule.