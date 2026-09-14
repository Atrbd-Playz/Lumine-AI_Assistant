import type { Appearance, AppearancePreset, Entry, LumineState, ThemePalettes } from "./types";

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

export const DEFAULT_LIGHT_PALETTE: AppearancePreset = {
  accent: "#c85c31",
  icon: "#6f6a63",
  canvas: "#f4f3f0",
  surface: "#fbfaf8",
  stage: "#252525",
  text: "#252525",
  avatar: "#df6232",
  chatSurface: "#fbfaf8",
  chatUser: "#e8e7e3",
  chatAssistant: "#f7d8ca",
  chatText: "#252525",
  chatAccent: "#c85c31",
};

export const DEFAULT_DARK_PALETTE: AppearancePreset = {
  accent: "#e86f3d",
  icon: "#aaa39b",
  canvas: "#181715",
  surface: "#211f1d",
  stage: "#11100f",
  text: "#f1ece5",
  avatar: "#df6232",
  chatSurface: "#211f1d",
  chatUser: "#2c2926",
  chatAssistant: "#4a2a1f",
  chatText: "#f1ece5",
  chatAccent: "#e86f3d",
};

export const DEFAULT_THEME_PALETTES: ThemePalettes = {
  light: DEFAULT_LIGHT_PALETTE,
  dark: DEFAULT_DARK_PALETTE,
};

export const DEFAULT_THEME_PRESETS: Record<keyof ThemePalettes, Record<string, AppearancePreset>> = {
  light: {
    "Anime Sakura": { ...DEFAULT_LIGHT_PALETTE, accent: "#d95f8c", canvas: "#fff5f7", surface: "#fffafd", stage: "#fff0f4", text: "#3a2630", icon: "#8d6472", avatar: "#f39ab5", chatSurface: "#fffafd", chatUser: "#ffe3ec", chatAssistant: "#f7c9d8", chatText: "#3a2630", chatAccent: "#d95f8c" },
    "Anime Sky": { ...DEFAULT_LIGHT_PALETTE, accent: "#4e91c8", canvas: "#eef8ff", surface: "#f9fdff", stage: "#e7f3fb", text: "#203444", icon: "#668399", avatar: "#82c7e8", chatSurface: "#f9fdff", chatUser: "#d9effb", chatAssistant: "#c5e4f3", chatText: "#203444", chatAccent: "#4e91c8" },
  },
  dark: {
    "Anime Night": { ...DEFAULT_DARK_PALETTE, accent: "#ff78ab", canvas: "#1d1720", surface: "#28202b", stage: "#140f18", text: "#ffeaf2", icon: "#c8a0b2", avatar: "#ec5e98", chatSurface: "#28202b", chatUser: "#382735", chatAssistant: "#51283e", chatText: "#ffeaf2", chatAccent: "#ff78ab" },
    "Anime Neon": { ...DEFAULT_DARK_PALETTE, accent: "#65d8d0", canvas: "#101d23", surface: "#172a30", stage: "#0b1418", text: "#e2fffc", icon: "#8db7b7", avatar: "#45c9c4", chatSurface: "#172a30", chatUser: "#203b40", chatAssistant: "#184b52", chatText: "#e2fffc", chatAccent: "#65d8d0" },
  },
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
  ["home", "home", "Home"],
  ["conversation", "chat", "Conversation"],
  ["tools", "tools", "Tools"],
  ["memory", "memory", "Memory"],
  ["activity", "activity", "Activity"],
] as const;
