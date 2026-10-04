import { Icon } from "./Icon";

/**
 * The window Lumine is looking at, when `desktop_control` is in use.
 *
 * ## Why this is a list and not a picture
 *
 * The desktop tool reads the Windows accessibility tree rather than capturing
 * pixels, so it knows *names* — "Save", "Search", "Delete" — rather than a
 * rectangle of pixels. Showing those names is both what the data actually is and
 * more useful than a screenshot: a user glancing at this can see whether
 * Lumine is about to click the thing they meant. A JPEG here would be a
 * lower-resolution, more expensive picture of less.
 *
 * ## Why it is not a toast
 *
 * Same reason as `BrowserPreview`, and the same accumulation problem: a toast is
 * gone in four seconds, which is exactly the moment a user reaches for the mouse
 * and wants to know what she is about to touch. Desktop control is the capability
 * where "what is she looking at" is a question with real consequences.
 *
 * ## Why the generation is on the face of it
 *
 * The worker mints a generation per observation and refuses any action planned
 * against an older one. Showing that number is what lets a user correlate what
 * they see with what the model is reasoning about — and it is the number the
 * permission card is asking them to approve against.
 *
 * ## The two warnings, and why each is worth its row
 *
 * **`Nothing here can be clicked by name`** is not a failure. A canvas, a game,
 * or a terminal exposes no names, and Lumine can still act there on a
 * coordinate — which is the weakest tier and always asks first. Hiding that
 * would make a working-but-limited screen look broken.
 *
 * **DPI warning** means the process could not opt into per-monitor DPI
 * awareness, so coordinates it reads and coordinates it clicks are in different
 * spaces and every click lands at the scale factor's distance from where it was
 * aimed. It is rare, it is silent, and a user who has not been told will assume
 * the feature is broken rather than mis-scaled.
 */

export type DesktopPreviewProps = {
  /** The latest observation, or null when the desktop tool has not looked yet. */
  state: {
    generation: number;
    window: string;
    process: string;
    addressable: boolean;
    dpiAligned: boolean;
    elements: number;
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

export function DesktopPreview({ state }: DesktopPreviewProps) {
  if (!state) return null;

  const age = ageLabel(state.ageMs);
  const stale = state.ageMs !== undefined && state.ageMs > 20_000;

  return (
    <aside className="desktop-preview" aria-label="The window Lumine is working in">
      <div className="desktop-preview-bar">
        <span className="desktop-preview-window">
          <Icon name="monitor" size={12} />
          <span className="desktop-preview-title">
            {state.window || "your desktop"}
          </span>
        </span>
        <span className="desktop-preview-badge">
          {/* The generation is what the model clicks against, so it is shown
              rather than hidden: a user approving an action wants to know which
              look of the screen it was planned against. */}
          Lumine is looking at this · screen #{state.generation}
        </span>
      </div>

      <div className="desktop-preview-facts">
        {state.addressable ? (
          <span className="desktop-preview-fact">
            {state.elements} {state.elements === 1 ? "control" : "controls"} she can
            click
          </span>
        ) : (
          <span className="desktop-preview-fact is-warn">
            Nothing here can be clicked by name
          </span>
        )}
        {state.process ? (
          <span className="desktop-preview-fact is-quiet">{state.process}</span>
        ) : null}
        {age ? (
          <span className={stale ? "desktop-preview-fact is-warn" : "desktop-preview-fact is-quiet"}>
            looked {age}
          </span>
        ) : null}
      </div>

      {!state.dpiAligned ? (
        <p className="desktop-preview-warning">
          This system would not let Lumine read screen positions the same way she
          clicks them, so clicks may land slightly off. Scaling the display to
          100% usually fixes it.
        </p>
      ) : null}
    </aside>
  );
}