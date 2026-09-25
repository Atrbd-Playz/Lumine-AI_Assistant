import { useCallback, useMemo, useState } from "react";
import { mockConversationService } from "./mockConversationService";
import type {
  ConversationAgentStatus,
  ConversationItem,
  ConversationMessage,
  ConversationMessageStatus,
  ConversationService,
  ConversationToolEvent,
} from "./types";

const createMessageId = () => `conversation-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

const isMessage = (item: ConversationItem): item is ConversationMessage => item.type !== "tool" && item.type !== "system";

type UseConversationOptions = {
  service?: ConversationService;
};

export function useConversation({ service = mockConversationService }: UseConversationOptions = {}) {
  const [items, setItems] = useState<ConversationItem[]>(() => service.getInitialMessages());
  const [agentStatus, setAgentStatus] = useState<ConversationAgentStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  const messages = useMemo(
    () => items.filter(isMessage),
    [items],
  );

  const addMessage = useCallback((message: Omit<ConversationMessage, "id"> & { id?: string }) => {
    const nextMessage: ConversationMessage = { ...message, id: message.id ?? createMessageId() };
    setItems((current) => [...current, nextMessage]);
    return nextMessage.id;
  }, []);

  const updateMessage = useCallback((id: string, changes: Partial<Omit<ConversationMessage, "id">>) => {
    setItems((current) => current.map((item) => (
      isMessage(item) && item.id === id ? { ...item, ...changes } : item
    )));
  }, []);

  const updateMessageStatus = useCallback((id: string, status: ConversationMessageStatus) => {
    updateMessage(id, { status });
  }, [updateMessage]);

  const upsertToolEvent = useCallback((event: Omit<ConversationToolEvent, "id"> & { id?: string }) => {
    const nextEvent: ConversationToolEvent = { ...event, id: event.id ?? createMessageId() };
    setItems((current) => {
      const index = current.findIndex((item) => item.type === "tool" && item.id === nextEvent.id);
      if (index < 0) {
        return [...current, nextEvent];
      }
      const next = [...current];
      const existing = next[index] as ConversationToolEvent;
      next[index] = { ...existing, ...nextEvent, timestamp: existing.timestamp } as ConversationItem;
      return next;
    });
    return nextEvent.id;
  }, []);

  const clearMessages = () => setItems([]);
  const resetMessages = () => {
    setItems(service.getInitialMessages());
    setAgentStatus("idle");
    setError(null);
  };

  const setConversationError = (message: string | null) => {
    setError(message);
    setAgentStatus(message ? "error" : "idle");
  };

  return {
    items,
    messages,
    agentStatus,
    error,
    addMessage,
    updateMessage,
    updateMessageStatus,
    upsertToolEvent,
    clearMessages,
    resetMessages,
    setAgentStatus,
    setError: setConversationError,
  };
}
