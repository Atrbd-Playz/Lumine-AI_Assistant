import { ParticipantKind, Room, RoomEvent, Track, type Participant, type RemoteParticipant, type RemoteTrack, type RemoteTrackPublication, type TranscriptionSegment } from "livekit-client";
import { invoke } from "@tauri-apps/api/core";
import { LIVEKIT_URL, getLiveKitToken } from "../../lib/livekit";

export type LumineVoiceStatus =
  | "idle"
  | "connecting"
  | "initializing"
  | "listening"
  | "thinking"
  | "speaking"
  | "disconnecting"
  | "error";

export type VoiceMessage = {
  role: "user" | "lumine";
  content: string;
  timestamp: Date;
  type: "voice";
  status: "complete" | "processing";
};

export type VoiceManagerSnapshot = {
  state: LumineVoiceStatus;
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
  attachedTrackSids: Set<string>;
  agentTimer?: number;
  agentConnected: boolean;
  disposed: boolean;
};

const AGENT_IDENTITY = "Lumine";
const TOKEN_TIMEOUT_MS = 10_000;
const CONNECT_TIMEOUT_MS = 15_000;
const DISPATCH_TIMEOUT_MS = 10_000;
const AGENT_TIMEOUT_MS = 15_000;

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
  onError: (message: string) => void;
};

export class LumineVoiceManager {
  private state: LumineVoiceStatus = "idle";
  private error: string | null = null;
  private startedAt: number | null = null;
  private activeSession: SessionContext | null = null;
  private operationLock = false;
  private readonly onStateChange: (snapshot: VoiceManagerSnapshot) => void;
  private readonly callbacks: VoiceLifecycleCallbacks;

  constructor(callbacks: VoiceLifecycleCallbacks, onStateChange: (snapshot: VoiceManagerSnapshot) => void) {
    this.callbacks = callbacks;
    this.onStateChange = onStateChange;
  }

  getSnapshot(): VoiceManagerSnapshot {
    return {
      state: this.state,
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
    return this.activeSession === session && !session.disposed;
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

    if (this.activeSession === session) {
      this.activeSession = null;
      this.startedAt = null;
    }

    this.state = "idle";
    this.error = null;
    this.publish();
    console.info("[Voice] Cleanup complete", { sessionId: session.id, room: session.roomName });
  }

  async start(): Promise<void> {
    if (this.operationLock && this.activeSession) {
      return;
    }

    if (this.activeSession) {
      await this.stop();
    }

    this.operationLock = true;
    const sessionId = crypto.randomUUID();
    const roomName = createRoomName();
    const identity = `user-${sessionId.slice(0, 12)}`;

    const session: SessionContext = {
      id: sessionId,
      generation: Date.now(),
      roomName,
      identity,
      room: new Room({ adaptiveStream: true, dynacast: true }),
      remoteAudioElements: new Set(),
      attachedTrackSids: new Set(),
      agentConnected: false,
      disposed: false,
    };

    this.activeSession = session;
    this.startedAt = Date.now();
    this.setState("connecting");

    const ownsSession = () => this.isCurrentSession(session);

    const attachAudio = (track: RemoteTrack, publication: RemoteTrackPublication, participant: RemoteParticipant) => {
      if (track.kind !== Track.Kind.Audio || !isAgent(participant) || !ownsSession() || session.attachedTrackSids.has(publication.trackSid)) {
        return;
      }

      session.attachedTrackSids.add(publication.trackSid);
      const element = track.attach();
      element.autoplay = true;
      session.remoteAudioElements.add(element);
      console.info("[Voice] Audio attached", { sessionId, trackSid: publication.trackSid });
      this.setState("speaking");

      element.addEventListener(
        "ended",
        () => {
          session.remoteAudioElements.delete(element);
          element.remove();
          if (ownsSession()) {
            this.setState("listening");
          }
        },
        { once: true },
      );

      void element.play().catch(() => {
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
      this.setState("listening");
      participant.audioTrackPublications.forEach((publication) => handlePublished(publication, participant));
    };

    const handleSubscribed = (track: RemoteTrack, publication: RemoteTrackPublication, participant: RemoteParticipant) => {
      attachAudio(track, publication, participant);
    };

    const handleUnsubscribed = (track: RemoteTrack) => {
      if (!ownsSession()) {
        return;
      }
      track.detach().forEach((element) => {
        session.remoteAudioElements.delete(element);
        element.remove();
      });
    };

    const handleDisconnected = () => {
      if (ownsSession()) {
        console.info("[Voice] Room disconnected", { sessionId: session.id, room: roomName });
        this.setState("idle");
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
      };

      const rawId = segments[0]?.id;
      if (rawId) {
        this.callbacks.onUpdateMessage(rawId, message as Partial<Record<string, unknown>>);
      } else {
        this.callbacks.onMessage(message as Record<string, unknown>);
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

    try {
      console.info("[Voice] Creating session", { sessionId, room: roomName, identity });

      if (!LIVEKIT_URL) {
        throw new Error("LiveKit is not configured. Set VITE_LIVEKIT_URL and try again.");
      }

      // Development mode intentionally keeps the Python worker separate from Tauri.
      // Do not wait on the Tauri-side worker readiness gate here; the live agent is
      // already registered through `lk agent dev` and can join a fresh session room.
      const token = await withTimeout(getLiveKitToken(roomName, identity), TOKEN_TIMEOUT_MS, "Couldn't create a secure voice session token.");
      if (!ownsSession()) {
        return;
      }

      console.info("[Voice] Starting session", { sessionId, room: roomName });
      await withTimeout(room.connect(LIVEKIT_URL, token), CONNECT_TIMEOUT_MS, "Couldn't connect to LiveKit.");
      if (!ownsSession()) {
        return;
      }

      await room.localParticipant.setMicrophoneEnabled(true);
      this.setState("initializing");

      session.agentTimer = window.setTimeout(() => {
        if (!ownsSession() || session.agentConnected) {
          return;
        }

        void this.handleFailure("Lumine's voice service did not join the session.");
      }, AGENT_TIMEOUT_MS);

      await withTimeout(invoke<string>("dispatch_agent", { room: roomName }), DISPATCH_TIMEOUT_MS, "Lumine's voice service could not be dispatched.");
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
    }
  }

  async stop(): Promise<void> {
    const session = this.activeSession;
    if (!session) {
      return;
    }

    this.operationLock = true;
    this.setState("disconnecting");
    const active = this.activeSession;
    this.activeSession = null;

    if (active) {
      await this.cleanupSession(active, "user stopped session");
    }

    this.operationLock = false;
    this.setState("idle");
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
    this.state = "idle";
    this.error = null;
    this.publish();
  }

  getState() {
    return this.state;
  }
}
