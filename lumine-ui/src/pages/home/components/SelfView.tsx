import { useEffect, useRef } from "react";
import { Icon } from "./Icon";

/**
 * The user's own picture, beside Lumine.
 *
 * ## Why the badge states a fact rather than a reassurance
 *
 * A live preview in the corner of a window is the exact shape of a video call
 * with one participant who cannot see you, and there is no way to tell the two
 * apart by looking. So the badge says which one this is, and it says it from a
 * fact -- whether the track is in the room -- rather than from whether the model
 * *could* read it.
 *
 * Those two are not the same, and the difference is the whole point. A model
 * that declares no image input leaves the picture here and nothing else, which
 * is what "Only you can see this" means. A model that can see, whose publish
 * failed, also gets "Only you can see this" rather than a claim it is not making.
 */
export type SelfViewProps = {
  /** The live capture, or null. Never both camera and screen. */
  stream: MediaStream;
  /** Which one it is. Decides the badge and the label. */
  source: "camera" | "screen";
  /** Whether this track is actually published to the room. */
  isPublished: boolean;
};

export function SelfView({ stream, source, isPublished }: SelfViewProps) {
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

  return (
    <aside className={`self-view self-view--${source}`} aria-label="Your camera preview">
      <video ref={videoRef} className="self-view-video" muted playsInline autoPlay />
      <div className="self-view-bar flex items-center justify-between gap-2 py-1.5 px-[9px] bg-surface">
        <span className="self-view-source inline-flex items-center gap-1 text-soft text-[11px]">
          <Icon name={source === "camera" ? "video" : "monitor"} size={12} />
          {source === "camera" ? "Camera" : "Screen"}
        </span>
        <span className={"self-view-badge py-0.5 px-1.5 rounded-full text-faint bg-surface-muted text-[10px] tracking-[0.01em] whitespace-nowrap" + (isPublished ? " is-shared" : "")}>
          {isPublished ? "Shared with Lumine" : "Only you can see this"}
        </span>
      </div>
    </aside>
  );
}
