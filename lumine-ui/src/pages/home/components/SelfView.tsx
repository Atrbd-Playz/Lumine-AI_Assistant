import { useEffect, useRef } from "react";
import { Icon } from "./Icon";
import { visionShareText, type VisionPhase } from "../../../lib/visualState";

/**
 * The user's own picture, and the one place that says whether Lumine is getting it.
 *
 * ## Why the statement is a header on the picture and not a second surface
 *
 * A live preview in the corner of a window is the exact shape of a video call
 * with one participant who cannot see you, and there is no way to tell the two
 * apart by looking. So the card says which one it is, and it says it from a
 * fact -- whether the track is in the room -- rather than from whether the model
 * *could* read it.
 *
 * Those two are not the same, and the difference is the whole point. A model
 * that declares no image input leaves the picture here and nothing else, which
 * is what "Only you can see this" means. A model that can see, whose publish
 * failed, also gets "Only you can see this" rather than a claim it is not making.
 *
 * It used to be said twice. A full-width status strip ran under the stage's
 * header and the card carried its own badge, and the two were rendered the
 * *same sentence* on purpose -- "so the corner and the strip can never
 * disagree" -- which is one fact paid for twice on every screen that shows it,
 * and, on the two states where the fallbacks differed, one fact stated two
 * *different* ways. `visionShareText` gave the strip "Lumine can see this
 * screen" and the badge "Shared with Lumine" for the same publish, so the two
 * surfaces that were supposed to be each other's check could be read against
 * each other and disagreed.
 *
 * The sentence now lives here, as the card's own header, which is also where a
 * reader looks for it: the picture and the claim about the picture are one
 * object. `role="status"` stayed, because the phase genuinely changes while a
 * share is live and a change announced is a change the user does not have to
 * watch for.
 *
 * ## Why there is no source label next to it
 *
 * The sentence carries the source in the two states that matter ("Lumine can
 * see this screen", "Lumine can see you"), the icon carries it in the others,
 * and a `SCREEN` caption beside it cost 43px of a card that is 176px wide at
 * the window this app opens at -- enough to ellipsise the sentence, which is
 * the payload. It is still spoken: the visually-hidden prefix names the source
 * for a screen reader whether or not the sentence happens to contain it.
 */
export type SelfViewProps = {
  /** The live capture, or null. Never both camera and screen. */
  stream: MediaStream;
  /** Which one it is. Decides the icon, the mirror, and the sentence. */
  source: "camera" | "screen";
  /** Whether this track is actually published to the room. */
  isPublished: boolean;
  /**
   * The worker's phase for this capture, or `null` before it reports one.
   *
   * It refines the header rather than replacing it: `visionShareText` returns
   * `null` for every phase this component's own wording already tells the
   * truth about, so the fallback below is what renders in the common case. The
   * three phases it does speak for are ones no local state can know — a frozen
   * share, and a model with no image input at all — which is the whole reason
   * to take a prop here. `isPublished` still decides the styling and still
   * floors the text: this card must never claim a share the track is not
   * making.
   */
  phase?: VisionPhase | null;
};

export function SelfView({ stream, source, isPublished, phase }: SelfViewProps) {
  const videoRef = useRef<HTMLVideoElement>(null);

  // The element has to be told about the stream after it exists, and the stream
  // changes identity on every toggle, so this is a subscription rather than a
  // value passed down. `srcObject` is the only way to show a live track.
  useEffect(() => {
    const element = videoRef.current;
    if (!element) return;
    element.srcObject = stream;
    // Autoplay is muted, which is also the only way a browser will start a track
    // without a user gesture of its own.
    if (stream) void element.play().catch(() => {});
    return () => {
      if (element.srcObject === stream) element.srcObject = null;
    };
  }, [stream]);

  const sourceName = source === "camera" ? "Camera" : "Screen";
  const state = visionShareText(phase, isPublished, source) ?? (isPublished ? "Shared with Lumine" : "Only you can see this");

  return (
    <figure className={`self-view self-view--${source}`}>
      <figcaption className={`self-view-status${isPublished ? " is-shared" : ""}`} role="status">
        <span className="self-view-pip" aria-hidden="true" />
        <span className="self-view-icon" aria-hidden="true">
          <Icon name={source === "camera" ? "video" : "monitor"} size={13} />
        </span>
        <span className="self-view-state">
          <span className="sr-only">{sourceName}: </span>
          {state}
        </span>
      </figcaption>
      <video
        ref={videoRef}
        className="self-view-video"
        muted
        playsInline
        autoPlay
        aria-label={source === "camera" ? "Your camera" : "Your screen"}
      />
    </figure>
  );
}
