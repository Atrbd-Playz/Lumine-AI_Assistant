import type { LumineState } from "./types";

/** Advances the demo voice state when the main microphone is clicked. */
export function getNextState(state: LumineState): LumineState {
  const nextState: Record<LumineState, LumineState> = {
    idle: "listening",
    listening: "thinking",
    thinking: "speaking",
    speaking: "idle",
  };
  return nextState[state];
}

/** Returns a readable foreground for a user-selected background color. */
export function getReadableTextColor(background: string): "#ffffff" | "#252525" {
  const normalized = background.replace("#", "");
  if (normalized.length !== 6) return "#ffffff";
  const value = Number.parseInt(normalized, 16);
  if (Number.isNaN(value)) return "#ffffff";
  const channels = [(value >> 16) & 255, (value >> 8) & 255, value & 255].map((channel) => {
    const normalizedChannel = channel / 255;
    return normalizedChannel <= 0.03928
      ? normalizedChannel / 12.92
      : Math.pow((normalizedChannel + 0.055) / 1.055, 2.4);
  });
  const luminance = channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  return luminance > 0.48 ? "#252525" : "#ffffff";
}

function getLuminance(color: string) {
  const normalized = color.replace("#", "");
  if (normalized.length !== 6) return null;
  const value = Number.parseInt(normalized, 16);
  if (Number.isNaN(value)) return null;
  const channels = [(value >> 16) & 255, (value >> 8) & 255, value & 255].map((channel) => {
    const normalizedChannel = channel / 255;
    return normalizedChannel <= 0.03928
      ? normalizedChannel / 12.92
      : Math.pow((normalizedChannel + 0.055) / 1.055, 2.4);
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

/** Keeps custom foreground colors readable without changing the user's other palette choices. */
export function getReadableForeground(color: string, backgrounds: string[], fallback: string, minimumContrast = 3) {
  const foreground = getLuminance(color);
  const isReadable = foreground !== null && backgrounds.every((background) => {
    const backgroundLuminance = getLuminance(background);
    if (backgroundLuminance === null) return false;
    const lighter = Math.max(foreground, backgroundLuminance);
    const darker = Math.min(foreground, backgroundLuminance);
    return (lighter + 0.05) / (darker + 0.05) >= minimumContrast;
  });
  return isReadable ? color : fallback;
}

export function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}
