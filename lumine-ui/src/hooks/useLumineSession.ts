import { useCallback, useEffect, useRef, useState } from "react";
import { ParticipantKind, Room, RoomEvent, Track, type Participant, type RemoteParticipant, type RemoteTrack, type RemoteTrackPublication, type TranscriptionSegment } from "livekit-client";
import { invoke } from "@tauri-apps/api/core";
import { getLiveKitToken, LIVEKIT_URL } from "../lib/livekit";
import type { ConversationMessage } from "../pages/home/conversation/types";

export type LumineConnectionState = "idle" | "connecting" | "waiting" | "listening" | "speaking" | "reconnecting" | "disconnecting" | "error";

type SessionOptions = {
  onMessage: (message: Omit<ConversationMessage, "id"> & { id?: string }) => void;
  onUpdateMessage: (id: string, changes: Partial<Omit<ConversationMessage, "id">>) => void;
  onError: (message: string) => void;
};

const AGENT_IDENTITY = "Lumine";
const AGENT_TIMEOUT_MS = 30_000;

function isAgent(participant: Participant) {
  return participant.kind === ParticipantKind.AGENT || participant.identity.toLowerCase() === AGENT_IDENTITY.toLowerCase() || participant.name?.toLowerCase() === AGENT_IDENTITY.toLowerCase() || participant.metadata?.includes('"role":"agent"') === true;
}

export function useLumineSession({ onMessage, onUpdateMessage, onError }: SessionOptions) {
  const [status, setStatus] = useState<LumineConnectionState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const roomRef = useRef<Room | null>(null);
  const generationRef = useRef(0);
  const agentTimerRef = useRef<number | null>(null);
  const audioElementsRef = useRef<Set<HTMLAudioElement>>(new Set());
  const transcriptIdsRef = useRef<Set<string>>(new Set());

  const fail = useCallback((message: string) => {
    setError(message);
    setStatus("error");
    onError(message);
  }, [onError]);

  const cleanup = useCallback(async () => {
    if (agentTimerRef.current !== null) window.clearTimeout(agentTimerRef.current);
    agentTimerRef.current = null;
    audioElementsRef.current.forEach((element) => { element.pause(); element.remove(); });
    audioElementsRef.current.clear();
    transcriptIdsRef.current.clear();
    const room = roomRef.current;
    roomRef.current = null;
    room?.removeAllListeners();
    await room?.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
    room?.disconnect();
    setStartedAt(null);
  }, []);

  const disconnect = useCallback(async () => {
    generationRef.current += 1;
    setStatus("disconnecting");
    await cleanup();
    setStatus("idle");
    setError(null);
  }, [cleanup]);

  const connect = useCallback(async () => {
    if (status !== "idle" && status !== "error") return;
    const generation = ++generationRef.current;
    if (!LIVEKIT_URL) {
      fail("LiveKit is not configured. Set VITE_LIVEKIT_URL and try again.");
      return;
    }

    setStatus("connecting");
    setError(null);
    const roomName = `lumine-session-${crypto.randomUUID()}`;
    const identity = `user-${crypto.randomUUID()}`;
    const room = new Room({ adaptiveStream: true, dynacast: true });
    roomRef.current = room;

    const handleAgent = (participant: RemoteParticipant) => {
      if (!isAgent(participant) || generationRef.current !== generation) return;
      if (agentTimerRef.current !== null) window.clearTimeout(agentTimerRef.current);
      setStatus("listening");
    };
    const handleSubscribed = (track: RemoteTrack, publication: RemoteTrackPublication, participant: RemoteParticipant) => {
      if (track.kind !== Track.Kind.Audio || !isAgent(participant)) return;
      const element = track.attach();
      element.autoplay = true;
      audioElementsRef.current.add(element);
      setStatus("speaking");
      element.addEventListener("ended", () => { audioElementsRef.current.delete(element); element.remove(); setStatus("listening"); }, { once: true });
      void element.play().catch(() => fail("Audio playback was blocked. Click the microphone again to retry."));
      void publication;
    };
    const handleUnsubscribed = (track: RemoteTrack) => {
      track.detach().forEach((element) => { audioElementsRef.current.delete(element); element.remove(); });
      setStatus("listening");
    };
    const handleDisconnected = () => { if (generationRef.current === generation) void cleanup().then(() => setStatus("idle")); };
    const handleReconnecting = () => setStatus("reconnecting");
    const handleReconnected = () => setStatus("listening");
    const handleTranscription = (segments: TranscriptionSegment[], participant?: Participant) => {
      const text = segments.map((segment) => segment.text).join(" ").trim();
      if (!text || !participant) return;
      const id = segments[0]?.id;
      const message: Omit<ConversationMessage, "id"> = { role: isAgent(participant) ? "lumine" : "user", content: text, timestamp: new Date(), type: "voice", status: segments.every((segment) => segment.final) ? "complete" : "processing" };
      if (id && transcriptIdsRef.current.has(id)) {
        onUpdateMessage(id, message);
      } else {
        if (id) transcriptIdsRef.current.add(id);
        onMessage({ ...message, id });
      }
    };

    room.on(RoomEvent.ParticipantConnected, handleAgent);
    room.on(RoomEvent.TrackSubscribed, handleSubscribed);
    room.on(RoomEvent.TrackUnsubscribed, handleUnsubscribed);
    room.on(RoomEvent.Disconnected, handleDisconnected);
    room.on(RoomEvent.Reconnecting, handleReconnecting);
    room.on(RoomEvent.Reconnected, handleReconnected);
    room.on(RoomEvent.TranscriptionReceived, handleTranscription);

    try {
      await invoke("start_agent");
      const token = await getLiveKitToken(roomName, identity);
      await room.connect(LIVEKIT_URL, token);
      if (generationRef.current !== generation) return;
      setStartedAt(Date.now());
      room.remoteParticipants.forEach(handleAgent);
      await room.localParticipant.setMicrophoneEnabled(true);
      setStatus("waiting");
      agentTimerRef.current = window.setTimeout(() => fail("Lumine did not join the room. Please try again."), AGENT_TIMEOUT_MS);
    } catch (cause) {
      if (generationRef.current !== generation) return;
      await cleanup();
      fail(cause instanceof Error ? cause.message : "Could not connect to Lumine.");
    }
  }, [cleanup, fail, onMessage, onUpdateMessage]);

  useEffect(() => () => { generationRef.current += 1; void cleanup(); }, [cleanup]);

  return { status, error, startedAt, connect, disconnect, isActive: status !== "idle" && status !== "error" };
}