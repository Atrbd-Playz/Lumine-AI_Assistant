import type { Appearance, AppearancePreset, LumineState, ThemePalettes } from "./types";

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

/**
 * What the stage says, per state.
 *
 * Every one of the seven states has its own line, and none of them is a
 * placeholder. That is the point of the table: it is a `Record` over
 * `LumineState`, so a state that reaches the UI without copy is a compile error
 * rather than an empty headline, and a state that lies — "here's what I found"
 * printed while a connection is still being negotiated — is a line someone has to
 * defend rather than a default.
 *
 * The greeting is deliberately *not* here. It is the only line that would have
 * been fabricated, and a static "Good evening" drawn at an arbitrary hour is the
 * kind of detail that makes a presence feel like a template.
 */
export const STATE_COPY: Record<LumineState, { eyebrow: string; title: string; detail: string }> = {
  idle: { eyebrow: "Lumine · resting", title: "Here whenever you are, Master.", detail: "Start a call and I’ll be right with you." },
  connecting: { eyebrow: "Reaching Lumine", title: "One moment, Master.", detail: "Waking the worker and joining the room." },
  online: { eyebrow: "Voice channel open", title: "I’m here.", detail: "Say anything — I’ll pick it up." },
  listening: { eyebrow: "Listening", title: "I’m listening.", detail: "Speak naturally — I’ll keep the context." },
  thinking: { eyebrow: "Thinking", title: "Working on it.", detail: "Putting the next best step together." },
  speaking: { eyebrow: "Speaking", title: "Here’s what I found.", detail: "I’m ready for your next instruction." },
  error: { eyebrow: "Connection needs attention", title: "I lost the thread, Master.", detail: "The reason is below — nothing was lost from this conversation." },
};

/**
 * The rail's items, and the union of their ids.
 *
 * `NavId` is derived from the array rather than written beside it, so the rail and
 * the router cannot disagree about what a destination is called. `Home.tsx` holds
 * `nav` as a `NavId` and narrows it before choosing a page, so a nav row with no
 * page behind it falls through to the stage rather than to whatever the last
 * branch happened to be.
 */
export const NAV_ITEMS = [
  ["home", "home", "Home"],
  ["focus", "timer", "Focus"],
  ["avatar", "avatar", "Avatar Lab"],
  ["tools", "tools", "Tools"],
  ["memory", "memory", "Memory"],
  ["activity", "activity", "Activity"],
] as const;

/**
 * The destinations that stay on a phone, and the rest go behind a menu.
 *
 * Six labelled rows do not fit in a 64px thumb bar at any font size that can be
 * read — they fit as six 9px glyphs, which is a different, worse interface. Three
 * carry the app (home, the timer and notes, what the worker reported) and the
 * fourth control opens the remainder, so nothing is unreachable and the bar stops
 * reflowing its own text on every rotation.
 */
export const PRIMARY_NAV_IDS = ["home", "focus", "activity"] as const satisfies readonly NavId[];

export type NavId = (typeof NAV_ITEMS)[number][0];

/**
 * The two destinations that render a workspace page.
 *
 * `tools` and `memory` are the same view with a different body, so they are one
 * `kind` rather than two routes. The tuple is `satisfies readonly NavId[]` so a
 * typo here is a compile error, and `Home.tsx` narrows on it rather than casting
 * `nav` — which is what let `"conversation"` reach `WorkspaceView` and index an
 * undefined record.
 */
export const WORKSPACE_NAV_IDS = ["tools", "memory"] as const satisfies readonly NavId[];

export type WorkspaceNavId = (typeof WORKSPACE_NAV_IDS)[number];

/**
 * Whether a destination renders a workspace page.
 *
 * Written as a predicate rather than an `as` at the call site, because the call
 * site is exactly where the original bug lived: `nav as "tools" | "memory"` is
 * a claim about a value, and it was false for two of the five destinations. Here
 * the claim is the body of the function, and every caller gets the narrowing for
 * free.
 */
export function isWorkspaceNav(nav: NavId): nav is WorkspaceNavId {
  return (WORKSPACE_NAV_IDS as readonly string[]).includes(nav);
}
