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
 * ## Where the vertical space went
 *
 * This used to be a `126px` grid row of its own, with a `102px` feature row above
 * it holding two hardcoded cards. That is a quarter of an 800px window spent on a
 * call button and two pieces of invented copy. The call controls are now one row
 * inside `SessionDock`'s header — a 64px strip, collapsed or not, call or no call.
 *
 * ## The timer moved out
 *
 * It used to be the only text in this bar, which is why it is the obvious thing to
 * want back. It now belongs to `SessionDock`'s header, next to the controls and the
 * transcript they belong to, and `MainSpace` owns the single interval. A call's
 * length is a fact about the *call*, and it was sitting in a component that also
 * renders the idle "call Lumine" button — a component with two unrelated jobs.
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
   * Why camera and screen are unavailable, carried in the control's accessible
   * name and its tooltip when off.
   *
   * Two separate reasons rather than one, because the two failures have nothing
   * to do with each other and different fixes: the model may be unable to read
   * frames, or the platform may be unable to capture. A single string has to
   * pick one, and whichever it picks is wrong on the other machine -- "choose a
   * realtime model" is nonsense advice to someone on macOS, where no realtime
   * model can capture a screen at all.
   *
   * The reason is prefixed with what the control is ("Camera — …"), not used as
   * the whole name. Replacing the name outright meant the button announced only
   * a sentence about a model, and a sentence with no control in it is not a
   * button's name.
   */
  cameraDisabledReason?: string;
  screenDisabledReason?: string;
  /**
   * Why the call cannot start. The call mark's accessible name when off.
   *
   * There is deliberately no `startedAt` prop any more. It used to drive this
   * component's own timer, and the timer moved to `SessionDock`'s header — so the
   * prop would now be read by nothing here while a second copy of the duration
   * logic sat in the file. A prop that survives its only use is how a component
   * ends up rendering a stale value nobody can find the source of.
   */
  blockedReason?: string;
};

const LIVE_STATES: LumineVoiceConnectionState[] = [
  "online",
  "connected",
  "listening",
  "thinking",
  "speaking",
];



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
  blockedReason,
}: CallBarProps) {
  const live = LIVE_STATES.includes(status);
  const connecting = status === "connecting" || status === "initializing" || status === "waiting" || status === "reconnecting";
  const ending = status === "disconnecting" || status === "ending";
  const blocked = Boolean(blockedReason);

  if (!live) {
    /*
      `aria-disabled`, not `disabled`.

      A natively disabled button is removed from the tab order, and every reason
      this component carries lives in the accessible name. So the people the copy
      was written for — someone who cannot see that the mark went grey, and needs
      to be told the setup is incomplete — are exactly the ones who can never land
      on it. Withholding the handler instead of the element keeps the control
      focusable, so the reason is read on focus, and it keeps `:hover` and `title`
      working for everyone else. The styling already keys off `is-blocked` and
      `is-connecting`, so nothing about the appearance moves.
    */
    const reason = blocked && blockedReason ? blockedReason : undefined;
    const unavailable = connecting || ending || blocked;
    const label = reason
      ? `Call Lumine — ${reason}`
      : connecting
        ? "Connecting to Lumine"
        : ending
          ? "Ending the call"
          : "Call Lumine";

    return (
      <footer className="call-bar call-bar--idle">
        <button
          type="button"
          className={`call-button ${connecting ? "is-connecting" : ""} ${ending ? "is-ending" : ""} ${blocked ? "is-blocked" : ""}`}
          onClick={unavailable ? undefined : onConnect}
          aria-disabled={unavailable || undefined}
          aria-label={label}
          title={reason}
        >
          <span className="call-button-contents grid place-items-center w-full h-full" aria-hidden="true">
            {connecting || ending ? <span className="voice-spinner" /> : <Icon name="phone" size={25} weight="bold" />}
          </span>
        </button>
      </footer>
    );
  }

  /*
    The same reachability rule as the idle mark above, for the same reason: both
    reasons here are long, specific sentences about a model or an operating
    system, and a sentence no screen reader can focus is a sentence nobody is
    told. Each flag is what `disabled` used to be — the element stays tabbable
    and the handler is simply not attached while it is set.
  */
  const cameraBlocked = Boolean(cameraDisabledReason) && !cameraOn;
  const screenBlocked = Boolean(screenDisabledReason) && !screenOn;

  return (
    <footer className="call-bar call-bar--live">
      {/*
        The timer used to be here, and it is in the dock's header now.

        Two identical clocks about four hundred pixels apart reads as a rendering
        bug, and it was going to happen the moment the dock grew a duration of its
        own. One owner for the number: `MainSpace` computes it once and hands it
        to whichever of the two places is currently visible. `startedAt` is still
        a prop because it decides *whether* there is a call to time.
      */}
      <div className="call-controls flex items-center gap-3">
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
          onClick={cameraBlocked ? undefined : onCameraToggle}
          aria-disabled={cameraBlocked || undefined}
          aria-pressed={cameraOn}
          aria-label={
            cameraBlocked && cameraDisabledReason
              ? `Camera — ${cameraDisabledReason}`
              : cameraOn
                ? "Turn camera off"
                : "Turn camera on"
          }
          title={cameraBlocked ? cameraDisabledReason : undefined}
        >
          <Icon name={cameraOn ? "video" : "video-off"} size={19} />
        </button>
        <button
          type="button"
          className={`call-control ${screenOn ? "is-off" : ""}`}
          onClick={screenBlocked ? undefined : onScreenToggle}
          aria-disabled={screenBlocked || undefined}
          aria-pressed={screenOn}
          aria-label={
            screenBlocked && screenDisabledReason
              ? `Screen share — ${screenDisabledReason}`
              : screenOn
                ? "Stop sharing the screen"
                : "Share the screen"
          }
          title={screenBlocked ? screenDisabledReason : undefined}
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
