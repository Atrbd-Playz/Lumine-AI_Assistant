import { useEffect, useState } from "react";
import { DEFAULT_APPEARANCE, DEFAULT_DARK_PALETTE, DEFAULT_LIGHT_PALETTE, DEFAULT_THEME_PALETTES, DEFAULT_THEME_PRESETS } from "../constants";
import { DEFAULT_INTERRUPTION_MODE, normalizeInterruptionMode, type InterruptionMode } from "../../../features/voice/interruption";
import type { Appearance, AppearancePreset, ThemeMode, ThemePalettes, ThemePresetCollection } from "../types";

const STORAGE_KEY = "lumine.preferences.v1";

type StoredPreferences = {
  mode: ThemeMode;
  cursorGaze: boolean;
  interruptionMode: InterruptionMode;
  appearance: Appearance;
  presets: Record<string, AppearancePreset>;
  palettes: ThemePalettes;
  themePresets: Record<ThemeMode, Record<string, AppearancePreset>>;
};

const DEFAULT_PREFERENCES: StoredPreferences = {
  mode: "dark",
  cursorGaze: true,
  interruptionMode: DEFAULT_INTERRUPTION_MODE,
  appearance: DEFAULT_APPEARANCE,
  presets: {},
  palettes: DEFAULT_THEME_PALETTES,
  themePresets: DEFAULT_THEME_PRESETS,
};

function readPreferences(): StoredPreferences {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (!stored) return DEFAULT_PREFERENCES;
    const parsed = JSON.parse(stored) as Partial<StoredPreferences>;
    const palettes = parsed.palettes ?? {
      light: { ...DEFAULT_LIGHT_PALETTE, ...(parsed.appearance ?? {}) },
      dark: { ...DEFAULT_DARK_PALETTE, ...(parsed.appearance ?? {}) },
    };
    return {
      ...DEFAULT_PREFERENCES,
      ...parsed,
      // `mode` is read as an index into two records, one line below, so a value
      // that is not one of the two keys (a hand-edited file, a value written by
      // an older build) would look up `undefined` and hand it to a component
      // that calls `Object.keys` on it. Validate it at the boundary, where the
      // fallback is a decision rather than a crash.
      mode: parsed.mode === "light" || parsed.mode === "dark" ? parsed.mode : DEFAULT_PREFERENCES.mode,
      appearance: { ...DEFAULT_APPEARANCE, ...parsed.appearance },
      presets: parsed.presets ?? {},
      palettes: { ...DEFAULT_THEME_PALETTES, ...palettes },
      // Per-key rather than `?? DEFAULT`: a stored `{ light: {... } }` with no
      // `dark` half replaces the default wholesale under `??`, because the
      // object itself is present. That is how `presets` came to be `undefined`
      // for anyone who had only ever saved a light preset.
      themePresets: {
        light: parsed.themePresets?.light ?? DEFAULT_THEME_PRESETS.light,
        dark: parsed.themePresets?.dark ?? DEFAULT_THEME_PRESETS.dark,
      },
      interruptionMode: normalizeInterruptionMode(parsed.interruptionMode),
    };
  } catch {
    return DEFAULT_PREFERENCES;
  }
}

/** Keeps appearance choices across launches while retaining safe defaults. */
export function usePreferences() {
  const [preferences, setPreferences] = useState<StoredPreferences>(readPreferences);
  const appearance = { ...preferences.appearance, ...preferences.palettes[preferences.mode] };

  useEffect(() => {
    // Best-effort, like every other write in the app. A private window or a
    // full disk must cost the user their saved theme on next launch, not the
    // settings screen they are standing in.
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
    } catch {
      // Losing the preference for this session is the cheap version of this failure.
    }
  }, [preferences]);

  return {
    mode: preferences.mode,
    setMode: (mode: ThemeMode) => setPreferences((current) => ({ ...current, mode, appearance: { ...current.appearance, ...current.palettes[mode] } })),
    cursorGaze: preferences.cursorGaze,
    setCursorGaze: (cursorGaze: boolean) => setPreferences((current) => ({ ...current, cursorGaze })),
    interruptionMode: preferences.interruptionMode,
    setInterruptionMode: (interruptionMode: InterruptionMode) => setPreferences((current) => ({ ...current, interruptionMode })),
    appearance,
    setAppearance: (nextAppearance: Appearance) => setPreferences((current) => ({ ...current, appearance: nextAppearance, palettes: { ...current.palettes, [current.mode]: toPalette(nextAppearance) } })),
    resetAppearance: () => setPreferences((current) => ({ ...current, appearance: { ...current.appearance, ...(current.mode === "light" ? DEFAULT_LIGHT_PALETTE : DEFAULT_DARK_PALETTE) }, palettes: { ...current.palettes, [current.mode]: current.mode === "light" ? DEFAULT_LIGHT_PALETTE : DEFAULT_DARK_PALETTE } })),
    /**
     * Returns every appearance setting to what shipped.
     *
     * This exists because the footer's "Reset all appearance" was wired to
     * `resetAppearance`, which only restores the twelve colours for the mode
     * currently on screen. The interface typeface, the chat typeface, the bubble
     * style and the avatar-colour toggle all survived a click on a button named
     * after them — the label described something the function did not do. So the
     * two are now distinct and each is named for what it does: `resetAppearance`
     * is the palette editor's Reset, and this is the footer's.
     *
     * Both modes' palettes are restored, because "all" means all and a user who
     * tuned light as well as dark asked for the app to go back to how it
     * shipped. Saved presets are deliberately untouched: a named palette is
     * something the user made, and forgetting your settings is not a request to
     * delete your work.
     */
    resetAllAppearance: () => setPreferences((current) => ({
      ...current,
      mode: "dark",
      cursorGaze: true,
      appearance: { ...DEFAULT_APPEARANCE, ...DEFAULT_DARK_PALETTE },
      palettes: { light: DEFAULT_LIGHT_PALETTE, dark: DEFAULT_DARK_PALETTE },
    })),
    presets: preferences.themePresets[preferences.mode],
    savePreset: (name: string, preset: AppearancePreset) => setPreferences((current) => ({ ...current, themePresets: { ...current.themePresets, [current.mode]: { ...current.themePresets[current.mode], [name]: preset } } })),
    importPresets: (collection: ThemePresetCollection) => setPreferences((current) => ({
      ...current,
      themePresets: {
        light: { ...current.themePresets.light, ...(collection.light ?? {}) },
        dark: { ...current.themePresets.dark, ...(collection.dark ?? {}) },
      },
    })),
    deletePreset: (name: string) => setPreferences((current) => {
      const presets = { ...current.themePresets[current.mode] };
      delete presets[name];
      return { ...current, themePresets: { ...current.themePresets, [current.mode]: presets } };
    }),
  };
}

function toPalette(appearance: Appearance): AppearancePreset {
  const { accent, icon, canvas, surface, stage, text, avatar, chatSurface, chatUser, chatAssistant, chatText, chatAccent } = appearance;
  return { accent, icon, canvas, surface, stage, text, avatar, chatSurface, chatUser, chatAssistant, chatText, chatAccent };
}
