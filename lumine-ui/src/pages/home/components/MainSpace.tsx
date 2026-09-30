import { useEffect, useState } from "react";
import { STATE_COPY } from "../constants";
import { formatClock } from "../utils";
import type { LumineState } from "../types";
import { Icon } from "./Icon";
import { Presence } from "./Presence";
import { CallBar } from "./CallBar";
import { SelfView } from "./SelfView";
import { ConversationPanel, type PanelKind } from "./ConversationPanel";
import { ConversationToggle, NotesToggle } from "./ConversationToggle";
import { NotesPanel } from "./NotesPanel";
import { WidgetStrip } from "../widgets/WidgetStrip";
import type { NotesApi } from "../notes/useNotes";
import type { ConversationAgentStatus, ConversationItem } from "../conversation/types";
import type { ConversationBubbleVariant } from "../types";
import type { LumineVoiceConnectionState } from "../../../features/voice/useLumineVoice";
import type { LocalMedia } from "../../../features/voice/useLocalMedia";
import type { LumineEmotionIntent } from "../../../components/avatar/avatarTypes";

type MainSpaceProps = {
  state: LumineState;
  cursorGaze: boolean;
  showAvatarColor: boolean;
  sessionStatus: LumineVoiceConnectionState;
  onConnect: () => void;
  onDisconnect: () => void;
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
  /** The setup gate, when the install cannot yet hold a conversation. */
  gate: React.ReactNode;
  voiceDisabledReason?: string;
  canReceiveVideo: boolean;

  /** The transcript. A right-hand aside of the stage again. */
  conversationItems: readonly ConversationItem[];
  conversationStatus: ConversationAgentStatus;
  bubbleVariant: ConversationBubbleVariant;
  /** Real text delivery to the room. Replaces the composer's dead end. */
  canSendText: boolean;
  sendTextHint: string;
  onSendText: (text: string) => boolean;
  onClearConversation: () => void;
  onResetConversation: () => void;
  /**
   * The presentation switch, back in the topbar where a presentation control
   * belongs. It was moved to the rail's foot as a "read the same from every
   * destination" argument, which is true and beside the point: a control that
   * changes *this* page's glass, sitting somewhere that is not this page, is a
   * control nobody connects to its own effect.
   */
  glassMode: boolean;
  onGlassModeToggle: () => void;
  /**
   * Opens the command palette, and the chord to press instead.
   *
   * Two props rather than one because the label has to be computed where the
   * platform is known — `describeBinding` resolves `mod` to the machine's own
   * modifier — and a component that drew "⌘K" as a literal would be right on
   * macOS and actively misleading everywhere else.
   */
  onOpenPalette: () => void;
  paletteHint: string;
  /**
   * The audio level subscription, forwarded to the avatar engine.
   *
   * A subscription and not a value: an RMS updates about sixty times a second,
   * and `energy={level}` in state would re-render this whole subtree at that
   * rate to move two SVG attributes. The engine writes it straight to its
   * transform instead, on the frame the browser is already painting.
   */
  subscribeEnergy?: (onLevel: (level: number) => void) => () => void;
  /**
   * Which right-hand aside occupies the slot, and who changes it.
   *
   * Controlled by `Home.tsx` because four things open one of them and none of
   * them is the panel: the two topbar switches, the command palette's "Show the
   * transcript", and `mod+shift+enter`. It is one value rather than two booleans
   * because there is one slot — the tracks are exclusive, so two open asides
   * would overlap and the reader would have to close one to reach the other.
   */
  panel: PanelKind;
  onPanelChange: (panel: PanelKind) => void;

  /** The notes, lifted so the band's preview and the aside never disagree. */
  notes: NotesApi;
  /** The note the aside is editing, and the one the band is previewing. */
  activeNoteId: string | null;
  onSelectNote: (id: string) => void;
  onCreateNote: () => void;
  onEditNote: (id: string, body: string) => void;
  onDeleteNote: (id: string) => void;
  /** Where the aside's "open the timer" button goes. */
  onFocusPage: () => void;
};

const LIVE_STATES: LumineVoiceConnectionState[] = ["online", "connected", "listening", "thinking", "speaking"];

