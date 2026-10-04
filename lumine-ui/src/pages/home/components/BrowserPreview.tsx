import { useEffect, useState } from "react";
import { Icon } from "./Icon";

/**
 * What Lumine's browser is looking at.
 *
 * ## Why this is not a toast and not a transcript entry
 *
 * A screenshot arrives on its own callback, separate from tool events, for two
 * reasons that are both about accumulation. A toast is transient and small; a
 * base64 JPEG in one is a decode stall and then nothing. The transcript is the
 * opposite problem: it accumulates, so a single picture there would sit in state
 * for the rest of the conversation holding megabytes nobody asked to keep.
 *
 * ## Why the badge says "what Lumine can see"
 *
 * This is the honest reading, and the difference is load-bearing. The worker
 * publishes the preview every time it screenshots, but Lumine does *not* see the
 * picture unless the model explicitly asked for it — a tool result is re-billed
 * on every later turn, so handing her an image costs tokens for the rest of the
 * session. So a frame here is evidence of what the *browser* shows, not of what
 * the *model* read.
 *
 * Which is the same distinction `SelfView` draws between a published track and a
 * model that can receive frames. Two surfaces, one rule: never claim a share that
 * is not happening.
 *
 * ## Why the age is shown
 *
 * A picture with no timestamp reads as live. This is a photograph of a page that
 * may have changed since, and `visual_state` publishes frame ages on the camera
 * side for exactly this reason. A preview that claims to be "now" after thirty
 * seconds is a lie about a browser the user cannot see.
 */
export type BrowserPreviewProps = {
  /** The data URL, or null when nothing has been captured this session. */
  preview: {
    image: string;
    url: string;
    title: string;
    /** Epoch ms on the worker's clock; 0 when unknown. */
    capturedAt: number;
    /** Milliseconds since capture, measured on arrival. */
    ageMs?: number;
  } | null;
};

/** Seconds, or "just now". Rounded up, because "0s old" reads as broken. */
function ageLabel(ageMs: number | undefined): string | null {
  if (ageMs === undefined) return null;
  const seconds = Math.ceil(ageMs / 1000);
  if (seconds < 3) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.round(minutes / 60)}h ago`;
}

export function BrowserPreview({ preview }: BrowserPreviewProps) {
  const [failed, setFailed] = useState(false);

  // Reset the failure when a new picture arrives, or one broken decode would
  // blank every preview for the rest of the session.
  useEffect(() => {
    setFailed(false);
  }, [preview?.image]);

  if (!preview) return null;

  const age = ageLabel(preview.ageMs);
  // The host rather than the full URL: a preview is a glance, and a full query
  // string is both noise and a place for a token to sit on screen.
  let host = "";
  try {
    host = preview.url ? new URL(preview.url).host : "";
  } catch {
    host = "";
  }

  return (
    <aside className="browser-preview" aria-label="The page Lumine is browsing">
      {failed ? (
        <div className="browser-preview-failed">
          <Icon name="globe" size={16} />
          <span>That page preview did not load.</span>
        </div>
      ) : (
        <img
          className="browser-preview-image"
          src={preview.image}
          alt={preview.title ? `Screenshot of ${preview.title}` : "Screenshot of the current page"}
          onError={() => setFailed(true)}
        />
      )}
      <div className="browser-preview-bar">
        <span className="browser-preview-host">
          <Icon name="globe" size={11} />
          {host || "the browser"}
        </span>
        <span className="browser-preview-badge">
          {age ? `Lumine can see this · ${age}` : "Lumine can see this"}
        </span>
      </div>
    </aside>
  );
}