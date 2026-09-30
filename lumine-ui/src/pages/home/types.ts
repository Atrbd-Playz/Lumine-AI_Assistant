/** Shared data shapes for the Lumine home experience. */

/**
 * What Lumine is doing, as the presence layer tells it.
 *
 * This used to be four states, and the four states were the problem: everything
 * that was not `speaking`, `listening` or `thinking` was reported as one of those
 * three, so `connecting`, `online` and `reconnecting` all drew the same thinking
 * face. A user pressing the call button and seeing a thoughtful expression has
 * been told the call connected, and has not been told anything at all.
 *
 * `connecting` and `online` are therefore first-class here rather than folded
 * into `thinking`, and they are what makes the idle/connecting/connected question
 * answerable at all. `error` is here for the same reason: it used to render as
 * `idle`, so a failed call looked exactly like a call nobody started.
 *
 * A total union, so `Record<LumineState, …>` tables in `constants.ts` and
 * `Presence.tsx` fail to compile the moment a state is added without a face and a
 * line of copy.
 */
export type LumineState = "idle" | "connecting" | "online" | "listening" | "thinking" | "speaking" | "error";
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
  | "info"
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
  | "search"
  | "phone"
  | "phone-down"
  | "video"
  | "video-off"
  | "monitor"
  | "speaker"
  | "speaker-off"
  | "sun"
  | "cloud"
  | "rain"
  | "snow"
  | "storm"
  | "news"
  | "note"
  | "timer"
  | "menu"
  | "collapse"
  | "expand";
