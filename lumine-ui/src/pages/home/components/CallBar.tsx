import { useEffect, useState } from "react";
import { Icon } from "./Icon";
import type { LumineVoiceConnectionState } from "../../../features/voice/useLumineVoice";

/**
 * The call controls, in the shape a call has.
 *
 * ## Why this replaced a dock
 *
 * There was a microphone button and a mute button sitting side by side, which is
 * two controls for one thing in the same state — pressing the big one when already
 * in a call ended the call, and the small one next to it muted it, and the only
 * thing distinguishing them was size. A call has a beginning and an end, and the
 * interface should show that: nothing but the call mark while off, and the four
 * controls that exist during a call once there is one.
 *
 * The end call is last and is the only red thing on screen. A row of four identical
 * circles leaves the destructive one to be found by hovering, and the answer to
 * "which one ends this" should not require a tooltip.
 *
 * ## The timer
 *
 * Only while connected, and counted from the session's own start rather than from
 * when this component mounted, so a reconnect does not reset it to zero. It is the
 * only text in the bar, because a call's length is the one number a person
 * actually wants from it.
 */
export type CallBarProps = {
  status: LumineVoiceConnectionState;
  onConnect: () => void;
  onDisconnect: () => void;
  muted: boolean;
  onMuteToggle: () => void;
  cameraOn: boolean;
  onCameraToggle: () => void;
  screenOn: boolean;
  onScreenToggle: () => void;
  /**
   * Why camera and screen are unavailable, as their accessible names when off.
   *
   * Two separate reasons rather than one, because the two failures have nothing
   * to do with each other and different fixes: the model may be unable to read
   * frames, or the platform may be unable to capture. A single string has to
   * pick one, and whichever it picks is wrong on the other machine -- "choose a
   * realtime model" is nonsense advice to someone on macOS, where no realtime
   * model can capture a screen at all.
   */
  cameraDisabledReason?: string;
  screenDisabledReason?: string;
  /** Epoch ms the current call started, or null while offline. */
  startedAt: number | null;
  /** Why the call cannot start. The call mark's accessible name when off. */
  blockedReason?: string;
};

const LIVE_STATES: LumineVoiceConnectionState[] = [
  "online",
  "connected",
  "listening",
  "thinking",
  "speaking",
];

/** `m:ss`, and then `h:mm:ss` once an hour has passed. */
function elapsed(since: number, now: number): string {
  const seconds = Math.max(0, Math.floor((now - since) / 1000));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function CallBar({
  status,
  onConnect,
  onDisconnect,
  muted,
  onMuteToggle,
  cameraOn,
  onCameraToggle,
  screenOn,
  onScreenToggle,
  cameraDisabledReason,
  screenDisabledReason,
  startedAt,
  blockedReason,
}: CallBarProps) {
  const live = LIVE_STATES.includes(status);
  const connecting = status === "connecting" || status === "initializing" || status === "waiting" || status === "reconnecting";
  const ending = status === "disconnecting" || status === "ending";
  const blocked = Boolean(blockedReason);

  // Ticks only while a call is up. An interval that runs on the home page for no
  // reason is a wake-up a laptop notices and a person does not.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!live || !startedAt) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [live, startedAt]);

  if (!live) {
    return (
      <footer className="call-bar call-bar--idle">
        <button
          type="button"
          className={`call-button ${connecting ? "is-connecting" : ""} ${ending ? "is-ending" : ""} ${blocked ? "is-blocked" : ""}`}
          onClick={onConnect}
          disabled={connecting || ending || blocked}
          aria-label={blocked ? blockedReason : connecting ? "Connecting to Lumine" : ending ? "Ending the call" : "Call Lumine"}
        >
          <span className="call-button-contents" aria-hidden="true">
            {connecting || ending ? <span className="voice-spinner" /> : <Icon name="phone" size={25} weight="bold" />}
          </span>
        </button>
      </footer>
    );
  }

  return (
    <footer className="call-bar call-bar--live">
      <span className="call-timer" aria-label="Call length">
        {startedAt ? elapsed(startedAt, now) : "0:00"}
      </span>
      <div className="call-controls">
        <button
          type="button"
          className={`call-control ${muted ? "is-off" : ""}`}
          onClick={onMuteToggle}
          aria-pressed={muted}
          aria-label={muted ? "Unmute microphone" : "Mute microphone"}
        >
          <Icon name={muted ? "mic-off" : "mic"} size={19} />
        </button>
        <button
          type="button"
          className={`call-control ${cameraOn ? "is-off" : ""}`}
          onClick={onCameraToggle}
          disabled={Boolean(cameraDisabledReason) && !cameraOn}
          aria-pressed={cameraOn}
          aria-label={cameraDisabledReason && !cameraOn ? cameraDisabledReason : cameraOn ? "Turn camera off" : "Turn camera on"}
        >
          <Icon name={cameraOn ? "video" : "video-off"} size={19} />
        </button>
        <button
          type="button"
          className={`call-control ${screenOn ? "is-off" : ""}`}
          onClick={onScreenToggle}
          disabled={Boolean(screenDisabledReason) && !screenOn}
          aria-pressed={screenOn}
          aria-label={screenDisabledReason && !screenOn ? screenDisabledReason : screenOn ? "Stop sharing the screen" : "Share the screen"}
        >
          <Icon name="monitor" size={19} />
        </button>
        <button
          type="button"
          className="call-control call-control--end"
          onClick={onDisconnect}
          aria-label="End call"
        >
          {/* The call button's waveform, barred. The two are the same glyph rather
              than two that happen to look related, and the bar is what makes this
              one the destructive control — which the red circle behind it repeats. */}
          <Icon name="phone-down" size={20} weight="bold" />
        </button>
      </div>
    </footer>
  );
}
