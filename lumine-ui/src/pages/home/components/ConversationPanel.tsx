import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { ConversationAgentStatus, ConversationItem, ConversationMessage } from "../conversation/types";
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
  items?: ConversationItem[];
  agentStatus: ConversationAgentStatus;
  bubbleVariant: ConversationBubbleVariant;
  onClose: () => void;
  onClear: () => void;
  onReset: () => void;
  onAddMessage: (message: Omit<ConversationMessage, "id">) => void;
};

export function ConversationPanel({ messages, items, agentStatus, bubbleVariant, onClose, onClear, onReset, onAddMessage }: ConversationPanelProps) {
  const [input, setInput] = useState("");
  const [showNewMessages, setShowNewMessages] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const scrollElement = scrollRef.current;
    if (!scrollElement) return;
    const nearBottom = scrollElement.scrollHeight - scrollElement.scrollTop - scrollElement.clientHeight < 96;
    if (nearBottom) {
      scrollElement.scrollTo({ top: scrollElement.scrollHeight, behavior: "smooth" });
      setShowNewMessages(false);
    } else {
      setShowNewMessages(true);
    }
  }, [messages]);

  const handleScroll = () => {
    const scrollElement = scrollRef.current;
    if (!scrollElement) return;
    if (scrollElement.scrollHeight - scrollElement.scrollTop - scrollElement.clientHeight < 96) {
      setShowNewMessages(false);
    }
  };

  const scrollToLatest = () => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
    setShowNewMessages(false);
  };
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
  const visibleItems = items ?? messages;

  return <aside id="conversation-panel" className="conversation-panel" aria-label="Conversation activity">
    <header className="conversation-head">
      <div><div className="conversation-brand"><Mark /><span>Conversation</span></div><p><span className={`conversation-status-dot is-${agentStatus}`} />{STATUS_COPY[agentStatus]}</p></div>
      <div className="conversation-head-actions"><button className="panel-icon" onClick={onReset} aria-label="Reset conversation" title="Reset conversation"><Icon name="reset" size={16} /></button><button className="panel-icon" onClick={onClear} aria-label="Clear conversation" title="Clear conversation"><Icon name="trash" size={16} /></button><button className="panel-icon" onClick={onClose} aria-label="Close conversation activity" title="Close conversation activity"><Icon name="close" size={17} /></button></div>
    </header>
    <div ref={scrollRef} onScroll={handleScroll} className="conversation-scroll scroll-hidden">
      {visibleItems.length === 0 ? <div className="conversation-empty"><p>No conversation yet</p></div> : <div className="conversation-list">{visibleItems.map((item, index) => item.type === "tool" ? <div key={item.id} className={`conversation-tool is-${item.status}`}><span>{item.status === "completed" ? "✓" : item.status === "failed" ? "!" : "◇"}</span><span>{item.name}</span>{item.summary && <small>{item.summary}</small>}</div> : item.type === "system" ? <div key={item.id} className="conversation-system">{item.text}</div> : <MessageRow key={item.id} message={item} variant={bubbleVariant} showLabel={!sameGroup((visibleItems[index - 1] as ConversationMessage | undefined), item)} showTimestamp={!sameGroup(item, visibleItems[index + 1] as ConversationMessage | undefined)} isGroupStart={!sameGroup(visibleItems[index - 1] as ConversationMessage | undefined, item)} isGroupEnd={!sameGroup(item, visibleItems[index + 1] as ConversationMessage | undefined)} />)}</div>}
    </div>
    {showNewMessages && <button className="conversation-new-messages" onClick={scrollToLatest}>↓ New messages</button>}
    <div className="conversation-composer"><textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={onKeyDown} placeholder="Leave a note for Lumine" rows={1} aria-label="Message Lumine" /><button className={input.trim() ? "is-ready" : ""} onClick={submit} aria-label="Send note"><Icon name="send" size={15} /></button></div>
  </aside>;
}
