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

/**
 * A CSS `font-family` for one of the six named interface faces.
 *
 * It lives here rather than in `Home.tsx` because there is a second caller now:
 * the appearance settings draw every typeface option *in its own face*, and a
 * chooser that samples a face with a stack built somewhere else is a chooser that
 * quietly disagrees with the app the moment one of the two is edited.
 *
 * The fallback is a real part of the answer, not defensive noise. Each face is
 * loaded from a webfont that can be absent — offline, blocked, or a machine that
 * never fetched it — and the generic after the name is what the browser reaches
 * for instead. A trailing `sans-serif` on a serif face would be wrong, which is
 * why the generic is chosen per family rather than appended to all of them.
 */
export function fontStack(font: string): string {
  switch (font) {
    case "Newsreader":
      return "Newsreader, serif";
    case "DM Mono":
      return "DM Mono, monospace";
    case "Space Grotesk":
      return "Space Grotesk, sans-serif";
    case "Roboto":
      return "Roboto, sans-serif";
    case "Ubuntu":
      return "Ubuntu, sans-serif";
    case "Manrope":
      return "Manrope, sans-serif";
    default:
      // An uncatalogued face. The name is still worth trying — a stored profile
      // may name a font this build has not heard of — but sans-serif has to be
      // there or the browser uses its default serif for a UI.
      return `"${font}", sans-serif`;
  }
}
