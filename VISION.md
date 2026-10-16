# Lumine — Product Vision

> **Lumine should feel like a companion living quietly on the computer, not an
> application the user has to open.**
>
> **She should sleep when not needed, hear when called, appear when addressed,
> understand the complete request, act when authorized, communicate naturally, and
> disappear again when the interaction is over.**
>
> **The full UI is her home, not her existence.**

Recorded 2026-10-05. This document is the *why*; [`ARCHITECTURE.md`](ARCHITECTURE.md)
is the *what*; [`ROADMAP.md`](ROADMAP.md) is the *when*; [`docs/decisions/`](docs/decisions/)
is the record of what we chose and what we refused. Where any of them disagree,
this file is the tiebreak, and [`docs/gap-audit.md`](docs/gap-audit.md) records
where today's code has not caught up with it yet.

## What Lumine is

Lumine is a **desktop AI companion and agent**. She is:

- **voice-first** — the fastest path from a person to a result is speaking
- **hands-free** — nothing about ordinary use requires touching anything
- **locally attentive** — she can hear that she is being called without
  broadcasting a microphone to a provider to find out
- **visually expressive** — she has a face, a presence, and a way of showing what
  she is doing, because "is she listening?" is a question a face answers faster
  than a status bar
- **context-aware** — a follow-up sentence should not need the wake phrase again
- **capable of using tools** — a browser, a program, a message, a music folder
- **capable of controlling the computer with permission** — real agency, bounded
  and audited
- **capable of multi-step work** — "find a relaxing playlist and play it" is one
  request, not seven
- **provider-flexible** — cloud or local inference; the attention layer is never
  coupled to one vendor
- **available immediately after Windows starts**
- **mostly invisible when not needed**

The intended experience sits closer to **Jarvis / Siri / Alexa, plus an
anime-style desktop companion, plus an autonomous computer-use agent**. It does
not sit closer to **ChatGPT in a desktop window.**

## What Lumine is not

This list is a product constraint, not a description of what is missing.

| Not this | Why |
| --- | --- |
| A generic chatbot | A chatbot waits to be opened and answers when asked. Lumine is already there. |
| A dashboard-heavy productivity app | Panels are where things are *configured*, not where they are *used*. |
| A voice assistant window | The window is the thing we are trying to stop needing. |
| A permanently open application | An app the user must keep open is an app the user can close, and then she is gone. |
| An uncontrolled automation bot | Hands without a gate are a hazard, not a feature. |
| A pile of unrelated AI demos | Every tool must be reachable from one sentence a person actually says. |

## The core principle

**Always available, but not always visible.**

```text
Windows boots
    ↓
Lumine's runtime starts, hidden          ← wake listener, lifecycle, nothing else
    ↓
SLEEPING — the user uses their computer normally
    ↓
"Hey Lumine, what's the weather?"
    ↓
wake detected, island appears instantly  ← feedback before any AI is awake
    ↓
LISTENING → THINKING → SPEAKING
    ↓
IDLE — session alive, island still there  ← "Go to YouTube" needs no wake phrase
    ↓
inactivity
    ↓
island retracts, agent disconnects, UI stays closed
    ↓
SLEEPING — only the wake listener is awake
```

The rule that makes this honest: **when nothing is being addressed, Lumine costs
almost nothing and occupies almost nothing.** Not a window. Not a room. Not a
model. Not a browser.

## The three surfaces, and what each one is for

Most product confusion in this repository's future comes from putting a feature in
the wrong place. There are exactly three interactive surfaces, and a feature
belongs in exactly one of them.

| Surface | Answers | Must never |
| --- | --- | --- |
| **The Island** — a small top-centre surface that appears on wake and retracts when done | "I heard you. Here is what I'm doing. Here is what I need from you." | Become a dashboard, or need the full UI to exist |
| **Voice** — the conversation itself | "What's the weather?", "Sleep." | Be the only way to reach a capability |
| **The Full UI** — her home and control centre | "Show me everything; let me configure her properly." | Be a prerequisite for basic interaction, or open on its own |
| *(the tray icon is part of the runtime, not a surface)* | Manual wake, manual open, quit | Become a second settings surface |

