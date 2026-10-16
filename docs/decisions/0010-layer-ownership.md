# 0010 — No new subsystem without a named owner, and the IPC seam stays additive

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

The vision asks for nine conceptual subsystems — wake, agent manager, voice agent,
tools, permissions, memory, tasks, island, full UI — and Phase 1 introduces at least
three genuinely new pieces (a resident runtime, a wake service, an island window).

Two failure modes are predictable, and both have already happened in this
repository in a smaller form:

**Ownership failure.** Two components own the same responsibility and drift. The
WhatsApp Web path gained a search-box fallback that `whatsapp.open_chat` did not
have, so a chat the account had but the virtualised list did not render was
unopenable to the send tool — two implementations of one step. Fixing it was a
delegation, and the drift was the finding.

**Protocol failure.** A seam is replaced rather than extended, and every consumer
breaks at once.

## Decision

**Every responsibility has exactly one named owner, and the existing event seam
grows rather than being replaced.**

1. **One owner per responsibility, named in this file.** Before a new subsystem is
   created, the question "which existing subsystem already owns that?" is answered
   in the proposal, not assumed.

   | Responsibility | Owner | May never be duplicated by |
   | --- | --- | --- |
   | Continuous mic capture, keyword detection, utterance capture | wake service (new, Phase 1) | the webview, the agent |
   | Companion state | lifecycle machine in the runtime (new, Phase 1) | any surface; LiveKit's `AgentState` |
   | Worker process lifecycle | `agent_manager.rs` | the webview |
   | Room, session, model stages | `agent/agent.py` | the runtime |
   | Whether an action may happen | `permission_gate.py` | any surface, any tool |
   | What a tool may return | `tool_results.py` | any tool |
   | Spoken-from-code lines | `runtime/voice_line.py` | calling `session.say` directly |
   | Tool progress narration | `runtime/progress.py` | a tool publishing its own |
   | Voice audio ownership in the UI | `features/voice/audioRegistry.ts` (removed with the Focus workspace; the ownership rule stands and needs a new home) | any component holding its own `<audio>` |
   | Skill matching tolerance | `messaging/whatsapp_flow.py`, imported by `media/match.py` | a second edit-distance |
   | Credential storage and injection | `credentials.rs` + `credential_injection.rs` | any command returning a secret |

2. **The IPC seam is additive.** New lifecycle, wake, utterance and suppression
   records are added to `LUMINE_EVENT` and to the runtime's event surface. The
   existing 24 record types keep working, and an older UI must not break on a newer
   worker.

3. **One canonical vocabulary per concept**, exported once. Where a value crosses a
   boundary, both ends import it or both derive it from one definition. The
   `_INVISIBLE` list for the WhatsApp composer is the model: two languages drifted
   and an *empty* composer read as holding a draft, on every chat, forever.

4. **A new command is not a new concept.** Before adding a Tauri command or a tool,
   check whether an existing one already owns it.

## Why not the alternative

Rejected: "we will tidy the seams in a later refactor." The drift is not found by
the refactor; it is found by a user report that a chat will not open.

Rejected: introduce a typed RPC protocol for the new layers now. It is the right
end state, and it is not yet justified — the current seam is proven, it works with
a worker started outside Tauri, and replacing it during a phase that is adding a
resident runtime would mean two hard problems at once. Revisit when the surface
genuinely stops being one JSON line.

## Consequences

- New components must declare their owner in their module docstring, in the same
  style the existing modules use. That is the cheap enforcement mechanism; a
  diagram nobody updates is not.
- When a surface needs something the runtime does not publish, the fix is a new
  record, not a new direct channel from the surface to the worker.
- A proposal that cannot name an owner is a proposal that has not been thought
  through.

## Revisit when

The event seam stops being sufficient — multiple surfaces needing different
projections, or a payload that no longer fits one JSON line comfortably.