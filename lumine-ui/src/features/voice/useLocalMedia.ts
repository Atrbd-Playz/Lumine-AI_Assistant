import { useCallback, useEffect, useRef, useState } from "react";

/**
 * The user's own camera and screen, captured once and published to the room.
 *
 * ## One source, not two
 *
 * LiveKit uses *only the most recently published video track*. Two independently
 * toggleable sources therefore have a state that cannot work: both on, with the
 * camera published first, and Lumine is looking at the screen while the app
 * shows the user their own face with a badge that says "Shared with Lumine".
 * The badge would be true of the track and false of the picture next to it.
 *
 * So there is one `active` source, and turning one on releases the other. What
 * the self-view card shows is then provably what the model is looking at, which
 * is the only property that makes the badge worth having.
 *
 * ## Publishing, not capturing twice
 *
 * LiveKit's own `setScreenShareEnabled` calls `getDisplayMedia` itself, which
 * would mean a second capture of the same screen with a second permission prompt
 * and a second teardown path -- and LiveKit's would be invisible to the release
 * logic below, so the OS indicator could survive the call ending. Instead this
 * hook owns the `MediaStream` and hands the derived `LocalVideoTrack` to the
 * room, so there is exactly one capture, one teardown, and one `ended` handler.
 *
 * That does put a LiveKit type at the edge of this hook. It is the one honest
 * exception, and it is confined to a function whose entire job is "put this
 * track in the room" -- `voice-manager.ts` -- rather than spread across the
 * state machine here.
 *
 * ## The model has to be able to read it
 *
 * Publishing is gated on the active model declaring image input. LiveKit
 * documents that a model which cannot read frames *silently ignores them* -- no
 * error, no warning -- so a camera on beside such a model would light the
 * recording indicator and deliver nothing. The gate is `canPublish`, supplied by
 * the caller from the same catalog field the Diagnostics matrix draws.
 *
 * ## Teardown is the whole job
 *
 * A camera left running is the single most alarming thing a desktop app can do,
 * because the user cannot see it. So the tracks are stopped on four occasions
 * and not on a fifth: turning the control off, the other source taking over,
 * the session ending, and the component unmounting. The screen capture's own
 * `ended` event is handled too, because the browser's "Stop sharing" bar is a
 * button the user will press, and the UI has to follow it rather than keep
 * claiming a share that is over.
 */

export type LocalMediaSource = "camera" | "screen";

/** The one source currently captured, and the stream carrying it. */
export type ActiveMedia = {
  source: LocalMediaSource;
  stream: MediaStream;
};

/**
 * What a toggle actually did.
 *
 * Three cases rather than a stream-or-null, because "turned off" and "the
 * permission prompt was dismissed" are both null and must not be handled the
 * same way: the first means take the track out of the room, the second means
 * leave the current capture alone and let the error field say what happened.
 * Collapsing them would stop a camera the user never asked to stop.
 *
 * `failed` carries the reason *with* the verdict, which is why the third case
 * is a shape rather than a bare marker. The same sentence is also written into
 * `cameraError` / `screenError` for the toast — but React state is not readable
 * in the tick it is set, so a caller that read it back after `await toggle()`
 * would get the *previous* failure and report yesterday's refusal as today's.
 * The relay to the worker takes it from here, where it is known at the moment
 * it happened.
 *
 * `message` is absent when nothing was attempted: a second click while a
 * permission prompt is already up is a no-op, not a failure, and reporting it
 * as one would tell the worker a capture it never saw was refused.
 */
export type MediaToggle =
  | { kind: "started"; media: ActiveMedia }
  | { kind: "stopped" }
  | { kind: "failed"; message?: string; denied?: boolean };

/** Either a capture, or the reason there is not one. Never both, never neither. */
type StartOutcome = { media: ActiveMedia } | { failure: CaptureFailure };

export type LocalMedia = {
  /** The live source, or null. Never both. */
  active: ActiveMedia | null;
  /** Why the camera could not start, phrased for a person rather than a log. */
  cameraError: string | null;
  screenError: string | null;
  /**
   * Whether this machine can capture a screen at all. False on macOS, where
   * WKWebView has no `getDisplayMedia` and Tauri's `navigator.mediaDevices` can
   * be missing outright. The button is disabled with a reason rather than
   * offered and then refused, because "this platform cannot" and "you said no"
   * are different problems with different fixes.
   */
  screenShareSupported: boolean;
  /** Whether this machine can capture a camera at all. */
  cameraSupported: boolean;
  /**
   * Turn a source on, or off if it is already the active one.
   *
   * Resolves to what happened, including the stream that was just captured, so
   * the caller can publish it without reading it back out of a stale closure.
   */
  toggleCamera: () => Promise<MediaToggle>;
  toggleScreen: () => Promise<MediaToggle>;
  stopAll: () => void;
};

/** What went wrong, and whether it was the operating system that said no. */
type CaptureFailure = {
  message: string;
  denied: boolean;
};

/** The browser names these in a way no user has ever been helped by. */
function explain(source: LocalMediaSource, error: unknown): CaptureFailure {
  const name = (error as { name?: string } | null)?.name;
  if (name === "NotAllowedError" || name === "PermissionDeniedError") {
    return {
      denied: true,
      message:
        source === "camera"
          ? "Camera permission was refused. Allow it in your system settings, then try again."
          : "Screen capture was refused.",
    };
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return {
      denied: false,
      message: source === "camera" ? "No camera was found on this machine." : "No screen was available to share.",
    };
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return {
      denied: false,
      message: source === "camera" ? "Another application is using the camera." : "The screen could not be captured.",
    };
  }
  return {
    denied: false,
    message: source === "camera" ? "The camera could not be started." : "The screen could not be shared.",
  };
}

