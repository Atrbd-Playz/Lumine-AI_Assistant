import { LocalVideoTrack, ParticipantKind, Room, RoomEvent, Track, type Participant, type RemoteParticipant, type RemoteTrack, type RemoteTrackPublication, type TranscriptionSegment } from "livekit-client";
import { invoke } from "@tauri-apps/api/core";
import { LIVEKIT_URL, getLiveKitToken } from "../../lib/livekit";
import { resolveEmotionIntent } from "../emotion/emotion-controller";
import type { LumineEmotionIntent } from "../../components/avatar/avatarTypes";
import { interruptionMetadata, type InterruptionMode } from "./interruption";
import { AudioLevelMonitor } from "./audioLevel";

export type LumineVoiceStatus =
  | "disconnected"
  | "connecting"
  | "connected"
  | "ending"
  | "error"
  | "idle"
  | "initializing"
  | "listening"
  | "thinking"
  | "speaking"
  | "disconnecting";

export type VoiceMessage = {
  role: "user" | "lumine";
  content: string;
  timestamp: Date;
  sessionId?: string;
  type: "voice";
  status: "complete" | "processing";
};

/**
 * Something the user needs to be told about, from the agent's own runtime.
 *
 * Distinct from `VoiceToolEvent` because the reaction differs: a tool finishing
 * is a status line, while a rate limit is a warning the user has to act on. The
 * `kind` is the whole point -- `limit` means a wait, and everything else means
 * something went wrong.
 */
export type VoiceNotice = {
  kind: "limit" | "agent_failure";
  /** Written for a person, not a log. */
  message: string;
  /** Named failure, so the UI can be specific without parsing prose. */
  failureKind: string;
  retryable: boolean;
  retryAfter?: number;
  sessionId: string;
};

export type VoiceToolEvent = {
  id: string;
  name: string;
  status: "started" | "completed" | "failed";
  /** A short status for the transcript. Never the tool's payload. */
  message: string;
  /**
   * The tool's actual output, for a transient toast.
   *
   * Separate from `message` on purpose: the transcript accumulates and the toast
   * does not, and the payload is the model's input rather than something said
   * out loud. This is the one surface where showing it costs nothing and helps.
   */
  payload?: string;
  timestamp: Date;
  sessionId: string;
  durationMs?: number;
};

export type VoiceManagerSnapshot = {
  state: LumineVoiceStatus;
  muted: boolean;
  error: string | null;
  startedAt: number | null;
  isActive: boolean;
  sessionId: string | null;
  roomName: string | null;
};

type SessionContext = {
  id: string;
  generation: number;
  roomName: string;
  identity: string;
  room: Room;
  remoteAudioElements: Set<HTMLAudioElement>;
  audioElementsByTrack: Map<string, HTMLAudioElement>;
  attachedTrackSids: Set<string>;
  transcriptMessageIds: Set<string>;
  agentTimer?: number;
  agentConnected: boolean;
  muted: boolean;
  /**
   * The video track this session published, and which source it came from.
   *
   * Held rather than re-derived because LiveKit uses only the most recently
   * published video track: unpublishing the wrong one leaves the previous
   * source live in the room, and the self-view card would then be showing a
   * track the model cannot see. One published track, tracked by name.
   */
  publishedVideo?: LocalVideoTrack;
  publishedVideoSource?: "camera" | "screen";
  /**
   * Lumine's loudness, for the presence layer.
   *
   * Manager-scoped, not per session. See `subscribeAudioLevel` for why, and
   * `AudioLevelMonitor.attach` for how one context is re-pointed across calls
   * rather than accumulated.
   */
  disposed: boolean;
};

const AGENT_IDENTITY = "Lumine";

/**
 * The room topic a typed message must be sent on to become a turn.
 *
 * `livekit-client` does not export this, so it is declared here rather than
 * imported, and it is the one string in this module that can rot silently. It
 * mirrors `livekit.agents.types.TOPIC_CHAT`, which `RoomIO.__init__` registers
 * unconditionally:
 *
 *     self._room.register_text_stream_handler(TOPIC_CHAT, self._on_chat_text_stream)
 *
 * so a message on this topic arrives as a `TextInputEvent` and the model answers
 * it out loud, through the same pipeline, persona and tools as a spoken turn.
 * Sending on any other topic is not an error anywhere — it simply never reaches
 * her, which reads as a broken app rather than as a wrong string.
 *
 * To re-verify after a `livekit-agents` upgrade:
 *   grep -n "TOPIC_CHAT" .venv/Lib/site-packages/livekit/agents/types.py
 * A mismatch here is a one-line fix. A mismatch found by a user typing into a
 * composer is a bug report.
 */
