import { useCallback, useEffect, useRef, useState } from "react";

/**
 * The user's own camera and screen, as a local preview.
 *
 * ## Why this is local and not part of the voice session
 *
 * The call bar's camera and screenshare buttons do not publish anything to the
 * room. `agent.py` opens a session with no video options and installs no frame
 * handler, so a published track would arrive in a room with nobody reading it —
 * and the operating system would light a recording indicator for a conversation
 * that cannot possibly see the user. So what these controls produce is a preview,
 * and the preview says so, rather than implying a video call that is not
 * happening.
 *
 * That is a statement about the agent, not a design choice about the UI. When the
 * agent gains `RoomInputOptions(video=True)` and a frame handler, the change is
 * to publish what this hook already captures; the state machine, the teardown and
 * the self-view stay exactly as they are.
 *
 * ## Teardown is the whole job
 *
 * A camera left running is the single most alarming thing a desktop app can do,
 * because the user cannot see it. So the tracks are stopped on three occasions
 * and not on a fourth: turning the control off, the session ending, and the
 * component unmounting. The screen capture's own `ended` event is handled too,
 * because the browser's "Stop sharing" bar is a button the user will press, and
 * the UI has to follow it rather than keep claiming a share that is over.
 */

export type LocalMediaSource = "camera" | "screen";

export type LocalMedia = {
  /** A live camera track, or null. The stream itself, for a preview element. */
  cameraStream: MediaStream | null;
  /** A live screen-capture track, or null. */
  screenStream: MediaStream | null;
  /** Why the camera could not start, phrased for a person rather than a log. */
  cameraError: string | null;
  screenError: string | null;
  toggleCamera: () => Promise<void>;
  toggleScreen: () => Promise<void>;
  stopAll: () => void;
};

/** The browser names these in a way no user has ever been helped by. */
function explain(source: LocalMediaSource, error: unknown): string {
  const name = (error as { name?: string } | null)?.name;
  if (name === "NotAllowedError" || name === "PermissionDeniedError") {
    return source === "camera"
      ? "Camera permission was refused. Allow it in your system settings, then try again."
      : "Screen capture was refused.";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return source === "camera" ? "No camera was found on this machine." : "No screen was available to share.";
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return source === "camera"
      ? "Another application is using the camera."
      : "The screen could not be captured.";
  }
  return source === "camera" ? "The camera could not be started." : "The screen could not be shared.";
}

function stopTracks(stream: MediaStream | null) {
  stream?.getTracks().forEach((track) => track.stop());
}

export function useLocalMedia({ enabled }: { enabled: boolean }): LocalMedia {
  const [cameraStream, setCameraStream] = useState<MediaStream | null>(null);
  const [screenStream, setScreenStream] = useState<MediaStream | null>(null);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [screenError, setScreenError] = useState<string | null>(null);
  const cameraRef = useRef<MediaStream | null>(null);
  const screenRef = useRef<MediaStream | null>(null);
  // Guards a slow permission prompt: two clicks can start two prompts, and the
  // second one to resolve wins while the first stream is orphaned and still live.
  const busyRef = useRef(false);
  // `stop()` fires `ended` on a capture track, so an intentional release would
  // otherwise reach the handler written for the user pressing the browser's own
  // "Stop sharing" bar. The two are told apart by who asked.
  const releasingRef = useRef(false);

  const release = useCallback((which: "camera" | "screen") => {
    const stream = which === "camera" ? cameraRef.current : screenRef.current;
    releasingRef.current = true;
    stopTracks(stream);
    releasingRef.current = false;
    if (which === "camera") {
      cameraRef.current = null;
      setCameraStream(null);
    } else {
      screenRef.current = null;
      setScreenStream(null);
    }
  }, []);

  const stopAll = useCallback(() => {
    release("camera");
    release("screen");
  }, [release]);

  // Ending the call releases the devices. Not deferred to unmount: the call bar
  // stays on screen while disconnected, and a camera that keeps its indicator lit
  // on a page with no call on it is the bug people report first.
  useEffect(() => {
    if (!enabled) stopAll();
  }, [enabled, stopAll]);

  useEffect(() => stopAll, [stopAll]);

  const toggleCamera = useCallback(async () => {
    if (busyRef.current) return;
    if (cameraRef.current) {
      release("camera");
      setCameraError(null);
      return;
    }
    busyRef.current = true;
    setCameraError(null);
    try {
      if (!navigator.mediaDevices?.getUserMedia) {
        throw Object.assign(new Error("unavailable"), { name: "NotFoundError" });
      }
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false,
      });
      cameraRef.current = stream;
      setCameraStream(stream);
      // Ended fires when the OS or another app revokes the device, which the user
      // sees as the camera stopping by itself. The UI has to agree with that, and
      // an intentional release above is the one case where it must not.
      stream.getVideoTracks()[0]?.addEventListener("ended", () => {
        if (releasingRef.current) return;
        cameraRef.current = null;
        setCameraStream(null);
      });
    } catch (error) {
      setCameraError(explain("camera", error));
    } finally {
      busyRef.current = false;
    }
  }, [release]);

  const toggleScreen = useCallback(async () => {
    if (busyRef.current) return;
    if (screenRef.current) {
      release("screen");
      setScreenError(null);
      return;
    }
    busyRef.current = true;
    setScreenError(null);
    try {
      if (!navigator.mediaDevices?.getDisplayMedia) {
        throw Object.assign(new Error("unavailable"), { name: "NotFoundError" });
      }
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: { ideal: 15 } },
        audio: false,
      });
      screenRef.current = stream;
      setScreenStream(stream);
      stream.getVideoTracks()[0]?.addEventListener("ended", () => {
        // The browser's own "Stop sharing" bar. Following it is the difference
        // between a control that tracks reality and one that lies.
        if (releasingRef.current) return;
        screenRef.current = null;
        setScreenStream(null);
      });
    } catch (error) {
      setScreenError(explain("screen", error));
    } finally {
      busyRef.current = false;
    }
  }, [release]);

  return { cameraStream, screenStream, cameraError, screenError, toggleCamera, toggleScreen, stopAll };
}
