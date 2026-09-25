import { useEffect, useRef, useState } from "react";
import { LumineVoiceManager, type LumineVoiceStatus, type VoiceToolEvent } from "../features/voice/voice-manager";
import { DEFAULT_INTERRUPTION_MODE, type InterruptionMode } from "../features/voice/interruption";
import type { ConversationMessage, ConversationToolEvent } from "../pages/home/conversation/types";

export type LumineConnectionState = LumineVoiceStatus | "online" | "waiting" | "reconnecting";

export type UseLumineSessionOptions = {
  onMessage: (message: Omit<ConversationMessage, "id"> & { id?: string }) => void;
  onUpdateMessage: (id: string, changes: Partial<Omit<ConversationMessage, "id">>) => void;
  onToolEvent?: (event: ConversationToolEvent) => void;
  interruptionMode?: InterruptionMode;
  onError: (message: string) => void;
};

export function useLumineSession({ onMessage, onUpdateMessage, onToolEvent, interruptionMode = DEFAULT_INTERRUPTION_MODE, onError }: UseLumineSessionOptions) {
  const managerRef = useRef<LumineVoiceManager | null>(null);
  const interruptionModeRef = useRef(interruptionMode);
  useEffect(() => {
    interruptionModeRef.current = interruptionMode;
  }, [interruptionMode]);
  const [snapshot, setSnapshot] = useState(() => ({
    state: "idle" as LumineVoiceStatus,
    error: null as string | null,
    startedAt: null as number | null,
    isActive: false,
    sessionId: null as string | null,
    roomName: null as string | null,
  }));

  if (!managerRef.current) {
    managerRef.current = new LumineVoiceManager(
      {
        onMessage: (message) => onMessage(message as Omit<ConversationMessage, "id"> & { id?: string }),
        onUpdateMessage: (id, changes) => onUpdateMessage(id, changes as Partial<Omit<ConversationMessage, "id">>),
        onEmotion: () => undefined,
        onToolEvent: (event: VoiceToolEvent) => {
          onToolEvent?.({
            id: `${event.sessionId}:${event.id}`,
            type: "tool",
            name: event.name,
            status: event.status,
            timestamp: event.timestamp,
            sessionId: event.sessionId,
            summary: event.message,
            durationMs: event.durationMs,
          });
        },
        onError,
      },
      (next) => setSnapshot(next),
    );
  }

  useEffect(() => {
    setSnapshot(managerRef.current!.getSnapshot());
    return () => {
      void managerRef.current?.stop();
    };
  }, []);

  const status: LumineConnectionState = snapshot.state === "disconnected" || snapshot.state === "idle" ? "idle" : snapshot.state === "connecting" ? "connecting" : snapshot.state === "initializing" ? "initializing" : snapshot.state === "listening" ? "listening" : snapshot.state === "speaking" ? "speaking" : snapshot.state === "disconnecting" || snapshot.state === "ending" ? "disconnecting" : snapshot.state === "error" ? "error" : snapshot.state === "thinking" ? "thinking" : "online";

  return {
    status,
    error: snapshot.error,
    startedAt: snapshot.startedAt,
    session: snapshot.sessionId ? { sessionId: snapshot.sessionId, roomName: snapshot.roomName ?? "", participantIdentity: "", agentIdentity: "Lumine" } : null,
    connect: () => managerRef.current!.start(interruptionModeRef.current),
    disconnect: () => managerRef.current!.stop(),
    isActive: snapshot.isActive,
  };
}