const TOPIC_CHAT = "lk.chat";
const TOKEN_TIMEOUT_MS = 10_000;
const CONNECT_TIMEOUT_MS = 15_000;
const DISPATCH_TIMEOUT_MS = 10_000;
const AGENT_TIMEOUT_MS = 15_000;
/**
 * Past this, a held operation lock is treated as stuck rather than in-flight.
 *
 * Comfortably longer than the longest thing a connect waits for: a 20s worker
 * spawn, a 30s readiness wait, and a 15s agent-join timeout, in sequence.
 */
const OPERATION_LOCK_TIMEOUT_MS = 90_000;
/** Spawning Python and importing the LiveKit SDK is the slow part of a cold start. */
const WORKER_START_TIMEOUT_MS = 20_000;
/** Longer than the spawn: readiness is the worker reporting it registered. */
const WORKER_READY_TIMEOUT_MS = 30_000;
const EMOTION_DEBUG = import.meta.env.VITE_LUMINE_DEBUG_EMOTION === "true";

type ToolStatusPayload = {
  type?: string;
  id?: string;
  name?: string;
  tool?: string;
  status?: string;
  message?: string;
  summary?: string;
  /** The payload, kept separate from `summary` so the transcript never shows it. */
  result?: string;
  duration_ms?: number;
  timestamp?: number;
};

type NoticePayload = {
  type?: string;
  kind?: string;
  message?: string;
  failureKind?: string;
  retryable?: boolean;
  retryAfter?: number;
};

const NOTICE_KINDS = new Set(["limit", "agent_failure"]);

function parseNotice(payload: unknown, sessionId: string): VoiceNotice | null {
  if (!payload || typeof payload !== "object") return null;
  const event = payload as NoticePayload;
  // `type` and `kind` are both accepted because the record is emitted once and
  // forwarded; being strict about which name is "correct" would only make a
  // notice silently vanish on a rename.
  const kind = (event.kind ?? event.type ?? "").trim();
  if (!NOTICE_KINDS.has(kind)) return null;
  const message = (event.message ?? "").trim();
  if (!message) return null;
  return {
    kind: kind as VoiceNotice["kind"],
    message,
    failureKind: (event.failureKind ?? "").trim() || "unknown",
    retryable: event.retryable !== false,
    retryAfter: typeof event.retryAfter === "number" ? event.retryAfter : undefined,
    sessionId,
  };
}

function parseToolStatus(payload: unknown, sessionId: string): VoiceToolEvent | null {
  if (!payload || typeof payload !== "object") return null;
  const event = payload as ToolStatusPayload;
  if (event.type !== "tool_status") return null;
  const name = (event.name || event.tool || "tool").trim();
  const status = event.status;
  if (!name || !event.id || (status !== "started" && status !== "completed" && status !== "failed")) {
    return null;
  }
  return {
    id: event.id,
    name,
    status,
    // `summary` first, and never `result`. A transcript records what was *said*,
    // and a tool payload is not something anyone said -- it is the model's input,
    // compressed and (for web data) wrapped in an injection guard. Showing it
    // inline would read as though the model had been handed it by the user.
    message: (event.summary || event.message || "").trim(),
    payload: typeof event.result === "string" && event.result.trim() ? event.result : undefined,
    timestamp: typeof event.timestamp === "number" ? new Date(event.timestamp * 1000) : new Date(),
    sessionId,
    durationMs: typeof event.duration_ms === "number" ? event.duration_ms : undefined,
  };
}

async function withTimeout<T>(operation: Promise<T>, timeoutMs: number, message: string) {
  let timer: number | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<T>((_, reject) => {
        timer = window.setTimeout(() => reject(new Error(message)), timeoutMs);
      }),
    ]);
  } finally {
    if (timer !== undefined) window.clearTimeout(timer);
  }
}

function isAgent(participant: Participant) {
  return (
    participant.kind === ParticipantKind.AGENT ||
    participant.identity.toLowerCase() === AGENT_IDENTITY.toLowerCase() ||
    participant.name?.toLowerCase() === AGENT_IDENTITY.toLowerCase() ||
    participant.metadata?.includes('"role":"agent"') === true
  );
}

function createRoomName() {
  return `lumine-${crypto.randomUUID().replace(/-/g, "")}`;
}

async function disconnectRoom(room: Room) {
  if (room.state === "disconnected") return;
  await new Promise<void>((resolve) => {
    const finish = () => {
      room.off(RoomEvent.Disconnected, finish);
      resolve();
    };
    room.on(RoomEvent.Disconnected, finish);
    room.disconnect();
    if (room.state === "disconnected") finish();
  });
}

export type VoiceLifecycleCallbacks = {
  onMessage: (message: Record<string, unknown> & { id?: string }) => void;
  onUpdateMessage: (id: string, changes: Partial<Record<string, unknown>>) => void;
  onEmotion: (emotion: LumineEmotionIntent) => void;
  onToolEvent: (event: VoiceToolEvent) => void;
  onNotice: (notice: VoiceNotice) => void;
  onError: (message: string) => void;
};

