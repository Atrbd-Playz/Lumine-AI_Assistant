import type { ConversationMessage as ConversationMessageData } from "../conversation/types";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import type { ConversationBubbleVariant } from "../types";

type ConversationMessageProps = {
  message: ConversationMessageData;
  variant: ConversationBubbleVariant;
  showLabel: boolean;
  showTimestamp: boolean;
  isGroupStart: boolean;
  isGroupEnd: boolean;
};

export function ConversationMessage({ message, variant, showLabel, showTimestamp, isGroupStart, isGroupEnd }: ConversationMessageProps) {
  const isUser = message.role === "user";
  const label = isUser ? "You" : message.role === "lumine" ? "Lumine" : "System";
  const timestamp = message.timestamp instanceof Date ? message.timestamp.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : message.timestamp;

  const groupClass = `${isGroupStart ? "group-start" : ""} ${isGroupEnd ? "group-end" : ""} ${!isGroupStart && !isGroupEnd ? "group-middle" : ""}`;
  return <Bubble variant={variant} align={isUser ? "end" : "start"} className={`conversation-message ${message.role} ${groupClass} ${message.status === "processing" ? "is-processing" : ""}`}>
    <BubbleContent><div className={`conversation-message-head ${showLabel ? "" : "is-hidden"}`}><span className="conversation-message-label">{label}</span></div><p>{message.content || "Listening for a response…"}</p>{showTimestamp && <time className="conversation-message-time">{timestamp}</time>}</BubbleContent>
  </Bubble>;
}
