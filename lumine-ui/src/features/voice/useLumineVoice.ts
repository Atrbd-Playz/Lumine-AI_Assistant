import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ConversationMessage, ConversationToolEvent } from "../../pages/home/conversation/types";
import {
  LumineVoiceManager,
  type LumineVoiceStatus,
  type VoiceBrowserPreview,
  type VoiceScreenState,
  type VoiceNotice,
  type VoicePermissionRequest,
  type VoiceToolEvent,
} from "./voice-manager";
import type { LumineEmotionIntent } from "../../components/avatar/avatarTypes";
import { DEFAULT_INTERRUPTION_MODE, type InterruptionMode } from "./interruption";

/**
 * A worker's record, narrowed to the three statuses this surface speaks.
 *
 * The worker sends progress steps — "opening WhatsApp", "draft is ready" — which
 * reuse `started` and `failed` and close with a `done` of their own. Everything
 * downstream of here knows only `started | completed | failed`, so the mapping
 * lives in one named function rather than in a ternary at each of two call
 * sites: two ternaries is how they come to disagree.
 *
 * A step's `done` becomes `completed` rather than being passed through, because
 * the transcript shows this as "this tool call finished", and a fourth status
 * there would render as neither.
 */
function toCallStatus(event: VoiceToolEvent): "started" | "completed" | "failed" {
  if (event.status === "failed") return "failed";
  if (event.status === "started") return "started";
  return "completed";
}

/**
 * The same narrowing for the surfaces that only ever describe a *finished* call.
 *
 * Separate rather than reusing `toCallStatus` so the return type excludes
 * `started`: a toast saying "started" is a toast about something that has not
 * happened yet, and the type is what stops that being written by accident.
 */
function toFinishedStatus(event: VoiceToolEvent): "completed" | "failed" {
  return event.status === "failed" ? "failed" : "completed";
}

export type LumineVoiceConnectionState = LumineVoiceStatus | "online" | "waiting" | "reconnecting";

/**
 * The statuses in which a typed message can actually arrive.
 *
 * Derived from the same union the connection map consumes, so it cannot name a
 * status that does not exist. Note that `connecting` is absent: the room exists
 * before the agent has joined, and text sent into a room with nobody in it is
 * accepted by the data layer and read by no one — the message would appear to
 * have been sent and would simply never be answered.
 */
const TEXT_SENDABLE_STATES: ReadonlySet<LumineVoiceConnectionState> = new Set<LumineVoiceConnectionState>([
  "connected",
  "online",
  "listening",
  "thinking",
  "speaking",
  "waiting",
]);

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
  /**
   * An approval the worker is blocked on. Answer with `respondPermission`.
   *
   * Held through a ref rather than captured by the manager's constructor,
   * because the manager is built once and a permission that reaches a stale
   * closure never renders: the card would simply not appear, and the worker
   * would refuse after its deadline. The user would see a tool that "did
   * nothing" with no question ever asked.
   */
  onPermission?: (request: VoicePermissionRequest) => void;
  /**
   * A screenshot of the page the browser tools are driving.
   *
   * Deliberately its own callback and *not* part of `onToolEvent`: this is a
   * picture, so it belongs on a panel and not in the transcript, which
   * accumulates and would otherwise hold a base64 JPEG for the life of the
   * conversation. Held through a ref for the same reason `onPermission` is — the
   * manager is built once, so a first-render closure would deliver the next
   * preview to a component that has since unmounted.
   */
  onBrowserPreview?: (preview: VoiceBrowserPreview) => void;
  /**
   * The window `desktop_control` is working in.
   *
   * Optional because a session that never touches the desktop never publishes
   * one, and every caller would otherwise have to implement a screen it will
   * never see.
   */
  onScreenState?: (state: VoiceScreenState) => void;
  interruptionMode?: InterruptionMode;
  onError: (message: string) => void;
};

