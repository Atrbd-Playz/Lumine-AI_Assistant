export const INTERRUPTION_MODES = ["finish_response", "barge_in"] as const;
export type InterruptionMode = (typeof INTERRUPTION_MODES)[number];
export const DEFAULT_INTERRUPTION_MODE: InterruptionMode = "barge_in";

export function normalizeInterruptionMode(value: unknown): InterruptionMode {
  return value === "finish_response" || value === "barge_in" ? value : DEFAULT_INTERRUPTION_MODE;
}

export function interruptionMetadata(mode: InterruptionMode): string {
  return JSON.stringify({ interruption_mode: mode });
}
