export type ConversationRole = "user" | "lumine" | "system";
export type ConversationMessageType = "text" | "voice" | "system";
export type ConversationMessageStatus = "processing" | "complete" | "error";
export type ConversationAgentStatus = "idle" | "listening" | "thinking" | "speaking" | "error";

export type ConversationMessage = {
  id: string;
  role: ConversationRole;
  content: string;
  timestamp: Date | string;
  type?: ConversationMessageType;
  status?: ConversationMessageStatus;
};

export type ConversationService = {
  getInitialMessages: () => ConversationMessage[];
};
