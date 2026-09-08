import type { Appearance, Entry, LumineState } from "./types";

/** The user-editable palette used by the appearance dialog. */
export const DEFAULT_APPEARANCE: Appearance = {
  accent: "#e86f3d",
  icon: "#aaa39b",
  canvas: "#f4f3f0",
  surface: "#fbfaf8",
  stage: "#252525",
  text: "#252525",
  avatar: "#df6232",
  showAvatarColor: true,
  font: "Ubuntu",
  chatFont: "Ubuntu",
  chatSurface: "#211f1d",
  chatUser: "#2c2926",
  chatAssistant: "#4a2a1f",
  chatText: "#f1ece5",
  chatAccent: "#e86f3d",
  chatBubbleVariant: "tinted",
};

export const STATE_COPY: Record<LumineState, { eyebrow: string; title: string; detail: string }> = {
  idle: { eyebrow: "Lumine · online", title: "Good evening, Master.", detail: "Everything is quiet. What should we work on?" },
  listening: { eyebrow: "Voice channel open", title: "I’m listening.", detail: "Speak naturally — I’ll keep the context." },
  thinking: { eyebrow: "Lumine is processing", title: "Working on it.", detail: "Pulling together the next best step." },
  speaking: { eyebrow: "Lumine is speaking", title: "Here’s what I found.", detail: "I’m ready for your next instruction." },
};

export const INITIAL_ENTRIES: Entry[] = [
  { id: 1, kind: "action", content: "Reminder created · Physics notes, 9:00 PM", time: "8:42 PM" },
  { id: 2, kind: "message", content: "Play something calm while I study.", time: "8:43 PM" },
  { id: 3, kind: "note", content: "Ambient focus music is playing", time: "Now" },
];

export const NAV_ITEMS = [
  ["home", "home"],
  ["presence", "spark"],
  ["tasks", "check"],
  ["history", "clock"],
] as const;