A practical test before adding a UI element: **would this be useful in a moment
when the user has just spoken one sentence and is waiting?** If yes, it belongs
in the island. If it is configuration, inspection, or history, it belongs in the
full UI. If it is neither, it may not belong at all.

## One-shot commands are the acceptance criterion

This is the single most important behavioural requirement, and it is the one most
likely to be implemented wrong:

> **"Hey Lumine, what's the weather?" must work as one interaction.**

Not:

```text
"Hey Lumine."  →  "Yes, Master?"  →  "What's the weather?"  →  answer
```

That two-step shape is the exact experience of every voice assistant that made
the user feel they were *operating* a machine. It costs a second of latency, a
turn of the user's patience, and the illusion that Lumine is a person rather than
a program with a microphone.

The implication is architectural, not a matter of wording: **the wake detector
must hand the rest of the utterance onwards**, because the room and the agent take
real time to come up and the user's sentence does not wait for them. A system that
detects the wake phrase and then starts listening to a room that isn't connected
yet has thrown away the request. See [`ARCHITECTURE.md`](ARCHITECTURE.md) →
*One-shot commands* and [ADR-0007](docs/decisions/0007-one-shot-commands.md).

Related, and also non-negotiable: **when she wakes to a complete request, she must
not greet first.** Greeting on session start is correct for a room the user opened
by pressing a button; it is wrong for a wake with a command already attached to it.
See [ADR-0013](docs/decisions/0013-greeting-conditional.md).

## Idle is not sleeping

Two different states, two different costs, and conflating them breaks the product
in both directions:

- **IDLE** — the conversation is alive. The next sentence needs no wake phrase.
  The agent is connected. This is what makes "Open Chrome." → "Go to YouTube." →
  "Search for relaxing music." feel like one session instead of three ceremonies.
- **SLEEPING** — no conversation. The agent is disconnected or stopped. Only the
  wake listener is live.

An idle timeout that fires while she is executing a task, waiting on an approval,
speaking, or recovering from an error is not an idle timeout, it is a bug. The
timeout must be *smart*: it suspends on work and resumes when the work ends.

## Sleep is a command, not a shutdown

"Sleep, Lumine" and "go to sleep" are part of the product surface. They return her
to `SLEEPING`; they do not kill the process, do not close the full UI if the user
opened it deliberately, and do not stop the wake listener. A feature that can only
be triggered by killing a window is not a feature.

## She must not wake herself

The microphone is a speaker, and the speaker is a microphone. An always-on
detector plus Lumine's own voice is a feedback loop that will make her interrupt
herself, answer herself, and eventually hold a conversation with nobody.

Every wake implementation must ship with, at minimum: suppression during
synthesis, a post-speech cooldown before detection re-arms, and a confidence
threshold. Echo cancellation is a later refinement, not a prerequisite for not
being broken on day one. See [ADR-0009](docs/decisions/0009-self-audio-protection.md).

## Personality is a product contract

She is warm, sweet, caring, intelligent, playful, dependable, approachable, and
lightly funny. She is not excessively formal, and she does not become a corporate
assistant because a feature needed a label.

This has to hold across *every* surface, which means it is enforced in code and not
only in a prompt:

- `agent/prompts/persona_core.md` is what every model request carries.
- `agent/prompts/persona.md` is the full persona, read on demand.
- Spoken-from-code lines (`runtime/voice_line.py`) are written in her voice,
  because a line the *product* speaks is still Lumine speaking.
- The island's copy is her copy. A notification that reads like a stack trace is a
  defect even if it is accurate.

Preserve `prompts/persona.md` and the avatar system as contracts. Implementation
convenience must never be the reason she becomes more formal, because convenience
is what a generic assistant is made of.

## Privacy is a design property, not a policy page

The always-on microphone is the most sensitive thing this product will ever hold,
and the whole architecture turns on one line:

> **Wake detection is continuous, local, and lightweight. AI processing begins
> after wake.**

