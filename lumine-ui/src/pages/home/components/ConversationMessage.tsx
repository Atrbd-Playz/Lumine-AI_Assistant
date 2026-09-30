import type { ConversationMessage as ConversationMessageData } from "../conversation/types";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { formatClock } from "../utils";
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
  /* One formatter, the stage's. This row used to render `3:07` while the corner
     clock two feet away rendered `03:07`, and there is no way to read those as
     one clock; a string that arrives already formatted is passed through, because
     it was not this row that made it and re-parsing it would invent an instant
     from text that may not hold one. */
  const timestamp = message.timestamp instanceof Date ? formatClock(message.timestamp) : message.timestamp;
  /* `dateTime` wants an instant, not the display string above, and the field is
     `Date | string`. A Date is already one; a string is parsed back when it can
     be and the attribute is dropped when it cannot, because `dateTime="10:30 AM"`
     is a machine-readable field containing a machine-unreadable value — which is
     worse than no field at all, since the whole point is that software can read
     what a screen reader reads aloud. */
  const at = message.timestamp instanceof Date ? message.timestamp : new Date(message.timestamp);
  const dateTime = Number.isNaN(at.getTime()) ? undefined : at.toISOString();

  const groupClass = `${isGroupStart ? "group-start" : ""} ${isGroupEnd ? "group-end" : ""} ${!isGroupStart && !isGroupEnd ? "group-middle" : ""}`;
  return <Bubble variant={variant} align={isUser ? "end" : "start"} className={`conversation-message ${message.role} ${groupClass} ${message.status === "processing" ? "is-processing" : ""}`}>
    <BubbleContent><div className={`conversation-message-head flex items-baseline justify-between gap-3 ${showLabel ? "" : "is-hidden"}`}><span className="conversation-message-label">{label}</span></div><p>{message.content || "Listening for a response…"}</p>{showTimestamp && <time className="conversation-message-time" dateTime={dateTime}>{timestamp}</time>}</BubbleContent>
  </Bubble>;
}
