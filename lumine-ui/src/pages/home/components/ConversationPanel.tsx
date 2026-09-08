import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { ConversationAgentStatus, ConversationMessage } from "../conversation/types";
import { ConversationMessage as MessageRow } from "./ConversationMessage";
import { Icon, Mark } from "./Icon";
import type { ConversationBubbleVariant } from "../types";

const STATUS_COPY: Record<ConversationAgentStatus, string> = {
  idle: "Ready when you are",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
  error: "Connection needs attention",
};

type ConversationPanelProps = {
  messages: ConversationMessage[];
  agentStatus: ConversationAgentStatus;
  bubbleVariant: ConversationBubbleVariant;
  onClose: () => void;
  onClear: () => void;
  onReset: () => void;
  onAddMessage: (message: Omit<ConversationMessage, "id">) => void;
};

export function ConversationPanel({ messages, agentStatus, bubbleVariant, onClose, onClear, onReset, onAddMessage }: ConversationPanelProps) {
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const scrollElement = scrollRef.current;
    if (!scrollElement) return;
    scrollElement.scrollTo({ top: scrollElement.scrollHeight, behavior: "smooth" });
  }, [messages]);
  const submit = () => {
    const content = input.trim();
    if (!content) return;
    onAddMessage({ role: "user", content, timestamp: "Now", type: "text", status: "complete" });
    setInput("");
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  const sameGroup = (left: ConversationMessage | undefined, right: ConversationMessage | undefined) => Boolean(left && right && left.role === right.role && left.type === right.type);

  return <aside id="conversation-panel" className="conversation-panel" aria-label="Conversation activity">
    <header className="conversation-head">
      <div><div className="conversation-brand"><Mark /><span>Conversation</span></div><p><span className={`conversation-status-dot is-${agentStatus}`} />{STATUS_COPY[agentStatus]}</p></div>
      <div className="conversation-head-actions"><button className="panel-icon" onClick={onReset} aria-label="Reset conversation" title="Reset conversation"><Icon name="reset" size={16} /></button><button className="panel-icon" onClick={onClear} aria-label="Clear conversation" title="Clear conversation"><Icon name="trash" size={16} /></button><button className="panel-icon" onClick={onClose} aria-label="Close conversation activity" title="Close conversation activity"><Icon name="close" size={17} /></button></div>
    </header>
    <div ref={scrollRef} className="conversation-scroll scroll-hidden">
      {messages.length === 0 ? <div className="conversation-empty"><span><Icon name="waveform" size={17} /></span><p>Your conversations with Lumine will appear here.</p><small>Voice moments and transcripts stay quietly in reach.</small></div> : <div className="conversation-list">{messages.map((message, index) => <MessageRow key={message.id} message={message} variant={bubbleVariant} showLabel={!sameGroup(messages[index - 1], message)} showTimestamp={!sameGroup(message, messages[index + 1])} isGroupStart={!sameGroup(messages[index - 1], message)} isGroupEnd={!sameGroup(message, messages[index + 1])} />)}</div>}
    </div>
    <div className="conversation-composer"><textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={onKeyDown} placeholder="Leave a note for Lumine" rows={1} aria-label="Message Lumine" /><button className={input.trim() ? "is-ready" : ""} onClick={submit} aria-label="Send note"><Icon name="send" size={15} /></button></div>
  </aside>;
}
