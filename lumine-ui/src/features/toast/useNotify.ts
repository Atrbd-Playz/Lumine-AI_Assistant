import { useCallback } from "react";
import { toast } from "@/components/ui/toast";

/**
 * Transient notifications.
 *
 * The vocabulary is the one the app already used — a tone and a message — so
 * call sites read the same as before. What changes is what happens next: toasts
 * queue instead of overwriting each other, they expire on their own, and an
 * error stays long enough to be read.
 */

export type ToastTone = "success" | "error" | "info" | "warning";

export type NotifyOptions = {
  /** Defaults to `info`. */
  tone?: ToastTone;
  /** The line that carries the meaning. */
  message: string;
  /** Optional short headline. One is derived from the tone when omitted. */
  title?: string;
  /** Milliseconds on screen. `0` keeps it until dismissed. */
  timeout?: number;
};

const TONE_TITLE: Record<ToastTone, string> = {
  success: "Done",
  error: "Something went wrong",
  info: "Heads up",
  warning: "Check this",
};

/**
 * Errors linger and interrupt; confirmations get out of the way. A user who
 * missed a success can act on it, but a user who missed an error will assume
 * the thing worked.
 */
const TONE_TIMEOUT: Record<ToastTone, number> = {
  success: 4000,
  info: 5000,
  warning: 8000,
  error: 10_000,
};

export function useNotify() {
  return useCallback(({ tone = "info", message, title, timeout }: NotifyOptions) => {
    toast.add({
      type: tone,
      title: title ?? TONE_TITLE[tone],
      description: message,
      timeout: timeout ?? TONE_TIMEOUT[tone],
      // Errors are announced urgently so a screen reader interrupts rather than
      // waiting for a pause in whatever the user is doing.
      priority: tone === "error" ? "high" : "low",
    });
  }, []);
}
