import {
  ArrowCounterClockwise,
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
  Toolbox,
  PaperPlaneTilt,
  Pulse,
  Sparkle,
  StackSimple,
  Stop,
  Brain,
  Trash,
  Waveform,
  X,
  type Icon as PhosphorIcon,
} from "@phosphor-icons/react";
import type { IconName } from "../types";

const iconMap: Record<IconName, PhosphorIcon> = {
  home: House,
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
  reset: ArrowCounterClockwise,
  close: X,
  presentation: StackSimple,
};

/** Shared Phosphor icon surface; color remains controlled by Lumine theme tokens. */
export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const Component = iconMap[name];
  return <Component size={size} weight="regular" aria-hidden="true" />;
}

export function Mark() {
  return <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>;
}
