export type ConversationRole = "user" | "lumine" | "system";
export type ConversationMessageType = "text" | "voice";
export type ConversationMessageStatus = "processing" | "complete" | "error";
/**
 * The transcript header's status.
 *
 * `connecting` is here because the header used to say "Ready when you are" while
 * a room was still being negotiated — which is the transcript's version of the
 * same lie the avatar was telling, in the one place a person reads a sentence
 * rather than watches a face. Widen the union only with a line of copy to go with
 * it; `ConversationPanel` has a `Record` over it and will otherwise not compile.
 */
export type ConversationAgentStatus = "idle" | "connecting" | "listening" | "thinking" | "speaking" | "error";

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
