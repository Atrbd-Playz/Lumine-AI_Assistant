import { useEffect, useRef, useState } from "react";
import type { ConversationMessage } from "../../pages/home/conversation/types";
import { LumineVoiceManager, type LumineVoiceStatus } from "./voice-manager";

export type LumineVoiceConnectionState = LumineVoiceStatus | "online" | "waiting" | "reconnecting";

export type UseLumineVoiceOptions = {
  onMessage: (message: Omit<ConversationMessage, "id"> & { id?: string }) => void;
  onUpdateMessage: (id: string, changes: Partial<Omit<ConversationMessage, "id">>) => void;
  onError: (message: string) => void;
};

export function useLumineVoice({ onMessage, onUpdateMessage, onError }: UseLumineVoiceOptions) {
  const managerRef = useRef<LumineVoiceManager | null>(null);
  const [snapshot, setSnapshot] = useState({
    state: "idle" as LumineVoiceStatus,
    error: null as string | null,
    startedAt: null as number | null,
    isActive: false,
    sessionId: null as string | null,
    roomName: null as string | null,
  });

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

  const status: LumineVoiceConnectionState = snapshot.state === "idle"
    ? "idle"
    : snapshot.state === "connecting"
      ? "connecting"
      : snapshot.state === "initializing"
        ? "initializing"
        : snapshot.state === "listening"
          ? "listening"
          : snapshot.state === "speaking"
            ? "speaking"
            : snapshot.state === "disconnecting"
              ? "disconnecting"
              : snapshot.state === "error"
                ? "error"
                : snapshot.state === "thinking"
                  ? "thinking"
                  : "online";

  return {
    state: snapshot.state,
    status,
    error: snapshot.error,
    startedAt: snapshot.startedAt,
    isActive: snapshot.isActive,
    session: snapshot.sessionId ? { sessionId: snapshot.sessionId, roomName: snapshot.roomName ?? "", participantIdentity: "", agentIdentity: "Lumine" } : null,
    connect: () => managerRef.current!.start(),
    disconnect: () => managerRef.current!.stop(),
    start: () => managerRef.current!.start(),
    stop: () => managerRef.current!.stop(),
    getState: () => managerRef.current!.getState(),
    isConnected: snapshot.state !== "idle" && snapshot.state !== "error" && snapshot.state !== "disconnecting",
    isListening: snapshot.state === "listening",
    isSpeaking: snapshot.state === "speaking",
    isThinking: snapshot.state === "thinking",
  };
}
