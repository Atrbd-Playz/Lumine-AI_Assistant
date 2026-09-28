import { STATE_COPY } from "../constants";
import type { LumineState } from "../types";
import { Icon } from "./Icon";
import { Presence } from "./Presence";
import { ConversationToggle } from "./ConversationToggle";
import { CallBar } from "./CallBar";
import { SelfView } from "./SelfView";
import type { LumineVoiceConnectionState } from "../../../features/voice/useLumineVoice";
import type { LocalMedia } from "../../../features/voice/useLocalMedia";
import type { LumineEmotionIntent } from "../../../components/avatar/avatarTypes";

type MainSpaceProps = {
  state: LumineState;
  cursorGaze: boolean;
  showAvatarColor: boolean;
  conversationOpen: boolean;
  onConversationToggle: () => void;
  sessionStatus: LumineVoiceConnectionState;
  onConnect: () => void;
  onDisconnect: () => void;
  glassMode: boolean;
  onGlassModeToggle: () => void;
  muted: boolean;
  onMuteToggle: () => void;
  /** The local camera and screen. Owned by the voice layer, drawn here. */
  media: LocalMedia;
  /**
   * Whether the track is in the room, not merely in the preview.
   *
   * Separate from `canReceiveVideo` because that is a property of the *model* and
   * this is a fact about the *track*, and the two disagree in the case that
   * matters most: a model that cannot see leaves the preview honest about being
   * local, while a model that can but whose publish failed must not claim to be
   * shared. The self-view badge is driven by this, not by the other.
   */
  isVideoPublished: boolean;
  /** Turn a source on or off. Publishes when the model is able to read it. */
  onMediaToggle: (source: "camera" | "screen") => void;
  /** Epoch ms the current call began, for the timer. */
  startedAt: number | null;
  emotion: LumineEmotionIntent | null;
  showEmotionDebug: boolean;
  /**
   * What replaces the quick-intent row, when something has to come first.
   *
   * The stage and the avatar stay. A first run that hides Lumine while asking for
   * a key reads as an app that is broken, and the person who has not set Lumine up
   * yet is exactly the person who should already be able to see it.
   */
  gate?: React.ReactNode;
  /** Why the call button is off. Its accessible name while off. */
  voiceDisabledReason?: string;
  /**
   * Whether the selected model could consume frames at all.
   *
   * A pipeline's language stage is handed a chat history of strings and has
   * nowhere to put a picture, so a camera beside one is a control that cannot do
   * what its icon says. Read from the same `inputModalities` the Diagnostics
   * matrix draws, so the two cannot disagree.
   */
  canReceiveVideo: boolean;
};

const LIVE_STATES: LumineVoiceConnectionState[] = ["online", "connected", "listening", "thinking", "speaking"];

/** Main command surface: status, presence, quick intents, and the call bar. */
export function MainSpace({
  state,
  cursorGaze,
  showAvatarColor,
  conversationOpen,
  onConversationToggle,
  sessionStatus,
  onConnect,
  onDisconnect,
  glassMode,
  onGlassModeToggle,
  muted,
  onMuteToggle,
  media,
  isVideoPublished,
  onMediaToggle,
  startedAt,
  emotion,
  showEmotionDebug,
  gate,
  voiceDisabledReason,
  canReceiveVideo,
}: MainSpaceProps) {
  const copy = STATE_COPY[state];
  const today = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "2-digit", month: "long" }).format(new Date());
  const live = LIVE_STATES.includes(sessionStatus);
  // One source, because the room only ever uses one. `media.active` is already
  // exclusive, so the card cannot be showing a different track from the one
  // Lumine is looking at.
  const preview = media.active;

  // Only meaningful during a call: the media controls are not drawn otherwise,
  // and a reason attached to a button that is not there is a reason nobody reads.
  // The model is checked first, because a model that cannot read frames makes
  // the platform question irrelevant -- there is nowhere for the picture to go
  // even on a machine that could capture one.
  const MODEL_CANNOT_SEE =
    "The selected model takes text and audio, so there is nowhere for a picture to go. Choose a realtime model to use a camera.";
  const PLATFORM_CANNOT_CAPTURE =
    "This operating system did not offer a camera to the app.";
  const PLATFORM_CANNOT_SHARE =
    "This operating system cannot share a screen to an app.";
  const cameraReason = live ? (!canReceiveVideo ? MODEL_CANNOT_SEE : media.cameraSupported ? undefined : PLATFORM_CANNOT_CAPTURE) : undefined;
  const screenReason = live ? (!canReceiveVideo ? MODEL_CANNOT_SEE : media.screenShareSupported ? undefined : PLATFORM_CANNOT_SHARE) : undefined;

  return <main className={`main-space ${gate ? "is-gated" : ""}`}>
    <header className="topbar"><div><p className="eyebrow">{today}</p><h1>Command space</h1></div><div className="topbar-actions"><button className={`visual-mode-toggle ${glassMode ? "is-active" : ""}`} onClick={onGlassModeToggle} aria-pressed={glassMode} aria-label={glassMode ? "Switch to classic presentation" : "Switch to glass presentation"} title={glassMode ? "Classic presentation" : "Glass presentation"}><Icon name="presentation" size={17} /></button><ConversationToggle open={conversationOpen} onClick={onConversationToggle} /></div></header>
    <section className="command-stage">
      <div className="stage-kicker"><span className="signal-dot" />{copy.eyebrow}</div>
      <Presence state={state} cursorGaze={cursorGaze} showAvatarColor={showAvatarColor} emotion={emotion} showEmotionDebug={showEmotionDebug} />
      {preview && <SelfView stream={preview.stream} source={preview.source} isPublished={isVideoPublished} />}
      <div className="stage-copy" key={state}><h2>{copy.title}</h2><p>{copy.detail}</p></div>
      <div className="stage-context context-left"><span>Today</span><strong>03</strong><small>tasks in focus</small></div>
      <div className="stage-context context-right"><span>Next reminder</span><strong>09:00</strong><small>Physics notes</small></div>
      {/* <div className="stage-line" /> */}
    </section>
    {gate ? <section className="command-bottom is-gated">{gate}</section> : <section className="command-bottom"><div className="feature-surface"><div><p className="eyebrow">Now playing</p><h3>Ambient focus</h3><p className="subtle">Soft light for the next hour</p></div><button className="text-button"><Icon name="music" size={15} />Adjust</button></div><div className="quick-actions"><p className="eyebrow">Quick intent</p><div><span className="subtle">Voice session controls are ready below.</span></div></div></section>}
    {media.cameraError && <p className="call-error" role="status">{media.cameraError}</p>}
    {media.screenError && <p className="call-error" role="status">{media.screenError}</p>}
    <CallBar
      status={sessionStatus}
      onConnect={onConnect}
      onDisconnect={onDisconnect}
      muted={muted}
      onMuteToggle={onMuteToggle}
      cameraOn={media.active?.source === "camera"}
      onCameraToggle={() => onMediaToggle("camera")}
      screenOn={media.active?.source === "screen"}
      onScreenToggle={() => onMediaToggle("screen")}
      cameraDisabledReason={cameraReason}
      screenDisabledReason={screenReason}
      startedAt={startedAt}
      blockedReason={voiceDisabledReason}
    />
  </main>;
}
