import { useEffect, useRef, useState } from "react";
import type { ConversationMessage, ConversationToolEvent } from "../../pages/home/conversation/types";
import { LumineVoiceManager, type LumineVoiceStatus, type VoiceToolEvent } from "./voice-manager";
import type { LumineEmotionIntent } from "../../components/avatar/avatarTypes";
import { DEFAULT_INTERRUPTION_MODE, type InterruptionMode } from "./interruption";

export type LumineVoiceConnectionState = LumineVoiceStatus | "online" | "waiting" | "reconnecting";

export type UseLumineVoiceOptions = {
  onMessage: (message: Omit<ConversationMessage, "id"> & { id?: string }) => void;
  onUpdateMessage: (id: string, changes: Partial<Omit<ConversationMessage, "id">>) => void;
  onEmotion?: (emotion: LumineEmotionIntent) => void;
  onToolEvent?: (event: ConversationToolEvent) => void;
  interruptionMode?: InterruptionMode;
  onError: (message: string) => void;
};

export function useLumineVoice({ onMessage, onUpdateMessage, onEmotion, onToolEvent, interruptionMode = DEFAULT_INTERRUPTION_MODE, onError }: UseLumineVoiceOptions) {
  const managerRef = useRef<LumineVoiceManager | null>(null);
  const interruptionModeRef = useRef(interruptionMode);
  useEffect(() => {
    interruptionModeRef.current = interruptionMode;
  }, [interruptionMode]);
  const [snapshot, setSnapshot] = useState({
    state: "idle" as LumineVoiceStatus,
    muted: false,
    error: null as string | null,
    startedAt: null as number | null,
    isActive: false,
    sessionId: null as string | null,
    roomName: null as string | null,
  });
  const [emotion, setEmotion] = useState<LumineEmotionIntent | null>(null);

  if (!managerRef.current) {
    managerRef.current = new LumineVoiceManager(
      {
        onMessage: (message) => onMessage(message as Omit<ConversationMessage, "id"> & { id?: string }),
        onUpdateMessage: (id, changes) => onUpdateMessage(id, changes as Partial<Omit<ConversationMessage, "id">>),
        onEmotion: (nextEmotion) => { setEmotion(nextEmotion); onEmotion?.(nextEmotion); },
        onToolEvent: (event: VoiceToolEvent) => {
          const conversationEvent: ConversationToolEvent = {
            id: `${event.sessionId}:${event.id}`,
            type: "tool",
            name: event.name,
            status: event.status,
            timestamp: event.timestamp,
            sessionId: event.sessionId,
            summary: event.message,
            durationMs: event.durationMs,
          };
          onToolEvent?.(conversationEvent);
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
      setEmotion(null);
    };
  }, []);

  useEffect(() => {
    if (snapshot.state === "idle" || snapshot.state === "error") {
      setEmotion(null);
    }
  }, [snapshot.state]);

  const status: LumineVoiceConnectionState = snapshot.state === "disconnected" || snapshot.state === "idle"
    ? "idle"
    : snapshot.state === "connecting"
      ? "connecting"
      : snapshot.state === "initializing"
        ? "initializing"
        : snapshot.state === "listening"
          ? "listening"
          : snapshot.state === "speaking"
            ? "speaking"
            : snapshot.state === "disconnecting" || snapshot.state === "ending"
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
    emotion,
    startedAt: snapshot.startedAt,
    isActive: snapshot.isActive,
    muted: snapshot.muted,
    session: snapshot.sessionId ? { sessionId: snapshot.sessionId, roomName: snapshot.roomName ?? "", participantIdentity: "", agentIdentity: "Lumine" } : null,
    connect: () => managerRef.current!.start(interruptionModeRef.current),
    disconnect: () => managerRef.current!.stop(),
    start: () => managerRef.current!.start(interruptionModeRef.current),
    stop: () => managerRef.current!.stop(),
    setMuted: (muted: boolean) => managerRef.current!.setMuted(muted),
    toggleMute: () => managerRef.current!.setMuted(!snapshot.muted),
    isListening: snapshot.state === "listening",
    isSpeaking: snapshot.state === "speaking",
    isThinking: snapshot.state === "thinking",
  };
}
