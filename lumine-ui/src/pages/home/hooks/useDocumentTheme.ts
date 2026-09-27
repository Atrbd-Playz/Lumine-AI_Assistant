import { useEffect } from "react";
import type { CSSProperties } from "react";
import type { ThemeMode } from "../types";

/**
 * Mirrors the app theme onto the root element.
 *
 * The palette is applied to `.lumine-app`, which is the element the app owns.
 * Anything rendered into a portal is a child of `<body>` instead, so it would
 * fall back to the bare `:root` dark tokens: no light mode, no custom palette,
 * and a notification that quietly ignores the Appearance settings.
 *
 * Setting the same class and the same custom properties on `<html>` closes that
 * gap. Everything is removed again on cleanup so a stale palette cannot outlive
 * the app that set it.
 */
export function useDocumentTheme(mode: ThemeMode, variables: CSSProperties) {
  // `variables` is rebuilt on every render, so depend on its shape rather than
  // its identity. Writing the same properties again is a no-op for the style
  // attribute, so an extra run costs nothing but avoids re-running the effect
  // for every unrelated state change in the app.
  const serialized = JSON.stringify(variables);

  useEffect(() => {
    const root = document.documentElement;
    root.classList.remove("theme-light", "theme-dark");
    root.classList.add(`theme-${mode}`);
    for (const [property, value] of Object.entries(JSON.parse(serialized) as Record<string, string>)) {
      if (value !== undefined && value !== null) root.style.setProperty(property, String(value));
    }

    return () => {
      for (const property of Object.keys(JSON.parse(serialized) as Record<string, string>)) {
        root.style.removeProperty(property);
      }
    };
  }, [mode, serialized]);
}
