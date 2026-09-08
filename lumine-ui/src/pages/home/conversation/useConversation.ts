import { useState } from "react";
import { mockConversationService } from "./mockConversationService";
import type {
  ConversationAgentStatus,
  ConversationMessage,
  ConversationMessageStatus,
  ConversationService,
} from "./types";

const createMessageId = () => `conversation-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

type UseConversationOptions = {
  service?: ConversationService;
};

export function useConversation({ service = mockConversationService }: UseConversationOptions = {}) {
  const [messages, setMessages] = useState<ConversationMessage[]>(() => service.getInitialMessages());
  const [agentStatus, setAgentStatus] = useState<ConversationAgentStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  const addMessage = (message: Omit<ConversationMessage, "id"> & { id?: string }) => {
    const nextMessage = { ...message, id: message.id ?? createMessageId() };
    setMessages((current) => [...current, nextMessage]);
    return nextMessage.id;
  };

  const updateMessage = (id: string, changes: Partial<Omit<ConversationMessage, "id">>) => {
    setMessages((current) => current.map((message) => message.id === id ? { ...message, ...changes } : message));
  };

  const updateMessageStatus = (id: string, status: ConversationMessageStatus) => {
    updateMessage(id, { status });
  };

  const clearMessages = () => setMessages([]);
  const resetMessages = () => {
    setMessages(service.getInitialMessages());
    setAgentStatus("idle");
    setError(null);
  };

  const setConversationError = (message: string | null) => {
    setError(message);
    setAgentStatus(message ? "error" : "idle");
  };

  return {
    messages,
    agentStatus,
    error,
    addMessage,
    updateMessage,
    updateMessageStatus,
    clearMessages,
    resetMessages,
    setAgentStatus,
    setError: setConversationError,
  };
}