export class LumineVoiceManager {
  private state: LumineVoiceStatus = "disconnected";
  private muted = false;
  private error: string | null = null;
  private startedAt: number | null = null;
  private activeSession: SessionContext | null = null;
  private generation = 0;
  private operationLock = false;
  /** When the lock was taken, so a lock that outlives its connect can be spotted. */
  private lockHeldAt: number | null = null;
  private readonly onStateChange: (snapshot: VoiceManagerSnapshot) => void;
  private readonly callbacks: VoiceLifecycleCallbacks;
  /**
   * One audio analyser for the manager's whole life, re-pointed per call.
   *
   * Deliberately not per session: see `subscribeAudioLevel`. A field rather than a
   * lazy getter because the cost of constructing it is a `Set` and the cost of
   * constructing an `AudioContext` per call is a leak nobody sees until the
   * browser starts refusing to make more.
   */
  private readonly audioLevel = new AudioLevelMonitor();

  constructor(callbacks: VoiceLifecycleCallbacks, onStateChange: (snapshot: VoiceManagerSnapshot) => void) {
    this.callbacks = callbacks;
    this.onStateChange = onStateChange;
  }

  getSnapshot(): VoiceManagerSnapshot {
    return {
      state: this.state,
      muted: this.muted,
      error: this.error,
      startedAt: this.startedAt,
      isActive: this.state !== "idle" && this.state !== "error",
      sessionId: this.activeSession?.id ?? null,
      roomName: this.activeSession?.roomName ?? null,
    };
  }

  /**
   * Subscribe to Lumine's loudness. Returns an unsubscribe.
   *
   * Manager-scoped rather than session-scoped, and that is the whole reason the
   * monitor lives on the manager. A per-session monitor means a subscription
   * taken by a component that outlives a call is handed a monitor that
   * `close()`d at the end of it — the avatar would go permanently still for the
   * rest of the app's life, with no error anywhere to explain it.
   *
   * One monitor also means one `AudioContext` for the whole process rather than
   * one per call. `attach` releases and rebuilds the graph, so a reconnection
   * re-points it, and nothing accumulates.
   */
  subscribeAudioLevel(onLevel: (level: number) => void): () => void {
    return this.audioLevel.subscribe(onLevel);
  }

  private publish() {
    this.onStateChange(this.getSnapshot());
  }

  private setState(nextState: LumineVoiceStatus, nextError?: string | null) {
    this.state = nextState;
    this.error = nextError ?? this.error;
    this.publish();
  }

  private isCurrentSession(session: SessionContext) {
    return this.activeSession === session && !session.disposed && session.generation === this.generation;
  }

  private async cleanupSession(session: SessionContext, reason?: string) {
    if (session.disposed) return;
    session.disposed = true;

    console.info("[Voice] Ending session", {
      sessionId: session.id,
      room: session.roomName,
      reason: reason ?? "cleanup",
    });

    if (session.agentTimer !== undefined) {
      window.clearTimeout(session.agentTimer);
    }

    // Detached, not closed, and before the room teardown so the graph is released
    // while the track that fed it is still resolvable.
    //
    // `detach` and not `close` because the monitor is manager-scoped and its
    // subscribers outlive this call. Closing here would drop the avatar's
    // subscription along with the context, and the next call would leave a face
    // that is permanently still — with nothing in the log to say why.
    this.audioLevel.detach();

    try {
      session.room.removeAllListeners();
      await session.room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
      // Before the publications sweep below, and against *this* session: the
      // sweep only stops audio, so a published video track would otherwise
      // survive teardown with its camera still lit.
      await this.unpublishFrom(session).catch(() => undefined);
      session.room.localParticipant.trackPublications.forEach((publication) => {
        if (publication.kind === Track.Kind.Audio) {
          publication.track?.stop();
        }
      });

      session.remoteAudioElements.forEach((element) => {
        element.pause();
        element.srcObject = null;
        element.remove();
      });
      session.remoteAudioElements.clear();
      session.audioElementsByTrack.clear();
      session.attachedTrackSids.clear();
      await disconnectRoom(session.room);
    } catch (error) {
      console.warn("[Voice] Cleanup warning", error);
    }

    try {
      await invoke("delete_livekit_room", { room: session.roomName }).catch((cause) => {
        console.warn("[Voice] Room deletion skipped", cause);
      });
    } catch (error) {
      console.warn("[Voice] Room removal failed", error);
    }

    const shouldResetUi = this.activeSession === session;
    if (shouldResetUi) {
      this.activeSession = null;
      this.startedAt = null;
    }

    if (shouldResetUi || this.generation === session.generation) {
      this.state = "disconnected";
      this.muted = false;
      this.error = null;
      this.publish();
    }
    console.info("[Voice] Cleanup complete", { sessionId: session.id, room: session.roomName });
  }

