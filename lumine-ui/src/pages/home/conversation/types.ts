export type ConversationRole = "user" | "lumine" | "system";
export type ConversationMessageType = "text" | "voice";
export type ConversationMessageStatus = "processing" | "complete" | "error";
export type ConversationAgentStatus = "idle" | "listening" | "thinking" | "speaking" | "error";

export type ConversationEmotion = {
  primary: string;
  intensity: number;
  source?: string;
};

export type ConversationMessage = {
  id: string;
  role: ConversationRole;
  content: string;
  timestamp: Date | string;
  sessionId?: string;
  type?: ConversationMessageType;
  status?: ConversationMessageStatus;
  emotion?: ConversationEmotion;
};

export type ConversationToolEvent = {
  id: string;
  type: "tool";
  name: string;
  status: "started" | "completed" | "failed";
  timestamp: Date | string;
  sessionId?: string;
  summary?: string;
  durationMs?: number;
};

export type ConversationSystemEvent = {
  id: string;
  type: "system";
  text: string;
  timestamp: Date | string;
  sessionId?: string;
};

export type ConversationItem = ConversationMessage | ConversationToolEvent | ConversationSystemEvent;

export type ConversationService = {
  getInitialMessages: () => ConversationMessage[];
};
