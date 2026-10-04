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

  /**
   * Patch one message in place, and hand back the *same array* when nothing moved.
   *
   * Streaming transcription calls this several times a second for one row, and
   * each call used to build a fresh array by mapping over every item. That is two
   * costs the reader paid for a change to one line: the whole transcript re-rendered,
   * and every layout effect keyed on `items` re-ran — including the one that keeps
   * the view pinned to the tail.
   *
   * Returning `current` when no row actually differs gives React nothing to do.
   * The row comparison is field-by-field rather than a JSON round trip because
   * these objects hold `Date`s, and two equal timestamps are different objects.
   */
  const updateMessage = useCallback((id: string, changes: Partial<Omit<ConversationMessage, "id">>) => {
    setItems((current) => {
      let touched = false;
      const next = current.map((item) => {
        if (!isMessage(item) || item.id !== id) return item;
        // Nothing to merge is nothing to do; a no-op patch still costs a render.
        const merged = { ...item, ...changes };
        const keys = Object.keys(merged) as (keyof ConversationMessage)[];
        if (keys.every((key) => Object.is(merged[key], item[key]))) return item;
        touched = true;
        return merged;
      });
      return touched ? next : current;
    });
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

  /**
   * Mark a new call in the transcript instead of destroying the last one.
   *
   * `clearMessages()` on connect was the old behaviour, and it is the single most
   * destructive thing this app did: a voice conversation *is* the record, and
   * calling Lumine again used to throw the previous call away before the new one
   * had produced a word. So a second call was paid for with the first.
   *
   * A divider is the right replacement rather than a timestamp badge on every
   * message: the useful question is "where does one call end and the next begin",
   * and that is one fact, not a fact per row. It is a `system` item, which keeps
   * the whole thing inside the existing union and — like everything else here —
   * knows nothing about where the messages came from.
   *
   * Silent when the transcript is empty. A divider above nothing is a rule with
   * nothing under it, which is the shape of a UI element that exists only to
   * justify itself.
   */
  const beginSession = useCallback((label = "New call") => {
    setItems((current) => {
      if (current.length === 0) return current;
      const divider: ConversationItem = {
        id: `session-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        type: "system",
        text: label,
        timestamp: new Date(),
      };
      return [...current, divider];
    });
  }, []);

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
    beginSession,
    resetMessages,
    setAgentStatus,
    setError: setConversationError,
  };
}