/** Main command surface: presence, the call, and the transcript. */
export function MainSpace({
  state,
  cursorGaze,
  showAvatarColor,
  sessionStatus,
  onConnect,
  onDisconnect,
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
  conversationItems,
  conversationStatus,
  bubbleVariant,
  canSendText,
  sendTextHint,
  onSendText,
  onClearConversation,
  onResetConversation,
  glassMode,
  onGlassModeToggle,
  onOpenPalette,
  paletteHint,
  subscribeEnergy,
  panel,
  onPanelChange,
  notes,
  activeNoteId,
  onSelectNote,
  onCreateNote,
  onEditNote,
  onDeleteNote,
  onFocusPage,
}: MainSpaceProps) {
  const copy = STATE_COPY[state];
  const today = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "2-digit", month: "long" }).format(new Date());
  const live = LIVE_STATES.includes(sessionStatus);
  // One source, because the room only ever uses one. `media.active` is already
  // exclusive, so the preview cannot be showing a different track from the one
  // Lumine is looking at.
  const preview = media.active;

  // Only meaningful during a call: the media controls are not drawn otherwise,
  // and a reason attached to a button that is not there is a reason nobody reads.
  // The model is checked first, because a model that cannot read frames makes
  // the platform question irrelevant -- there is nowhere for the picture to go
  // even on a machine that could capture one.
  const MODEL_CANNOT_SEE =
    "The selected model takes text and audio, so there is nowhere for a picture to go. Choose a realtime model to use a camera.";
  const PLATFORM_CANNOT_CAPTURE = "This operating system did not offer a camera to the app.";
  const PLATFORM_CANNOT_SHARE = "This operating system cannot share a screen to an app.";
  const cameraReason = live ? (!canReceiveVideo ? MODEL_CANNOT_SEE : media.cameraSupported ? undefined : PLATFORM_CANNOT_CAPTURE) : undefined;
  const screenReason = live ? (!canReceiveVideo ? MODEL_CANNOT_SEE : media.screenShareSupported ? undefined : PLATFORM_CANNOT_SHARE) : undefined;

  // The one place the call's length is computed. The stage's corner card shows it
  // while the transcript is shut. One `useNow` for the whole page means the two
  // can never both be mounted and disagree, and the interval exists only during a
  // live call.
  const now = useNow(startedAt);
  const callLength = live && startedAt !== null ? <CallLength startedAt={startedAt} now={now} /> : null;

  /** One aside or none: opening one closes the other. */
  const toggle = (kind: Exclude<PanelKind, null>) => onPanelChange(panel === kind ? null : kind);

  return (
    <main className={`main-space ${gate ? "is-gated" : ""}${panel ? " is-panel-open" : ""}`}>
      {/*
        The stage, and it is a band rather than the whole window.

        It used to claim every row that was left, which is the layout an avatar
        wants and a home screen does not: the everyday things then had nowhere to
        live except a column down one side, competing with her for the width. Now
        the stage is a section across the top — full width while the slot beside it
        is shut, narrower while an aside is open — and everything else sits under
        it, the way a shop's offer band sits across the page and the rest of the
        page continues below.
      */}
      <section className="command-stage col-start-1 row-start-1">
        <header className="topbar">
          <div className="topbar-identity">
            <p className="eyebrow">{today}</p>
            <h1>Command space</h1>
          </div>
          {/*
            The palette affordance, and the reason it exists.

            `mod+k` resolves to Ctrl+K on Windows and Linux, and nothing in the UI
            ever said so — so a shortcut nobody could see read as a broken one, and
            the chord people reach for on Windows (Win+K) is taken by the operating
            system before any webview sees it. Showing the *actual* chord removes
            that whole class of confusion: the key is named where the shortcut is
            offered.
          */}
          <div className="topbar-actions flex items-center gap-1.5">
            <button
              type="button"
              className="palette-hint"
              onClick={onOpenPalette}
              aria-label="Search commands"
              title="Search commands"
            >
              <Icon name="search" size={15} />
              <span>Search</span>
              <kbd>{paletteHint}</kbd>
            </button>
            <button
              type="button"
              className={`visual-mode-toggle ${glassMode ? "is-active" : ""}`}
              onClick={onGlassModeToggle}
              aria-pressed={glassMode}
              aria-label={glassMode ? "Switch to classic presentation" : "Switch to glass presentation"}
              title={glassMode ? "Classic presentation" : "Glass presentation"}
            >
              <Icon name="presentation" size={17} />
            </button>
            <ConversationToggle open={panel === "conversation"} onClick={() => toggle("conversation")} />
            <NotesToggle open={panel === "notes"} onClick={() => toggle("notes")} />
          </div>
        </header>

        {/*
          The stage's corner instruments: the local time, and the call's length.

          The left one replaces the turn counter, which was a number about a
          conversation that may not have happened yet sitting in the largest type
          region on the screen. Time is the fact a corner card is *for* — always
          true, read without deciding to read it, and small enough not to compete
          with the greeting beside it.
        */}
        <StageClock />
        {callLength}

        <div className="stage-presence relative z-[1] grid place-items-center self-center min-h-0">
          <Presence
            state={state}
            cursorGaze={cursorGaze}
            showAvatarColor={showAvatarColor}
            emotion={emotion}
            showEmotionDebug={showEmotionDebug}
            subscribeEnergy={subscribeEnergy}
          />
          {preview && <SelfView stream={preview.stream} source={preview.source} isPublished={isVideoPublished} />}
        </div>

        {/*
          The greeting, and only the greeting.

          A second line under it restated the same state the headline already
          carries — "I'm listening. / Speak naturally — I'll keep the context." —
          and the screen had four other places doing the same job. One line with a
          claim in it beats two lines where the second is the first explained.
        */}
        <div className="stage-copy" key={state}>
          <h2>{copy.title}</h2>
        </div>

        {gate && <div className="stage-gate">{gate}</div>}
      </section>

      {/*
        The everyday band: weather, the front page, the notes, music.

        Under the stage rather than beside it, so the stage keeps a full-width
        composition and the band is where the eye goes afterwards. It takes the
        height the stage was never going to use — which is the whole difference
        between a section and a background.
      */}
      <WidgetStrip
        notes={notes}
        onOpenNotes={() => onPanelChange("notes")}
        onWriteNote={() => {
          onCreateNote();
          onPanelChange("notes");
        }}
      />

      {/*
        The call, in its own strip and centred.

        It used to live inside the transcript's header, which meant the controls
        were squeezed into whatever the status text and the chevron left over —
        right-aligned, and legible only because the status string happened to be
        short. A control that important is not what goes in a flex row's remainder.
      */}
      <div className="call-strip">
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
          blockedReason={voiceDisabledReason}
        />
      </div>

      <ConversationPanel
        items={conversationItems}
        agentStatus={conversationStatus}
        bubbleVariant={bubbleVariant}
        canSubmit={canSendText}
        submitHint={sendTextHint}
        onSubmit={onSendText}
        onClear={onClearConversation}
        onReset={onResetConversation}
        open={panel === "conversation"}
        onOpenChange={(open) => onPanelChange(open ? "conversation" : null)}
      />

      <NotesPanel
        open={panel === "notes"}
        onOpenChange={(open) => onPanelChange(open ? "notes" : null)}
        notes={notes}
        activeId={activeNoteId}
        onSelect={onSelectNote}
        onCreate={onCreateNote}
        onSave={onEditNote}
        onDelete={onDeleteNote}
        onFocusPage={onFocusPage}
      />
    </main>
  );
}