export function useLumineVoice({ onMessage, onUpdateMessage, onEmotion, onToolEvent, onToolResult, onNotice, onPermission, onBrowserPreview, onScreenState, interruptionMode = DEFAULT_INTERRUPTION_MODE, onError }: UseLumineVoiceOptions) {
  const managerRef = useRef<LumineVoiceManager | null>(null);
  const interruptionModeRef = useRef(interruptionMode);
  const onToolResultRef = useRef(onToolResult);
  const onPermissionRef = useRef(onPermission);
  const onBrowserPreviewRef = useRef(onBrowserPreview);
  const onScreenStateRef = useRef(onScreenState);
  useEffect(() => {
    onToolResultRef.current = onToolResult;
  }, [onToolResult]);
  useEffect(() => {
    onPermissionRef.current = onPermission;
  }, [onPermission]);
  useEffect(() => {
    onBrowserPreviewRef.current = onBrowserPreview;
  }, [onBrowserPreview]);
  useEffect(() => {
    onScreenStateRef.current = onScreenState;
  }, [onScreenState]);
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
          // A progress step reuses `started`/`failed` and closes with its own
          // `done`, so it is mapped onto the three statuses this surface knows
          // before it goes anywhere. Steps reach the activity timeline — which
          // renders them as an ordered list — and deliberately stop there.
          const conversationEvent: ConversationToolEvent = {
            id: `${event.sessionId}:${event.id}`,
            type: "tool",
            name: event.name,
            status: toCallStatus(event),
            timestamp: event.timestamp,
            sessionId: event.sessionId,
            summary: event.message,
            durationMs: event.durationMs,
          };
          onToolEvent?.(conversationEvent);
          // Only a whole call raises a toast. A six-step send would raise six,
          // which is the "two signals for one action" problem below at six times
          // the scale — and the payload belongs to the call, not to a step.
          if (!event.action && event.status !== "started") {
            // Fires for every finished call, payload or not.
            //
            // It used to fire only when a payload was present, which left a failed
            // tool with no payload reporting itself nowhere. The fix is here rather
            // than at the call site so that exactly one signal per tool call exists
            // to report: two signals is what produced two toasts for one action.
            onToolResultRef.current?.({
              name: event.name,
              status: toFinishedStatus(event),
              payload: event.payload ?? "",
              durationMs: event.durationMs,
              sessionId: event.sessionId,
            });
          }
        },
        // Optional at the call site but required by the manager, so a caller that
        // does not care about notices is not forced to write a no-op.
        onNotice: (notice) => onNotice?.(notice),
        // Read from the ref on every delivery: the manager is constructed once,
        // and a request routed through a first-render closure would be shown to
        // whoever the component was when the call began.
        onPermission: (request) => onPermissionRef.current?.(request),
        onBrowserPreview: (preview) => onBrowserPreviewRef.current?.(preview),
        onScreenState: (state) => onScreenStateRef.current?.(state),
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

  /**
   * Lumine's loudness, 0..1, pushed rather than polled.
   *
   * A callback and not a number, and the reason is the render cost: an RMS
   * updates about 60 times a second, so exposing it as state would re-render
   * everything that read it 60 times a second. Handing out a subscription lets
   * the consumer store the value in a ref and write it straight to whatever is
   * drawing.
   *
   * Stable by identity, so it is safe as an effect dependency — an inline arrow
   * here would tear down and re-establish the avatar's subscription on every
   * single render, which is the exact cost the subscription was introduced to
   * avoid.
   */
  const subscribeAudioLevel = useCallback(
    (onLevel: (level: number) => void) => managerRef.current!.subscribeAudioLevel(onLevel),
    [],
  );

  /**
   * Say something to Lumine in text. Resolves to whether it was delivered.
   *
   * A boolean rather than a throw, because the caller is a composer and a
   * rejected promise inside a submit handler is an unhandled rejection. `false`
   * means "keep your draft"; the manager has already reported the reason.
   *
   * Stable by identity so it can be a dependency of a memo without churning.
   */
  const sendText = useCallback((text: string) => managerRef.current!.sendText(text), []);

  /**
   * Answer a pending approval. Resolves to whether the answer was delivered.
   *
   * `false` means the card should stay up: the packet did not leave, and the
   * user can try again while the worker's deadline still has time on it.
   * `true` means it was sent -- the worker owns whether it still counted (an
   * expired or already-answered id is a no-op there, audited as such).
   */
  const respondPermission = useCallback(
    (requestId: string, decision: "approve" | "deny") => managerRef.current!.sendPermission(requestId, decision),
    [],
  );

  /**
   * Whether a typed message can reach Lumine.
   *
   * Derived from the snapshot rather than held as its own state, because it is
   * the same fact as the connection and a second copy would be wrong for exactly
   * as long as a call ended between the render and the click.
   *
   * Mute is included deliberately. It looks unrelated — muting is about the
   * microphone — but on this app a muted call has the microphone disabled, so
   * there is no *speakable* channel left, and typing during a muted call is
   * someone who has forgotten they muted.
   */
  const canSendText = useMemo(
    () => TEXT_SENDABLE_STATES.has(snapshot.state) && !snapshot.muted,
    [snapshot.state, snapshot.muted],
  );

  return {
    state: snapshot.state,
    status,
    error: snapshot.error,
    emotion,
    startedAt: snapshot.startedAt,
    isActive: snapshot.isActive,
    muted: snapshot.muted,
    subscribeAudioLevel,
    sendText,
    respondPermission,
    canSendText,
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
