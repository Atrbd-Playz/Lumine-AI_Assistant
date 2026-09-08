import type { ConversationMessage, ConversationService } from "./types";

const MOCK_MESSAGES: ConversationMessage[] = [
  {
    id: "mock-user-1",
    role: "user",
    content: "Play something calm while I study.",
    timestamp: "8:43 PM",
    type: "voice",
    status: "complete",
  },
  {
    id: "mock-lumine-1",
    role: "lumine",
    content: "Ambient focus is playing. I’ll keep the volume low.",
    timestamp: "8:43 PM",
    type: "voice",
    status: "complete",
  },
];

export const mockConversationService: ConversationService = {
  getInitialMessages: () => MOCK_MESSAGES.map((message) => ({ ...message })),
};
