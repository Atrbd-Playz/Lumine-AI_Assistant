import { useEffect, useRef, useState } from "react";
import { LumineVoiceManager, type LumineVoiceStatus } from "../features/voice/voice-manager";
import type { ConversationMessage } from "../pages/home/conversation/types";

export type LumineConnectionState = LumineVoiceStatus | "online" | "waiting" | "reconnecting";

export type UseLumineSessionOptions = {
  onMessage: (message: Omit<ConversationMessage, "id"> & { id?: string }) => void;
  onUpdateMessage: (id: string, changes: Partial<Omit<ConversationMessage, "id">>) => void;
  onError: (message: string) => void;
};

export function useLumineSession({ onMessage, onUpdateMessage, onError }: UseLumineSessionOptions) {
  const managerRef = useRef<LumineVoiceManager | null>(null);
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

  const status: LumineConnectionState = snapshot.state === "idle" ? "idle" : snapshot.state === "connecting" ? "connecting" : snapshot.state === "initializing" ? "initializing" : snapshot.state === "listening" ? "listening" : snapshot.state === "speaking" ? "speaking" : snapshot.state === "disconnecting" ? "disconnecting" : snapshot.state === "error" ? "error" : snapshot.state === "thinking" ? "thinking" : "online";

  return {
    status,
    error: snapshot.error,
    startedAt: snapshot.startedAt,
    session: snapshot.sessionId ? { sessionId: snapshot.sessionId, roomName: snapshot.roomName ?? "", participantIdentity: "", agentIdentity: "Lumine" } : null,
    connect: () => managerRef.current!.start(),
    disconnect: () => managerRef.current!.stop(),
    isActive: snapshot.isActive,
  };
}