/**
 * The local time, in the stage's lower-left corner.
 *
 * Its own component because it ticks: a `setInterval` in `MainSpace` would
 * re-render the avatar, the transcript's rows and the call strip once a second to
 * move four glyphs, and the avatar is the one part of this page that is already
 * animating on every frame it has.
 *
 * The label is the zone's own city rather than "Local time", because the eyebrow
 * above already says the date and a corner card that repeats a label the reader
 * can see is a corner card being read for nothing.
 */
function StageClock() {
  const now = useClock();
  const date = new Date(now);
  const time = formatClock(date);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone ?? "";
  const city = zone.split("/").pop()?.replace(/_/g, " ") ?? "";
  return (
    <div className="stage-context context-left">
      <span>{city || "Local time"}</span>
      <strong className="stage-clock-figure">{time}</strong>
    </div>
  );
}

/**
 * `m:ss`, and `h:mm:ss` once an hour has passed.
 *
 * The right-hand card is omitted while the transcript is open, because the dock's
 * header then shows the same duration. Two identical clocks a few hundred pixels
 * apart read as a rendering bug even though both are correct.
 */
function CallLength({ startedAt, now }: { startedAt: number; now: number }) {
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return (
    <div className="stage-context context-right">
      <span>In call</span>
      <strong>{h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`}</strong>
    </div>
  );
}

/**
 * Ticks once a second, and only while a call is up.
 *
 * An interval that runs on the home page with no call in progress is a wake-up a
 * laptop notices and a person does not. `CallBar` had this same rule for the same
 * reason; it is written once here now, because the two timers were going to be
 * identical and identical code written twice is one bug waiting for the next edit
 * to only make in one place.
 */
function useNow(startedAt: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);
  return now;
}

/** The corner clock's own tick, always running. */
function useClock(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}
