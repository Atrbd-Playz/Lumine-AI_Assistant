import { recommendedEyeMode } from "../../components/avatar/expressiveEyePaths";
import type { LumineEmotion, LumineEmotionEventEnvelope, LumineEmotionIntent, LumineEmotionSource } from "../../components/avatar/avatarTypes";

const EMOTION_DEBUG = import.meta.env.VITE_LUMINE_DEBUG_EMOTION === "true";

export const CANONICAL_EMOTIONS: LumineEmotion[] = [
  "neutral",
  "happy",
  "loving",
  "delighted",
  "amused",
  "excited",
  "playful",
  "mischievous",
  "jealous",
  "wink",
  "sleepy",
  "sad",
  "surprised",
  "embarrassed",
  "shy",
  "confused",
  "thinking",
  "angry",
  "curious",
  "focused",
  "proud",
  "worried",
  "relieved",
  "determined",
  "calm",
  "alert",
  "concerned",
  "idle",
];

const EMOTION_ALIASES: Record<string, LumineEmotion> = {
  idle: "neutral",
  neutral: "neutral",
  default: "neutral",
  none: "neutral",
  delighted: "delighted",
  amused: "amused",
  relieved: "relieved",
  determined: "determined",
  calm: "calm",
  alert: "alert",
  concerned: "concerned",
  mischievous: "mischievous",
};

const validateSource = (source: string): LumineEmotionSource =>
  source === "llm" || source === "heuristic" || source === "voice" || source === "system" || source === "user" || source === "lab"
    ? source
    : "system";

export function normalizeEmotion(value: string | LumineEmotion | undefined): LumineEmotion {
  if (!value) return "neutral";
  const normalized = String(value).trim().toLowerCase().replace(/-/g, "_");
  return EMOTION_ALIASES[normalized] ?? (CANONICAL_EMOTIONS.includes(normalized as LumineEmotion) ? normalized as LumineEmotion : "neutral");
}

export function clampIntensity(raw: number | undefined): number {
  const value = Number.isFinite(raw) ? Number(raw) : 0.5;
  return Math.min(1, Math.max(0, value));
}

export function resolveEmotionIntent(intent: Partial<LumineEmotionIntent> & { primary?: LumineEmotion | string; source?: string }): LumineEmotionIntent {
  const primary = normalizeEmotion(intent.primary ?? "neutral");
  const secondary = intent.secondary ? normalizeEmotion(String(intent.secondary)) : undefined;
  const resolved = {
    primary,
    secondary: secondary === primary ? undefined : secondary,
    intensity: clampIntensity(intent.intensity ?? 0.5),
    durationMs: typeof intent.durationMs === "number" ? intent.durationMs : undefined,
    source: validateSource(String(intent.source ?? "system")),
    priority: typeof intent.priority === "number" ? intent.priority : 0,
  };
  if (EMOTION_DEBUG) console.info("[Emotion] controller resolved", resolved);
  return resolved;
}

export function buildEmotionEvent(intent: Partial<LumineEmotionIntent> | undefined): LumineEmotionEventEnvelope | null {
  if (!intent) return null;
  const resolved = resolveEmotionIntent(intent as Partial<LumineEmotionIntent> & { primary?: LumineEmotion | string; source?: string });
  return {
    version: 1,
    type: "lumine.emotion",
    payload: {
      primary: resolved.primary,
      secondary: resolved.secondary ?? undefined,
      intensity: resolved.intensity,
      durationMs: resolved.durationMs ?? null,
      source: resolved.source,
      priority: resolved.priority,
    },
  };
}

export function resolveExpressionMode(emotion: LumineEmotion): "mask" | "arc" {
  return recommendedEyeMode(emotion);
}

export class EmotionController {
  private active?: LumineEmotionIntent;
  private previous?: LumineEmotionIntent;

  trigger(rawIntent: Partial<LumineEmotionIntent> & { primary?: LumineEmotion | string; source?: string }) {
    const next = resolveEmotionIntent(rawIntent);
    this.previous = this.active;
    this.active = next;
    return next;
  }

  getCurrent(): LumineEmotionIntent | undefined {
    return this.active;
  }

  getPrevious(): LumineEmotionIntent | undefined {
    return this.previous;
  }
}
