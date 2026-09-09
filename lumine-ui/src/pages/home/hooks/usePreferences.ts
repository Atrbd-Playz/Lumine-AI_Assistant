import { useEffect, useState } from "react";
import { DEFAULT_APPEARANCE, DEFAULT_DARK_PALETTE, DEFAULT_LIGHT_PALETTE, DEFAULT_THEME_PALETTES, DEFAULT_THEME_PRESETS } from "../constants";
import type { Appearance, AppearancePreset, ThemeMode, ThemePalettes, ThemePresetCollection } from "../types";

const STORAGE_KEY = "lumine.preferences.v1";

type StoredPreferences = {
  mode: ThemeMode;
  cursorGaze: boolean;
  appearance: Appearance;
  presets: Record<string, AppearancePreset>;
  palettes: ThemePalettes;
  themePresets: Record<ThemeMode, Record<string, AppearancePreset>>;
};

const DEFAULT_PREFERENCES: StoredPreferences = {
  mode: "dark",
  cursorGaze: true,
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
      appearance: { ...DEFAULT_APPEARANCE, ...parsed.appearance },
      presets: parsed.presets ?? {},
      palettes: { ...DEFAULT_THEME_PALETTES, ...palettes },
      themePresets: parsed.themePresets ?? DEFAULT_THEME_PRESETS,
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
    localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
  }, [preferences]);

  return {
    mode: preferences.mode,
    setMode: (mode: ThemeMode) => setPreferences((current) => ({ ...current, mode, appearance: { ...current.appearance, ...current.palettes[mode] } })),
    cursorGaze: preferences.cursorGaze,
    setCursorGaze: (cursorGaze: boolean) => setPreferences((current) => ({ ...current, cursorGaze })),
    appearance,
    setAppearance: (nextAppearance: Appearance) => setPreferences((current) => ({ ...current, appearance: nextAppearance, palettes: { ...current.palettes, [current.mode]: toPalette(nextAppearance) } })),
    resetAppearance: () => setPreferences((current) => ({ ...current, appearance: { ...current.appearance, ...(current.mode === "light" ? DEFAULT_LIGHT_PALETTE : DEFAULT_DARK_PALETTE) }, palettes: { ...current.palettes, [current.mode]: current.mode === "light" ? DEFAULT_LIGHT_PALETTE : DEFAULT_DARK_PALETTE } })),
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
