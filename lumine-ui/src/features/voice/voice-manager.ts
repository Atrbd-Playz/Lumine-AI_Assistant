import { ParticipantKind, Room, RoomEvent, Track, type Participant, type RemoteParticipant, type RemoteTrack, type RemoteTrackPublication, type TranscriptionSegment } from "livekit-client";
import { invoke } from "@tauri-apps/api/core";
import { LIVEKIT_URL, getLiveKitToken } from "../../lib/livekit";
import { resolveEmotionIntent } from "../emotion/emotion-controller";
import type { LumineEmotionIntent } from "../../components/avatar/avatarTypes";
import { interruptionMetadata, type InterruptionMode } from "./interruption";

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
  disposed: boolean;
};

const AGENT_IDENTITY = "Lumine";
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

    try {
      session.room.removeAllListeners();
      await session.room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
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
