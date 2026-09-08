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

export type IconName =
  | "home"
  | "spark"
  | "check"
  | "clock"
  | "settings"
  | "mic"
  | "send"
  | "stop"
  | "more"
  | "music"
  | "bell"
  | "activity"
  | "waveform"
  | "trash"
  | "reset"
  | "close";