Shipping a wake system that streams microphone audio to a cloud STT to decide
whether somebody said her name would mean paying for silence and spending the
user's privacy on it. The architecture must make that impossible by construction
rather than by policy: the audio path that runs continuously and the path that
reaches a provider must be different paths, owned by different components, with
only a handoff between them at the moment of wake.

The same logic says the honest local/offline story is worth preserving: local wake,
local VAD, local STT/LLM/TTS are all *allowed*, and nothing decided today should
make them impossible tomorrow.

## Performance is a feeling

She should feel instantaneous, and instantaneous is a budget:

```text
wake detected  →  island acknowledges  →  command captured  →  agent works
      (ms)              (immediate)            (already done)        (seconds)
```

Two rules follow, and both are about *where the work happens*:

1. **Never make the user wait for the full UI to launch before acknowledging
   them.** Feedback belongs to the wake path, which is the cheapest thing running.
2. **Never make wake depend on the AI stack being up.** Wake detection must work
   when the worker is crashed, the provider is down, and the configuration is
   wrong. A companion who cannot hear you because her brain is disconnected is not
   a companion.

## Proactive, eventually — and respectful

Proactive behaviour (reminders, scheduled tasks, background monitoring, task
completion notices, contextual suggestions) is explicitly in scope for the future
and explicitly *not* for the first year of the product. The governing philosophy:

> **Available, aware, but respectful.**

Proactive means Lumine chooses to speak when she has something worth saying. It
never means filling silence, never means interrupting a task in progress, and
never means escalating frequency to look alive.

## Success criteria

A clean install, on a machine that has just finished starting Windows, must behave
like this without the user opening anything:

1. **Nothing appears.** No window, no tray clutter, no notification.
2. The user says **"Hey Lumine, what's the weather?"**
3. The island appears *immediately* and visibly acknowledges that she heard them.
4. The rest of the utterance is captured. Nothing is lost to startup.
5. She answers out loud. The island shows what she is doing.
6. The island stays for a follow-up. **"Open Chrome."** works with no wake phrase.
7. The user stops. After a short idle period: the island retracts, the agent
   disconnects, the wake listener is still listening, the full UI never opened.
8. **"Arise, Lumine."** starts the whole cycle again.
9. **"Lumine, open your interface."** — and only then does the full UI appear.
10. **"Sleep, Lumine."** returns her to silence without closing anything the user
    was using.

Everything in [`ROADMAP.md`](ROADMAP.md) is in service of that paragraph.

## The decision rule

When two implementations are both defensible, prefer the one that better serves:

1. invisible background operation
2. fast wake
3. one-shot voice commands
4. temporary island interaction
5. full UI as optional
6. independent agent lifecycle
7. tool-based computer control
8. permission-aware actions
9. local/cloud model flexibility
10. future proactive behaviour
11. low resource usage
12. maintainability

Do not optimise for the easiest version *today* when it creates debt that blocks
one of those twelve. That is the difference between a prototype that works once
and a companion that still works in a year.

## Before implementing anything

Ask, in this order:

- **What user problem does this solve?** Anything that does not answer this is a
  demo.
- **Which phase does it belong to?** See [`ROADMAP.md`](ROADMAP.md). A feature from
  a later phase implemented early will be built on assumptions that later phases
  invalidate.
- **Which surface does it belong in?** Island, full UI, tray, or voice. See the
  table above. "The full UI, because that's where the code is" is not an answer.
- **Does it conflict with a recorded decision?** [`docs/decisions/`](docs/decisions/).
  If it does, the decision gets superseded explicitly, in writing, with a reason.
- **Is a new dependency actually necessary?** Most of what Phase 1 needs is audio
  plumbing and window management. A dependency that carries its own model weights,
  its own runtime, or its own network path is a product decision, not a convenience.

And the standing refusal, which has killed good features before: **do not turn
Lumine into a generic chatbot, a dashboard-heavy productivity app, a normal voice
assistant window, a permanently open Electron/Tauri application, an uncontrolled
computer automation bot, or a collection of unrelated AI demos.**