function stopTracks(stream: MediaStream | null) {
  stream?.getTracks().forEach((track) => track.stop());
}

function supportsCamera(): boolean {
  return typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}

function supportsScreenShare(): boolean {
  return typeof navigator !== "undefined" && !!navigator.mediaDevices?.getDisplayMedia;
}

export function useLocalMedia({ enabled }: { enabled: boolean }): LocalMedia {
  const [active, setActive] = useState<ActiveMedia | null>(null);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [screenError, setScreenError] = useState<string | null>(null);
  const activeRef = useRef<ActiveMedia | null>(null);
  // Guards a slow permission prompt: two clicks can start two prompts, and the
  // second one to resolve wins while the first stream is orphaned and still live.
  const busyRef = useRef(false);

  const release = useCallback(() => {
    const current = activeRef.current;
    activeRef.current = null;
    setActive(null);
    stopTracks(current?.stream ?? null);
  }, []);

  const stopAll = useCallback(() => {
    release();
  }, [release]);

  // Ending the call releases the devices. Not deferred to unmount: the call bar
  // stays on screen while disconnected, and a camera that keeps its indicator lit
  // on a page with no call on it is the bug people report first.
  useEffect(() => {
    if (!enabled) stopAll();
  }, [enabled, stopAll]);

  useEffect(() => stopAll, [stopAll]);

  /**
   * A capture ended, and this is how we tell which kind of ending it was.
   *
   * The obvious guard is a flag set around `stop()`, and it does not work:
   * `MediaStreamTrack.stop()` only *queues* the `ended` event, so the flag is
   * already back to false by the time the handler runs, and every intentional
   * release looks exactly like the user pressing the browser's own "Stop
   * sharing" bar. Identity answers it instead -- if this stream is not the
   * active one, something else already replaced or released it, and there is
   * nothing left to update.
   *
   * That is also the only version that gets the *other* case right: the user
   * revoking the device from the system tray fires `ended` on the stream that is
   * still active, and the UI has to agree with it immediately.
   */
  const onCaptureEnded = useCallback((stream: MediaStream) => {
    if (activeRef.current?.stream !== stream) return;
    activeRef.current = null;
    setActive(null);
  }, []);

  /** Take over from whatever is running, so only one track is ever live. */
  const takeOver = useCallback(() => {
    if (activeRef.current) release();
  }, [release]);

  /**
   * Capture one source, or say why it could not be captured.
   *
   * The failure comes back rather than being written into the error state
   * here: state set inside this function is not readable in the same tick, so
   * the caller would read the *previous* failure and report it as this one's.
   * One write site — `toggle` — owns both the toast and the report, from the
   * same sentence.
   */
  const start = useCallback(
    async (source: LocalMediaSource): Promise<StartOutcome> => {
      busyRef.current = true;
      try {
        if (source === "camera") {
          if (!supportsCamera()) {
            return { failure: { denied: false, message: "This machine did not offer a camera to the app." } };
          }
          const stream = await navigator.mediaDevices.getUserMedia({
            video: { width: { ideal: 1280 }, height: { ideal: 720 } },
            audio: false,
          });
          stream.getVideoTracks()[0]?.addEventListener("ended", () => onCaptureEnded(stream));
          takeOver();
          const started: ActiveMedia = { source: "camera", stream };
          activeRef.current = started;
          setActive(started);
          return { media: started };
        }

        if (!supportsScreenShare()) {
          return { failure: { denied: false, message: "This operating system cannot share a screen to an app." } };
        }
        const stream = await navigator.mediaDevices.getDisplayMedia({
          video: { frameRate: { ideal: 15 } },
          audio: false,
        });
        // The browser's own "Stop sharing" bar. Following it is the difference
        // between a control that tracks reality and one that lies.
        stream.getVideoTracks()[0]?.addEventListener("ended", () => onCaptureEnded(stream));
        takeOver();
        const started: ActiveMedia = { source: "screen", stream };
        activeRef.current = started;
        setActive(started);
        return { media: started };
      } catch (error) {
        return { failure: explain(source, error) };
      } finally {
        busyRef.current = false;
      }
    },
    [takeOver, onCaptureEnded],
  );

  const toggle = useCallback(
    async (source: LocalMediaSource): Promise<MediaToggle> => {
      if (busyRef.current) return { kind: "failed" };
      if (activeRef.current?.source === source) {
        release();
        return { kind: "stopped" };
      }
      if (source === "camera") setCameraError(null);
      else setScreenError(null);
      const outcome = await start(source);
      if ("failure" in outcome) {
        if (source === "camera") setCameraError(outcome.failure.message);
        else setScreenError(outcome.failure.message);
        return { kind: "failed", message: outcome.failure.message, denied: outcome.failure.denied };
      }
      return { kind: "started", media: outcome.media };
    },
    [release, start],
  );

  return {
    active,
    cameraError,
    screenError,
    screenShareSupported: supportsScreenShare(),
    cameraSupported: supportsCamera(),
    toggleCamera: () => toggle("camera"),
    toggleScreen: () => toggle("screen"),
    stopAll,
  };
}
