# Home feature

This folder contains the pieces that make up Lumine's home screen.

- `../Home.tsx` is the page shell. It owns shared state such as the current voice state, theme, and settings dialog visibility.
- `types.ts` contains the TypeScript shapes shared by more than one component.
- `constants.ts` contains default values and static copy. Keeping these out of JSX makes content easy to find.
- `utils.ts` contains small pure functions. These do not render UI and are simple to test independently.
- `components/` contains visual areas. Each component receives values and callbacks through props instead of reaching into page state.

## Where to make a change

- Change navigation buttons in `components/Sidebar.tsx`.
- Change the avatar or cursor gaze in `components/Presence.tsx`.
- Change the main stage and voice controls in `components/MainSpace.tsx`.
- Change tasks, recent context, or the composer in `components/ContextPanel.tsx`.
- Change appearance controls in `components/AppearanceDialog.tsx`.
- Change conversation data contracts and backend adapters in `conversation/`.
- Change the shadcn Bubble primitive in `../../components/ui/bubble.tsx`.
- Change shared colors and layout rules in `../index.css`.

The page shell should stay small. When a new feature grows beyond a few lines of JSX, give it its own component and pass the smallest useful props from `Home.tsx`.

## Conversation integration

`ConversationPanel` is a supporting activity layer. It reads only from `useConversation`, which currently defaults to `mockConversationService`; it does not know about LiveKit, STT, TTS, or the Python agent.

When the real backend is ready, add a `ConversationService` adapter in `conversation/` and pass it to `useConversation({ service })` in `Home.tsx`. Keep LiveKit event listeners in that adapter or a dedicated integration hook, translating `USER_TRANSCRIPT`, `LUMINE_THINKING`, `LUMINE_RESPONSE`, `LUMINE_SPEAKING`, `LUMINE_FINISHED`, and `ERROR` into the hook actions. For streaming responses, call `addMessage` once and use `updateMessage` for each partial response.

Conversation bubbles use the generated shadcn component at `src/components/ui/bubble.tsx`. The component owns structure and variants; `index.css` supplies Lumine's chat-specific colors and typography through the Appearance settings.
