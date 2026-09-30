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
  WaveformSlash,
  VideoCamera,
  VideoCameraSlash,
  MonitorArrowUp,
  SpeakerHigh,
  SpeakerSlash,
  Info,
  MagnifyingGlass,
  Sun,
  Cloud,
  CloudRain,
  CloudSnow,
  Lightning,
  Newspaper,
  NotePencil,
  Timer,
  List,
  CaretLeft,
  CaretRight,
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
  info: Info,
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
  // The command palette's affordance in the topbar. The chord is shown next to it
  // because `mod+k` is Ctrl+K on Windows and Linux and nothing else in the app
  // ever said so.
  search: MagnifyingGlass,
  // The call bar. A waveform rather than a handset, because the control starts a
  // voice conversation and this is the app's own mark for one — the same glyph the
  // transcript and the activity page use. Reaching for a handset put a second
  // visual language on the only control that begins a call.
  //
  // The end-call mark is the *same* waveform with a bar through it, not a second
  // unrelated symbol. Two details make that work where the handset version did
  // not: a waveform is drawn as thin strokes, so a bar across it stays a bar
  // instead of merging into a filled blob at 20px — which is exactly why the
  // handset was never `PhoneDisconnect` — and "the voice, stopped" is legible
  // without the pair having to be related by rotation. Nothing is rotated here,
  // so nothing depends on the two glyphs sharing a bounding box.
  phone: Waveform,
  "phone-down": WaveformSlash,
  video: VideoCamera,
  "video-off": VideoCameraSlash,
  // Arrow up out of a screen, which is the direction a screenshare travels and
  // the direction Tauri asks the OS to allow.
  monitor: MonitorArrowUp,
  speaker: SpeakerHigh,
  "speaker-off": SpeakerSlash,
  // The everyday rail. Weather is drawn from a small vocabulary of its own rather
  // than one thermometer glyph, because a card whose whole job is "what is it like
  // out" has to answer that before it is read — the label under it is the
  // confirmation, not the answer.
  sun: Sun,
  cloud: Cloud,
  rain: CloudRain,
  snow: CloudSnow,
  storm: Lightning,
  news: Newspaper,
  note: NotePencil,
  timer: Timer,
  // The rail's own controls rather than destinations: a list glyph for the small
  // screen's overflow menu, and a caret for folding the rail down to icons.
  menu: List,
  collapse: CaretLeft,
  expand: CaretRight,
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
   * The call waveforms are `bold` rather than `fill`. A filled waveform is a
   * solid block of vertical bars with no gaps left between them, so at 24px and
   * below the bars merge and the shape stops reading as audio at all. A bold
   * outline keeps the gaps — which *are* the waveform — legible inside a small
   * filled circle, and it matches the toolbar controls beside it, which are
   * outlines too.
   */
  weight?: "thin" | "light" | "regular" | "bold" | "fill" | "duotone";
}) {
  const Component = iconMap[name];
  return <Component size={size} weight={weight} aria-hidden="true" />;
}

export function Mark() {
  return <span className="brand-mark inline-flex items-center gap-0.5 h-[15px]" aria-hidden="true"><i /><i /><i /></span>;
}
