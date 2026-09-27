import {
  ArrowCounterClockwise,
  Copy,
  Bell,
  ChatCircle,
  Check,
  Clock,
  DotsThree,
  GearSix,
  House,
  Microphone,
  MicrophoneSlash,
  MusicNotes,
  Plus,
  Toolbox,
  PaperPlaneTilt,
  Pulse,
  Sparkle,
  Smiley,
  StackSimple,
  Stop,
  Brain,
  Trash,
  Waveform,
  X,
  Phone,
  VideoCamera,
  VideoCameraSlash,
  MonitorArrowUp,
  SpeakerHigh,
  SpeakerSlash,
  type Icon as PhosphorIcon,
} from "@phosphor-icons/react";
import type { IconName } from "../types";

const iconMap: Record<IconName, PhosphorIcon> = {
  home: House,
  // A face, not an eye. The Avatar Lab is where Lumine's expression is built, so
  // an eye — which is what this was — pointed at looking-at rather than at being.
  avatar: Smiley,
  spark: Sparkle,
  check: Check,
  clock: Clock,
  settings: GearSix,
  mic: Microphone,
  "mic-off": MicrophoneSlash,
  send: PaperPlaneTilt,
  stop: Stop,
  more: DotsThree,
  music: MusicNotes,
  bell: Bell,
  activity: Pulse,
  chat: ChatCircle,
  tools: Toolbox,
  memory: Brain,
  waveform: Waveform,
  trash: Trash,
  plus: Plus,
  copy: Copy,
  reset: ArrowCounterClockwise,
  close: X,
  presentation: StackSimple,
  // The call bar. A handset rather than a square, because the control starts a
  // conversation rather than toggling a state.
  //
  // The end-call mark is the *same* handset, rotated, not `PhoneDisconnect`. A
  // slash drawn through a filled handset produces a heavy blob at 20px — the
  // earpiece, the mouthpiece and the bar all merge into one silhouette — and it
  // reads as a "blocked" sign rather than a hang-up. Dropping the handset 135
  // degrees is the convention every call app uses, and it makes the pair
  // obviously related: same object, two positions.
  phone: Phone,
  "phone-down": Phone,
  video: VideoCamera,
  "video-off": VideoCameraSlash,
  // Arrow up out of a screen, which is the direction a screenshare travels and
  // the direction Tauri asks the OS to allow.
  monitor: MonitorArrowUp,
  speaker: SpeakerHigh,
  "speaker-off": SpeakerSlash,
};

/** Shared Phosphor icon surface; color remains controlled by Lumine theme tokens. */
export function Icon({
  name,
  size = 18,
  weight = "regular",
}: {
  name: IconName;
  size?: number;
  /**
   * Phosphor draws the same glyph in several weights, and the choice is part of
   * the icon rather than a separate asset.
   *
   * The call handsets are `bold` rather than `fill`. A filled handset is a solid
   * silhouette with no interior detail, so at 24px and below the earpiece and
   * mouthpiece blur into a single blob and the shape stops being a handset. A
   * bold outline keeps both ends legible inside a small filled circle, and it
   * matches the toolbar controls beside it, which are outlines too.
   */
  weight?: "thin" | "light" | "regular" | "bold" | "fill" | "duotone";
}) {
  const Component = iconMap[name];
  return <Component size={size} weight={weight} aria-hidden="true" />;
}

export function Mark() {
  return <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>;
}
