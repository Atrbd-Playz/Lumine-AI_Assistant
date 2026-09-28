import { useCallback, useEffect, useRef, useState } from "react";
import type { ConversationMessage, ConversationToolEvent } from "../../pages/home/conversation/types";
import { LumineVoiceManager, type LumineVoiceStatus, type VoiceNotice, type VoiceToolEvent } from "./voice-manager";
import type { LumineEmotionIntent } from "../../components/avatar/avatarTypes";
import { DEFAULT_INTERRUPTION_MODE, type InterruptionMode } from "./interruption";

export type LumineVoiceConnectionState = LumineVoiceStatus | "online" | "waiting" | "reconnecting";

/**
 * Every `LumineVoiceStatus`, mapped to what the UI shows.
 *
 * This is a total map rather than a ternary chain on purpose. The chain had a
 * `: "online"` fallback, and `"connected"` was never given a case, so a session
 * that reached `connected` was reported to the user as `online` -- the fallback
 * answered for a state nobody had thought about. A `Record` keyed by the source
 * union makes that a compile error instead: adding a state to `LumineVoiceStatus`
 * now fails the build until it is mapped here.
 */
const CONNECTION_STATE_BY_VOICE_STATUS: Record<LumineVoiceStatus, LumineVoiceConnectionState> = {
  disconnected: "idle",
  idle: "idle",
  connecting: "connecting",
  // The room is joined and the agent is present but has not spoken yet. `online`
  // is the honest word; it is what the label and the active styling both mean.
  connected: "online",
  initializing: "initializing",
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
  disconnecting: "disconnecting",
  ending: "disconnecting",
  error: "error",
};

export type VoiceToolResult = {
  name: string;
  status: "completed" | "failed";
  /**
   * The tool's actual output. Goes to a toast, never into the transcript.
   *
   * Empty for a failure that produced none, which is why this is delivered for
   * every finished call rather than only the ones that carry data.
   */
  payload: string;
  durationMs?: number;
  sessionId: string;
};

export type UseLumineVoiceOptions = {
  onMessage: (message: Omit<ConversationMessage, "id"> & { id?: string }) => void;
  onUpdateMessage: (id: string, changes: Partial<Omit<ConversationMessage, "id">>) => void;
  onEmotion?: (emotion: LumineEmotionIntent) => void;
  onToolEvent?: (event: ConversationToolEvent) => void;
  /**
   * A tool's output, delivered separately from `onToolEvent`.
   *
   * Two callbacks rather than one wider event because the two go to different
   * places: the transcript accumulates and the toast does not, and the payload is
   * the model's input rather than something anyone said. Keeping `payload` off
   * `ConversationToolEvent` leaves no path by which it reaches the transcript by
   * accident.
   */
  onToolResult?: (result: VoiceToolResult) => void;
  /** A rate limit or failure the user should be shown, not just logged. */
  onNotice?: (notice: VoiceNotice) => void;
  interruptionMode?: InterruptionMode;
  onError: (message: string) => void;
};

export function useLumineVoice({ onMessage, onUpdateMessage, onEmotion, onToolEvent, onToolResult, onNotice, interruptionMode = DEFAULT_INTERRUPTION_MODE, onError }: UseLumineVoiceOptions) {
  const managerRef = useRef<LumineVoiceManager | null>(null);
  const interruptionModeRef = useRef(interruptionMode);
  const onToolResultRef = useRef(onToolResult);
  useEffect(() => {
    onToolResultRef.current = onToolResult;
  }, [onToolResult]);
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
          if (event.status !== "started") {
            // Fires for every finished call, payload or not.
            //
            // It used to fire only when a payload was present, which left a failed
            // tool with no payload reporting itself nowhere. The fix is here rather
            // than at the call site so that exactly one signal per tool call exists
            // to report: two signals is what produced two toasts for one action.
            onToolResultRef.current?.({
              name: event.name,
              status: event.status,
              payload: event.payload ?? "",
              durationMs: event.durationMs,
              sessionId: event.sessionId,
            });
          }
        },
        // Optional at the call site but required by the manager, so a caller that
        // does not care about notices is not forced to write a no-op.
        onNotice: (notice) => onNotice?.(notice),
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

  const status: LumineVoiceConnectionState = CONNECTION_STATE_BY_VOICE_STATUS[snapshot.state];

  /**
   * The room's video controls, memoised rather than inlined.
   *
   * These are dependencies of an effect in `Home.tsx` that reconciles what is
   * published against what is actually captured. An inline arrow is a new
   * function every render, so that effect would run on every render too, and a
   * teardown guard that re-evaluates constantly is one keystroke away from
   * publishing something the user turned off.
   */
  const publishVideo = useCallback(
    (source: "camera" | "screen", stream: MediaStream) => managerRef.current!.publishVideo(source, stream),
    [],
  );
  const unpublishVideo = useCallback(() => managerRef.current!.unpublishVideo(), []);

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
    /**
     * Video goes to the room, not just to the preview.
     *
     * The two halves are separate calls on purpose: `useLocalMedia` owns the
     * capture and its teardown, this owns the publication, and the caller
     * decides whether a model that cannot read frames should be sent any. A
     * single combined call would have to trust that decision, and LiveKit's
     * answer to being sent one it cannot use is silence.
     */
    publishVideo,
    unpublishVideo,
    isListening: snapshot.state === "listening",
    isSpeaking: snapshot.state === "speaking",
    isThinking: snapshot.state === "thinking",
  };
}
