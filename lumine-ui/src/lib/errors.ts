/** Lumine error categories — maps low-level failures to human-readable messages. */

export type ErrorCategory =
  | "network"
  | "authentication"
  | "rate_limit"
  | "provider_unavailable"
  | "microphone"
  | "permission_denied"
  | "voice_connection"
  | "tool_execution"
  | "invalid_input"
  | "unknown";

export interface CategorizedError {
  category: ErrorCategory;
  title: string;
  message: string;
  action?: string;
  technical?: string;
}

const RULES: Array<{ category: ErrorCategory; match: RegExp; title: string; message: string; action?: string }> = [
  {
    category: "rate_limit",
    match: /rate\s*limit|quota|429|too\s*many\s*requests/i,
    title: "Usage limit reached",
    message: "Lumine couldn't respond — the current API limit has been reached.",
    action: "Try again later or switch to another configured provider.",
  },
  {
    category: "authentication",
    match: /401|403|invalid.*(key|token|api)|unauthori[sz]ed|authentication/i,
    title: "Authentication problem",
    message: "The voice service couldn't verify the connection credentials.",
    action: "Check your API keys and make sure they're still valid.",
  },
  {
    category: "microphone",
    match: /microphone|mic.*(denied|blocked|permission)|audio.*(input|capture)/i,
    title: "Microphone unavailable",
    message: "Lumine couldn't access the microphone.",
    action: "Allow microphone permission in your system settings, then try again.",
  },
  {
    category: "permission_denied",
    match: /permission|denied|blocked/i,
    title: "Permission needed",
    message: "Lumine needs a permission it doesn't have yet.",
    action: "Open Settings → Permissions to review what Lumine can access.",
  },
  {
    category: "voice_connection",
    match: /livekit|room|voice.*(service|session|channel)|join.*session|dispatch/i,
    title: "Voice connection dropped",
    message: "The voice channel was interrupted.",
    action: "Check your connection and reconnect to the session.",
  },
  {
    category: "provider_unavailable",
    match: /unavailable|down|offline|timeout|timed?\s*out|504|503/i,
    title: "Service unavailable",
    message: "A cloud service Lumine depends on didn't respond in time.",
    action: "Try again in a moment.",
  },
  {
    category: "network",
    match: /network|internet|connection|fetch\s*failed|offline/i,
    title: "Network problem",
    message: "Lumine can't reach the network right now.",
    action: "Check your internet connection and try again.",
  },
  {
    category: "tool_execution",
    match: /tool|wrench|execut/i,
    title: "Tool couldn't run",
    message: "A tool Lumine tried to use didn't complete.",
    action: "Check the tool permission and try once more.",
  },
];

export function categorizeError(raw: string): CategorizedError {
  const text = raw || "";
  for (const rule of RULES) {
    if (rule.match.test(text)) {
      return {
        category: rule.category,
        title: rule.title,
        message: rule.message,
        action: rule.action,
        technical: text !== rule.message ? text : undefined,
      };
    }
  }
  // Default to a voice/unknown fallback.
  return {
    category: "unknown",
    title: "Something went wrong",
    message: text || "Lumine hit an unexpected problem.",
    action: "Try again. If it keeps happening, restart Lumine.",
    technical: text,
  };
}