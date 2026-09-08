import { useEffect, useState } from "react";
import { DEFAULT_APPEARANCE } from "../constants";
import type { Appearance, AppearancePreset, ThemeMode } from "../types";

const STORAGE_KEY = "lumine.preferences.v1";

type StoredPreferences = {
  mode: ThemeMode;
  cursorGaze: boolean;
  appearance: Appearance;
  presets: Record<string, AppearancePreset>;
};

const DEFAULT_PREFERENCES: StoredPreferences = {
  mode: "dark",
  cursorGaze: true,
  appearance: DEFAULT_APPEARANCE,
  presets: {},
};

function readPreferences(): StoredPreferences {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (!stored) return DEFAULT_PREFERENCES;
    const parsed = JSON.parse(stored) as Partial<StoredPreferences>;
    return {
      ...DEFAULT_PREFERENCES,
      ...parsed,
      appearance: { ...DEFAULT_APPEARANCE, ...parsed.appearance },
      presets: parsed.presets ?? {},
    };
  } catch {
    return DEFAULT_PREFERENCES;
  }
}

/** Keeps appearance choices across launches while retaining safe defaults. */
export function usePreferences() {
  const [preferences, setPreferences] = useState<StoredPreferences>(readPreferences);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
  }, [preferences]);

  return {
    mode: preferences.mode,
    setMode: (mode: ThemeMode) => setPreferences((current) => ({ ...current, mode })),
    cursorGaze: preferences.cursorGaze,
    setCursorGaze: (cursorGaze: boolean) => setPreferences((current) => ({ ...current, cursorGaze })),
    appearance: preferences.appearance,
    setAppearance: (appearance: Appearance) => setPreferences((current) => ({ ...current, appearance })),
    presets: preferences.presets,
    savePreset: (name: string, preset: AppearancePreset) => setPreferences((current) => ({ ...current, presets: { ...current.presets, [name]: preset } })),
    deletePreset: (name: string) => setPreferences((current) => {
      const presets = { ...current.presets };
      delete presets[name];
      return { ...current, presets };
    }),
  };
}
