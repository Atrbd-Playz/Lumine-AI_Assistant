# Conversation architecture

The Home conversation feature is intentionally backend-agnostic:

```text
ConversationPanel (UI)
        |
useConversation (state/actions)
        |
ConversationService (data boundary)
        |
Lumine backend adapter (future)
        |
LiveKit / Python agent
```

## Data model

`ConversationMessage` contains `id`, `role` (`user`, `lumine`, or `system`), `content`, `timestamp`, an optional `type` (`text`, `voice`, or `system`), and an optional `status` (`processing`, `complete`, or `error`).

## Mock mode

`useConversation()` currently defaults to `mockConversationService`, which supplies the two voice transcript examples shown in the Home panel. The mock service has no backend dependencies.

## Connecting the backend

Create a service adapter that implements `ConversationService` and pass it to `useConversation({ service })` from `Home.tsx`, or replace the default service there. Keep LiveKit listeners in that adapter or in a dedicated backend integration hook. They should translate events into the hook actions:

- `USER_TRANSCRIPT`: `addMessage({ role: "user", type: "voice", ... })`
- `LUMINE_THINKING`: `setAgentStatus("thinking")`
- Partial `LUMINE_RESPONSE`: `addMessage` once, then `updateMessage(id, { content })`
- `LUMINE_RESPONSE`: `updateMessageStatus(id, "complete")`
- `LUMINE_SPEAKING`: `setAgentStatus("speaking")`
- `LUMINE_FINISHED`: `setAgentStatus("idle")`
- `ERROR`: `setError(message)`

This keeps LiveKit, STT, TTS, and Python agent details out of the panel components.