  /**
   * Make sure a Python worker is running before a room is dispatched.
   *
   * Readiness is a separate step from starting: the process has to finish
   * importing the LiveKit SDK and register with the server before a dispatch
   * finds it, and dispatching into a room nobody is listening for produces a room
   * that sits silently connected. So the wait is explicit, and bounded, rather
   * than hoping the spawn has settled by the time the room exists.
   *
   * Failure is not fatal. A worker already started outside Tauri is invisible to
   * Rust and can serve the room fine, so a timeout is reported and then tolerated
   * -- the agent-join timeout further down is the real backstop, and its error
   * message is the one the user will actually need.
   */
  /**
   * Whether the operation lock has outlived any plausible connect.
   *
   * The lock is normally released in a `finally`, so this should never be true.
   * It exists because "the mic button does nothing and only a restart fixes it"
   * is indistinguishable from a hung await, and a stuck lock is cheap to detect
   * and cheap to clear. Generous on purpose: exceeding it means something is
   * genuinely wrong, not that a slow connect was interrupted.
   */
  private lockIsStuck(): boolean {
    return this.lockHeldAt !== null && performance.now() - this.lockHeldAt > OPERATION_LOCK_TIMEOUT_MS;
  }

  private async ensureWorker(sessionId: string, markLatency: (stage: string, details?: Record<string, unknown>) => void): Promise<void> {
    // Refuse before spawning anything.
    //
    // This is the backstop, not the primary gate: the voice button is already
    // disabled while setup is incomplete. It is here because the button is not the
    // only way in — a stale render, a second window, or a keyring entry deleted
    // between the check and the press would all reach this point. A missing
    // `CARTESIA_API_KEY` otherwise becomes an unhandled 401 inside a session the
    // user has already started talking into, which is indistinguishable from a
    // muted microphone.
    const setup = await invoke<{ ready: boolean; blocking: string[]; required: { id: string; local: boolean; stored: boolean; inEnv: boolean }[] }>("get_setup_status").catch(() => null);
    if (setup && !setup.ready) {
      const missing = setup.required
        .filter((item) => !item.local && !item.stored && !item.inEnv)
        .map((item) => item.id);
      throw new Error(
        setup.blocking.length > 0
          ? `Lumine's configuration is not valid: ${setup.blocking[0]}`
          : missing.length > 0
            ? `Lumine needs ${missing.join(" and ")} before it can listen. Add the key in Settings, then try again.`
            : "Lumine is not configured yet. Open Settings to finish setup.",
      );
    }

    try {
      await withTimeout(invoke("start_agent"), WORKER_START_TIMEOUT_MS, "Couldn't start Lumine's voice worker.");
      markLatency("worker_started", { sessionId });
    } catch (error) {
      console.warn("[Voice] start_agent failed; a worker may already be running elsewhere", {
        sessionId,
        error: error instanceof Error ? error.message : String(error),
      });
      return;
    }

    try {
      await withTimeout(invoke("wait_for_agent_worker"), WORKER_READY_TIMEOUT_MS, "Lumine's voice worker did not become ready.");
      markLatency("worker_ready", { sessionId });
    } catch (error) {
      console.warn("[Voice] Worker readiness timed out; continuing to dispatch", {
        sessionId,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  async start(interruptionMode: InterruptionMode = "barge_in"): Promise<void> {
    if (this.operationLock && this.activeSession) {
      // A second click while the first connect is still in flight. Legitimate, and
      // the right thing to ignore -- but silently, because a stuck lock is
      // indistinguishable from a dead button and the only cure a user finds is
      // restarting the app.
      if (!this.lockIsStuck()) {
        return;
      }
      console.warn("[Voice] Releasing a stuck operation lock; the previous connect never finished");
      this.operationLock = false;
      this.lockHeldAt = null;
    }

    if (this.activeSession) {
      await this.stop();
    }

    this.operationLock = true;
    this.lockHeldAt = performance.now();
    const sessionId = crypto.randomUUID();
    const roomName = createRoomName();
    const identity = `user-${sessionId.slice(0, 12)}`;
    const latencyStartedAt = performance.now();
    const markLatency = (stage: string, details: Record<string, unknown> = {}) => {
      console.info("[Voice][Latency]", {
        sessionId,
        stage,
        elapsedMs: Math.round(performance.now() - latencyStartedAt),
        ...details,
      });
    };

    const session: SessionContext = {
      id: sessionId,
      generation: Date.now(),
      roomName,
      identity,
      room: new Room({ adaptiveStream: true, dynacast: true }),
      remoteAudioElements: new Set(),
      audioElementsByTrack: new Map(),
      attachedTrackSids: new Set(),
      transcriptMessageIds: new Set(),
      agentConnected: false,
      muted: false,
      disposed: false,
    };

    const generation = ++this.generation;
    session.generation = generation;
    this.activeSession = session;
    this.startedAt = Date.now();
    this.setState("connecting");

    const ownsSession = () => this.isCurrentSession(session);

    const attachAudio = (track: RemoteTrack, publication: RemoteTrackPublication, participant: RemoteParticipant) => {
      const existing = session.audioElementsByTrack.get(publication.trackSid);
      if (track.kind !== Track.Kind.Audio || !isAgent(participant) || !ownsSession() || (existing && !existing.ended)) {
        return;
      }
      if (existing) {
        session.remoteAudioElements.delete(existing);
        existing.remove();
      }

      session.attachedTrackSids.add(publication.trackSid);
      const element = track.attach();
      element.autoplay = true;
      session.remoteAudioElements.add(element);
      session.audioElementsByTrack.set(publication.trackSid, element);
      console.info("[Voice] Audio attached", { sessionId, trackSid: publication.trackSid });
      markLatency("agent_audio_attached", { trackSid: publication.trackSid });
      this.setState("speaking");

      const logMediaEvent = (eventName: string) => {
        console.info("[Voice][Audio]", { sessionId, trackSid: publication.trackSid, event: eventName });
      };
      element.addEventListener("playing", () => logMediaEvent("playing"));
      element.addEventListener("stalled", () => logMediaEvent("stalled"));
      element.addEventListener("waiting", () => logMediaEvent("waiting"));
      element.addEventListener("error", () => logMediaEvent("error"));
      element.addEventListener(
        "ended",
        () => {
          logMediaEvent("ended");
          session.remoteAudioElements.delete(element);
          if (session.audioElementsByTrack.get(publication.trackSid) === element) {
            session.audioElementsByTrack.delete(publication.trackSid);
            session.attachedTrackSids.delete(publication.trackSid);
          }
          element.remove();
          // Stop metering only once the *last* audio element is gone. Detaching on
          // the first `ended` would tear the monitor down out from under a
          // replacement track that had already attached, leaving the avatar
          // permanently silent for a session that is working fine.
          if (session.remoteAudioElements.size === 0) {
            this.audioLevel.detach();
          }
          if (ownsSession()) {
            this.setState("listening");
          }
        },
        { once: true },
      );

      void element.play().catch(() => {
        logMediaEvent("play_rejected");
        if (ownsSession()) {
          void this.handleFailure("Lumine connected, but audio playback was blocked.");
        }
      });

      // Start measuring her voice.
      //
      // `track.mediaStream` is the same samples the element is about to play, so
      // the meter and the speaker cannot disagree — which is the failure you get
      // from a second `getUserMedia`, and the reason the meter is a tap on this
      // track rather than an independent capture.
      //
      // Attached after `play()` and not before: `mediaStream` exists as soon as
      // the track is attached, and a monitor started earlier would spend its
      // first frames on a stream with no audio in it yet.
      this.audioLevel.attach(track.mediaStream);
    };

    const handlePublished = (publication: RemoteTrackPublication, participant: RemoteParticipant) => {
      if (publication.kind !== Track.Kind.Audio || !isAgent(participant) || !ownsSession()) {
        return;
      }

      if (publication.track) {
        attachAudio(publication.track, publication, participant);
      } else {
        publication.setSubscribed(true);
      }
    };

    const handleAgent = (participant: RemoteParticipant) => {
      if (!isAgent(participant) || !ownsSession()) {
        return;
      }

      session.agentConnected = true;
      if (session.agentTimer !== undefined) {
        window.clearTimeout(session.agentTimer);
      }

      console.info("[Voice] Agent joined", { sessionId: session.id, identity: participant.identity });
      markLatency("agent_joined", { identity: participant.identity });
      this.setState("connected");
      this.setState("listening");
      participant.audioTrackPublications.forEach((publication) => handlePublished(publication, participant));
    };

    const handleSubscribed = (track: RemoteTrack, publication: RemoteTrackPublication, participant: RemoteParticipant) => {
      attachAudio(track, publication, participant);
    };

    const handleUnsubscribed = (track: RemoteTrack, publication?: RemoteTrackPublication) => {
      if (!ownsSession()) {
        return;
      }
      const trackSid = publication?.trackSid ?? track.sid;
      console.info("[Voice][Audio]", { sessionId: session.id, trackSid, event: "unsubscribed" });
      markLatency("agent_track_unsubscribed", { trackSid });
      if (!trackSid) {
        track.detach().forEach((element) => {
          session.remoteAudioElements.delete(element);
          element.remove();
        });
        return;
      }
      session.attachedTrackSids.delete(trackSid);
      const attached = session.audioElementsByTrack.get(trackSid);
      if (attached) {
        session.audioElementsByTrack.delete(trackSid);
        session.remoteAudioElements.delete(attached);
        attached.remove();
      }
      track.detach().forEach((element) => {
        session.remoteAudioElements.delete(element);
        element.remove();
      });
      if (session.remoteAudioElements.size === 0 && ownsSession()) {
        this.setState("listening");
      }
    };

    const handleDisconnected = () => {
      if (ownsSession()) {
        console.info("[Voice] Room disconnected", { sessionId: session.id, room: roomName });
        this.setState("disconnected");
      }
    };

    const handleReconnecting = () => {
      if (ownsSession()) {
        this.setState("connecting");
      }
    };

    /**
     * Lumine's spoken words, as text.
     *
     * ## Why this is still on the deprecated `TranscriptionReceived`
     *
     * The modern replacement is a `registerTextStreamHandler("lk.transcription", …)`
     * on the room, and it is deliberately *not* used. With the defaults this
     * project runs, the agent publishes the same words twice:
     *
     * - `RoomIO` builds a `_ParticipantTranscriptionOutput` and drives
     *   `capture_text()`, which writes to the `lk.transcription` text stream.
     * - Because `sync_transcription` is left at its default, it *also* builds a
     *   `TranscriptSynchronizer`, whose `push_text`/`flush` call the deprecated
     *   `room.local_participant.publish_transcription()` — which is what raises
     *   `TranscriptionReceived` here.
     *
     * Both carry the same content, so registering the stream handler alongside
     * this would post every message twice. Migrating means setting
     * `sync_transcription=False` in `agent/pipeline/pipeline_factory.py` first, which is an
     * agent-side change and therefore out of scope for a frontend pass.
     *
     * The `deprecated` tag is a warning, not a removal: the event still fires on
     * 1.8.3, and it is the only channel that delivers this content. What is *not*
     * delivered here is the user's own speech — the agent consumes user STT
     * internally as `user_input_transcribed` and never republishes it — so the
     * branch that would mark a message `role: "user"` from this event cannot fire
     * today. It is kept because the day the agent does publish it, the transcript
     * should already be able to show it; dropping the branch would lose the text
     * rather than merely mislabel it.
     */
    const handleTranscription = (segments: TranscriptionSegment[], participant?: Participant) => {
      if (!ownsSession() || !participant) {
        return;
      }

      const text = segments.map((segment) => segment.text).join(" ").trim();
      if (!text) {
        return;
      }

      const message: VoiceMessage = {
        role: isAgent(participant) ? "lumine" : "user",
        content: text,
        timestamp: new Date(),
        type: "voice",
        status: segments.every((segment) => segment.final) ? "complete" : "processing",
        sessionId: session.id,
      };

      const rawId = segments[0]?.id;
      if (rawId) {
        if (session.transcriptMessageIds.has(rawId)) {
          this.callbacks.onUpdateMessage(rawId, message as Partial<Record<string, unknown>>);
        } else {
          session.transcriptMessageIds.add(rawId);
          this.callbacks.onMessage({ ...message, id: rawId } as Record<string, unknown>);
        }
      } else {
        this.callbacks.onMessage(message as Record<string, unknown>);
      }
    };

    const handleDataReceived = (payload: Uint8Array, participant?: RemoteParticipant, _kind?: unknown, topic?: string) => {
      if (
        !ownsSession() ||
        !participant ||
        !isAgent(participant) ||
        (topic !== "lumine.emotion" && topic !== "lumine.tool" && topic !== "lumine.notice")
      ) {
        return;
      }

      const receivedAt = performance.now();
      if (EMOTION_DEBUG) console.info("[Voice] data packet received", { sessionId: session.id, topic, sender: participant.identity, receivedAt });
      try {
        const decoded = JSON.parse(new TextDecoder().decode(payload)) as {
          type?: string;
          payload?: Partial<LumineEmotionIntent> & { primary?: string; source?: string };
        };
        if (topic === "lumine.tool") {
          const toolEvent = parseToolStatus(decoded, session.id);
          if (toolEvent) {
            this.callbacks.onToolEvent(toolEvent);
          }
          return;
        }
        if (topic === "lumine.notice") {
          const notice = parseNotice(decoded, session.id);
          if (notice) {
            this.callbacks.onNotice(notice);
          }
          return;
        }
        if (decoded.type !== "lumine.emotion" || !decoded.payload) {
          return;
        }
        const emotion = resolveEmotionIntent(decoded.payload);
        if (EMOTION_DEBUG) console.info("[Emotion] parsed", emotion);
        this.callbacks.onEmotion(emotion);
      } catch (error) {
        console.warn("[Voice] Ignoring malformed data event", error);
      }
    };

    const room = session.room;
    room.on(RoomEvent.ParticipantConnected, handleAgent);
    room.on(RoomEvent.TrackPublished, handlePublished);
    room.on(RoomEvent.TrackSubscribed, handleSubscribed);
    room.on(RoomEvent.TrackUnsubscribed, handleUnsubscribed);
    room.on(RoomEvent.Disconnected, handleDisconnected);
    room.on(RoomEvent.Reconnecting, handleReconnecting);
    room.on(RoomEvent.TranscriptionReceived, handleTranscription);
    room.on(RoomEvent.DataReceived, handleDataReceived);

    try {
      console.info("[Voice] Creating session", { sessionId, room: roomName, identity });
      markLatency("creating_session");

      if (!LIVEKIT_URL) {
        throw new Error("LiveKit is not configured. Set VITE_LIVEKIT_URL and try again.");
      }

      // The app owns the worker's lifecycle. Without this, voice silently depends
      // on a Python process someone remembered to start by hand, and a saved
      // settings change does nothing until it is restarted by hand too.
      //
      // `start_agent` is idempotent (it returns the existing status if the managed
      // child is alive) and it hands the child the app's config path plus any
      // keyring credentials, so the worker reads the settings the UI shows. A
      // worker already running from `lk agent dev` is not visible to Rust and will
      // briefly coexist with this one; LiveKit dispatches a room to a single agent,
      // so the second simply idles.
      await this.ensureWorker(sessionId, markLatency);

      const token = await withTimeout(getLiveKitToken(roomName, identity), TOKEN_TIMEOUT_MS, "Couldn't create a secure voice session token.");
      markLatency("token_ready");
      if (!ownsSession()) {
        return;
      }

      console.info("[Voice] Starting session", { sessionId, room: roomName });
      await withTimeout(room.connect(LIVEKIT_URL, token), CONNECT_TIMEOUT_MS, "Couldn't connect to LiveKit.");
      markLatency("room_connected");
      if (!ownsSession()) {
        return;
      }

      await room.localParticipant.setMicrophoneEnabled(true);
      markLatency("microphone_ready");
      this.muted = false;
      this.setState("initializing");

      session.agentTimer = window.setTimeout(() => {
        if (!ownsSession() || session.agentConnected) {
          return;
        }

        void this.handleFailure("Lumine's voice service did not join the session.");
      }, AGENT_TIMEOUT_MS);

      await withTimeout(invoke<string>("dispatch_agent", { room: roomName, metadata: interruptionMetadata(interruptionMode) }), DISPATCH_TIMEOUT_MS, "Lumine's voice service could not be dispatched.");
      markLatency("agent_dispatched", { interruptionMode });
      if (!ownsSession()) {
        return;
      }

      console.info("[Voice] Agent dispatched", { sessionId, room: roomName, agent: "lumine" });
      this.setState("connecting");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Lumine voice session failed.";
      if (ownsSession()) {
        await this.handleFailure(message);
      }
    } finally {
      this.operationLock = false;
    this.lockHeldAt = null;
    }
  }

  async stop(): Promise<void> {
    const session = this.activeSession;
    if (!session) {
      return;
    }

    this.operationLock = true;
    this.lockHeldAt = performance.now();
    this.state = "ending";
    this.error = null;
    this.publish();

    const active = this.activeSession;
    const generation = session.generation;
    this.activeSession = null;
    this.state = "disconnected";
    this.muted = false;
    this.error = null;
    this.publish();

    if (active) {
      await this.cleanupSession(active, "user stopped session");
    }

    this.operationLock = false;
    this.lockHeldAt = null;
    this.generation = Math.max(this.generation, generation + 1);
  }

  async setMuted(muted: boolean): Promise<void> {
    const session = this.activeSession;
    if (!session || session.disposed || this.state === "disconnecting") {
      return;
    }

    await session.room.localParticipant.setMicrophoneEnabled(!muted);
    session.muted = muted;
    this.muted = muted;
    this.publish();
  }

  /**
   * Whether a typed message can reach Lumine right now.
   *
   * A getter rather than a value the UI keeps in step, because the two drift: a
   * `canSendText` boolean held in state is a second copy of a fact the manager
   * already knows, and the copy is wrong for exactly as long as a call ends
   * between the render and the click.
   */
  get canSendText(): boolean {
    const session = this.activeSession;
    return Boolean(session && !session.disposed && this.state !== "disconnecting" && this.state !== "ending" && session.agentConnected);
  }

  /**
   * Say something to Lumine in text.
   *
   * This is a real turn, not a local note. `lk.chat` is the topic `RoomIO`
   * registers on the agent side unconditionally, so the text arrives as a
   * `TextInputEvent` and the model answers it in her own voice — the same
   * pipeline, the same persona, the same tools. Nothing on the Python side had
   * to change for that to be true, which is why it was worth checking the
   * installed agent rather than adding an endpoint.
   *
   * ## Why the topic is explicit
   *
   * `sendText` with no options defaults to `lk.chat`, so naming it looks
   * redundant. It is named anyway, because this is the one place where a silent
   * default change would be invisible: send to the wrong topic and the message
   * vanishes with no error on either side, which reads as "the app is broken"
   * rather than as "the topic was wrong". The constant is imported from the
   * client's own types so a rename upstream is a compile error here.
   *
   * ## Why the return value is a boolean and not a throw
   *
   * The caller is a composer, and a composer must not be able to take the
   * session down by being submitted at the wrong moment. A rejected promise
   * inside an input handler is an unhandled rejection; a `false` is something the
   * UI can leave the draft alone and say nothing about.
   */
  async sendText(text: string): Promise<boolean> {
    const trimmed = text.trim();
    if (!trimmed) return false;

    const session = this.activeSession;
    if (!session || session.disposed || !this.canSendText) {
      this.error = "There is no open call to send that to.";
      this.callbacks.onError(this.error);
      this.publish();
      return false;
    }

    try {
      await session.room.localParticipant.sendText(trimmed, { topic: TOPIC_CHAT });
      return true;
    } catch (error) {
      // Reported rather than swallowed. A typed message that fails to send is the
      // one failure where the user has no other way to find out — they pressed
      // Enter, the draft cleared, and Lumine said nothing.
      console.warn("[Voice] Text send failed", error);
      this.error = "That message did not reach Lumine.";
      this.callbacks.onError(this.error);
      this.publish();
      return false;
    }
  }

  /**
   * Put a captured stream into the room, replacing whatever video was there.
   *
   * The caller owns the `MediaStream` -- it came from `useLocalMedia`, which is
   * also what stops it. This only wraps and publishes, so there is one capture
   * and one teardown rather than a second one hidden inside LiveKit's own
   * `setScreenShareEnabled`, whose tracks this module would not be able to
   * release.
   *
   * Publishing the previous source first would be the more polite order, but it
   * would also leave a window where both are live and the model is looking at
   * the outgoing one. Unpublishing first costs a frame of nothing.
   */
  async publishVideo(source: "camera" | "screen", stream: MediaStream): Promise<boolean> {
    const session = this.activeSession;
    if (!session || session.disposed || this.state === "disconnecting") {
      return false;
    }
    // Nothing to publish if the capture produced no video track. A `MediaStream`
    // with only audio is a legal thing to hand this function, and wrapping its
    // `undefined` would fail somewhere less obvious than here.
    const mediaTrack = stream.getVideoTracks()[0];
    if (!mediaTrack) {
      this.error = "The capture produced no video to share.";
      this.callbacks.onError(this.error);
      this.publish();
      return false;
    }
    try {
      await this.unpublishFrom(session);
      // `userProvidedTrack` is true because the track was captured by
      // `useLocalMedia` rather than created by LiveKit, which is what tells the
      // SDK not to try to manage its lifecycle itself.
      const track = new LocalVideoTrack(mediaTrack, undefined, true);
      await session.room.localParticipant.publishTrack(track, { source: source === "screen" ? Track.Source.ScreenShare : Track.Source.Camera });
      session.publishedVideo = track;
      session.publishedVideoSource = source;
      return true;
    } catch {
      // The message names the camera even for a screen share, because this is
      // reached when the room rejected the track rather than when the capture
      // failed -- and the capture's own failures are reported by
      // `useLocalMedia`, which is the only thing that can tell them apart.
      this.error = "The capture could not be shared with Lumine.";
      this.callbacks.onError(this.error);
      this.publish();
      return false;
    }
  }

  /**
   * Take the published video back out and stop the track.
   *
   * `unpublish` does not stop the underlying media, so the `stop()` here is what
   * actually turns the camera light off. Skipping it is how a desktop app ends
   * up recording to nobody.
   *
   * Takes the session explicitly rather than reading `activeSession`, because
   * teardown runs against a captured session that may already have been
   * replaced. Reading the field there would unpublish the *new* session's
   * camera, or nothing at all, depending on timing.
   */
  private async unpublishFrom(session: SessionContext | null): Promise<void> {
    const track = session?.publishedVideo;
    if (!session || !track) return;
    session.publishedVideo = undefined;
    session.publishedVideoSource = undefined;
    try {
      await session.room.localParticipant.unpublishTrack(track, true);
    } catch {
      // The room may already be gone. The track still has to be stopped, so this
      // is swallowed rather than propagated -- there is nothing left to publish to.
    }
  }

  async unpublishVideo(): Promise<void> {
    await this.unpublishFrom(this.activeSession);
  }

  private async handleFailure(message: string) {
    const session = this.activeSession;
    if (!session) {
      this.error = message;
      this.state = "error";
      this.publish();
      this.callbacks.onError(message);
      return;
    }

    this.activeSession = null;
    this.error = message;
    this.state = "error";
    this.publish();
    this.callbacks.onError(message);
    await this.cleanupSession(session, message);
    this.operationLock = false;
    this.lockHeldAt = null;
    this.state = "disconnected";
  }
}
