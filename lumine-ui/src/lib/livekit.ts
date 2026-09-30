import { invoke } from "@tauri-apps/api/core";

export const LIVEKIT_URL = import.meta.env.VITE_LIVEKIT_URL as string | undefined;

export async function getLiveKitToken(room: string, identity: string) {
  return invoke<string>("get_livekit_token", { room, identity });
}