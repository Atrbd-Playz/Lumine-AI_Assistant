import { useEffect, useRef } from "react";
import { Icon } from "./Icon";

/**
 * The user's own picture, beside Lumine.
 *
 * ## Why the caption is not optional
 *
 * The frames here are not reaching Lumine. `agent.py` starts its session with no
 * video options and no frame handler, so a live preview in the corner of the
 * window would read as a video call with one participant who cannot see you. The
 * caption is the difference between "your camera is on" and "you are being
 * filmed by an app you cannot check", and it is a fact rather than a warning: the
 * badge turns to "Shared with Lumine" the moment the agent can receive frames,
 * and nothing else about this card changes.
 */
export type SelfViewProps = {
  /** A camera or screen-capture track, or null when neither is on. */
  stream: MediaStream | null;
  /** Which one it is. Decides the badge and the label. */
  source: "camera" | "screen";
  /** Whether the active model could actually consume frames. */
  canReachLumine: boolean;
};

export function SelfView({ stream, source, canReachLumine }: SelfViewProps) {
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
      <div className="self-view-bar">
        <span className="self-view-source">
          <Icon name={source === "camera" ? "video" : "monitor"} size={12} />
          {source === "camera" ? "Camera" : "Screen"}
        </span>
        <span className={"self-view-badge" + (canReachLumine ? " is-shared" : "")}>
          {canReachLumine ? "Shared with Lumine" : "Only you can see this"}
        </span>
      </div>
    </aside>
  );
}
