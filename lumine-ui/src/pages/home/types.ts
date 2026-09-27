/** Shared data shapes for the Lumine home experience. */

export type LumineState = "idle" | "listening" | "thinking" | "speaking";
export type EntryKind = "note" | "action" | "message";
export type ThemeMode = "light" | "dark";
export type InterfaceFont = "Manrope" | "Newsreader" | "Space Grotesk" | "DM Mono" | "Roboto" | "Ubuntu";
export type ConversationBubbleVariant = "default" | "secondary" | "muted" | "tinted" | "outline" | "ghost";

export type Entry = {
  id: number;
  kind: EntryKind;
  content: string;
  time: string;
};

export type Appearance = {
  accent: string;
  icon: string;
  canvas: string;
  surface: string;
  stage: string;
  text: string;
  avatar: string;
  showAvatarColor: boolean;
  font: InterfaceFont;
  chatFont: InterfaceFont;
  chatSurface: string;
  chatUser: string;
  chatAssistant: string;
  chatText: string;
  chatAccent: string;
  chatBubbleVariant: ConversationBubbleVariant;
};

export type AppearancePreset = Pick<Appearance, "accent" | "icon" | "canvas" | "surface" | "stage" | "text" | "avatar" | "chatSurface" | "chatUser" | "chatAssistant" | "chatText" | "chatAccent">;
export type ThemePalettes = Record<ThemeMode, AppearancePreset>;
export type ThemePresetCollection = Partial<Record<ThemeMode, Record<string, AppearancePreset>>>;

/**
 * Every icon the app draws.
 *
 * One set, one library, one map. The union is closed on purpose: a name that is
 * not on this list is a typo the compiler catches, rather than a name that maps to
 * nothing and renders an empty box. The call controls are here for the same reason
 * the rest are — a call bar with hand-drawn SVGs next to Phosphor glyphs reads as
 * two apps.
 */
export type IconName =
  | "home"
  | "avatar"
  | "chat"
  | "tools"
  | "memory"
  | "activity"
  | "spark"
  | "check"
  | "clock"
  | "settings"
  | "mic"
  | "mic-off"
  | "send"
  | "stop"
  | "more"
  | "music"
  | "bell"
  | "waveform"
  | "trash"
  | "plus"
  | "copy"
  | "reset"
  | "close"
  | "presentation"
  | "phone"
  | "phone-down"
  | "video"
  | "video-off"
  | "monitor"
  | "speaker"
  | "speaker-off";
