import { useState } from "react";
import type { Appearance, AppearancePreset, ConversationBubbleVariant, InterfaceFont, ThemeMode, ThemePresetCollection } from "../types";

type ColorKey = "accent" | "icon" | "canvas" | "surface" | "stage" | "text" | "avatar";
type PresetColorKey = ColorKey | "chatSurface" | "chatUser" | "chatAssistant" | "chatText" | "chatAccent";
const COLORS: Array<[ColorKey, string]> = [["accent", "Accent"], ["icon", "Icons"], ["canvas", "Canvas"], ["surface", "Panels"], ["stage", "Command space"], ["text", "Text"], ["avatar", "Avatar glow"]];
const FONTS: Array<[InterfaceFont, string]> = [["Ubuntu", "Friendly clarity"], ["Roboto", "Clean readability"], ["Manrope", "Quiet utility"], ["Newsreader", "Editorial warmth"], ["Space Grotesk", "Technical clarity"], ["DM Mono", "Machine precision"]];
const BUBBLE_VARIANTS: Array<[ConversationBubbleVariant, string]> = [["default", "Primary"], ["secondary", "Secondary"], ["muted", "Muted"], ["tinted", "Tinted"], ["outline", "Outline"], ["ghost", "Ghost"]];
const CHAT_COLORS: Array<[keyof Appearance, string]> = [["chatSurface", "Chat surface"], ["chatUser", "User bubble"], ["chatAssistant", "Assistant bubble"], ["chatText", "Chat text"], ["chatAccent", "Chat accent"]];
const PRESET_KEYS: Array<keyof AppearancePreset> = ["accent", "icon", "canvas", "surface", "stage", "text", "avatar", "chatSurface", "chatUser", "chatAssistant", "chatText", "chatAccent"];
const IMPORT_EXAMPLE = `{
  "light": {
    "Sakura": {
      "accent": "#d95f8c", "icon": "#8d6472", "canvas": "#fff5f7", "surface": "#fffafd",
      "stage": "#fff0f4", "text": "#3a2630", "avatar": "#f39ab5",
      "chatSurface": "#fffafd", "chatUser": "#ffe3ec", "chatAssistant": "#f7c9d8",
      "chatText": "#3a2630", "chatAccent": "#d95f8c"
    }
  },
  "dark": {}
}`;

function Switch({ checked, onChange, label }: { checked: boolean; onChange: () => void; label: string }) {
  return <button type="button" className={`switch ${checked ? "on" : ""}`} role="switch" aria-checked={checked} aria-label={label} onClick={onChange}><span /></button>;
}

/** Modal settings surface. It owns no state; the page remains the single source of truth. */
export function AppearanceDialog({ mode, setMode, cursorGaze, setCursorGaze, appearance, setAppearance, presets, savePreset, importPresets, deletePreset, onResetPalette, onReset, onClose }: { mode: ThemeMode; setMode: (value: ThemeMode) => void; cursorGaze: boolean; setCursorGaze: (value: boolean) => void; appearance: Appearance; setAppearance: (value: Appearance) => void; presets: Record<string, AppearancePreset>; savePreset: (name: string, preset: AppearancePreset) => void; importPresets: (collection: ThemePresetCollection) => void; deletePreset: (name: string) => void; onResetPalette: () => void; onReset: () => void; onClose: () => void }) {
  const [presetName, setPresetName] = useState("");
  const [importText, setImportText] = useState("");
  const [importMessage, setImportMessage] = useState("");
  const update = <Key extends keyof Appearance>(key: Key, value: Appearance[Key]) => setAppearance({ ...appearance, [key]: value });
  const presetKeys: PresetColorKey[] = PRESET_KEYS;
  const currentPreset = Object.fromEntries(presetKeys.map((key) => [key, appearance[key]])) as AppearancePreset;
  const applyPreset = (preset: AppearancePreset) => setAppearance({ ...appearance, ...preset });
  const saveCurrentPreset = () => { const name = presetName.trim(); if (!name) return; savePreset(name, currentPreset); setPresetName(""); };
  const parseImport = (text: string) => {
    try {
      const parsed = JSON.parse(text) as unknown;
      if (!parsed || typeof parsed !== "object") throw new Error("The root must be an object.");
      const collection: ThemePresetCollection = {};
      for (const theme of ["light", "dark"] as const) {
        const themeValue = (parsed as Record<string, unknown>)[theme];
        if (themeValue === undefined) continue;
        if (!themeValue || typeof themeValue !== "object" || Array.isArray(themeValue)) throw new Error(`${theme} must contain named presets.`);
        const presetsForTheme: Record<string, AppearancePreset> = {};
        for (const [name, value] of Object.entries(themeValue as Record<string, unknown>)) {
          if (!name.trim() || !value || typeof value !== "object" || Array.isArray(value)) throw new Error(`Preset ${name || "without a name"} is invalid.`);
          const preset = value as Record<string, unknown>;
          if (!PRESET_KEYS.every((key) => typeof preset[key] === "string" && /^#[0-9a-fA-F]{6}$/.test(preset[key] as string))) throw new Error(`Preset ${name} needs all 12 six-digit hex colors.`);
          presetsForTheme[name.trim()] = Object.fromEntries(PRESET_KEYS.map((key) => [key, preset[key]])) as AppearancePreset;
        }
        collection[theme] = presetsForTheme;
      }
      if (!collection.light && !collection.dark) throw new Error("Add a light or dark section.");
      importPresets(collection);
      setImportText("");
      setImportMessage("Presets imported.");
    } catch (error) {
      setImportMessage(error instanceof Error ? error.message : "Could not read this preset file.");
    }
  };
  const importFile = (event: React.ChangeEvent<HTMLInputElement>) => { const file = event.target.files?.[0]; if (!file) return; file.text().then(parseImport).catch(() => setImportMessage("Could not read this file.")); event.target.value = ""; };
  return <div className="settings-scrim" role="presentation" onMouseDown={onClose}><section className="appearance-dialog" role="dialog" aria-modal="true" aria-labelledby="appearance-title" onMouseDown={(event) => event.stopPropagation()}><header><div><p className="eyebrow">Lumine preferences</p><h2 id="appearance-title">Appearance</h2></div><button className="dialog-close" onClick={onClose} aria-label="Close appearance settings">×</button></header><div className="appearance-body">
    <section className="setting-group"><div className="setting-title"><div><strong>Color mode</strong><p>Change Lumine’s ambient ground.</p></div><div className="segmented"><button className={mode === "light" ? "active" : ""} onClick={() => setMode("light")}>Light</button><button className={mode === "dark" ? "active" : ""} onClick={() => setMode("dark")}>Dark</button></div></div></section>
    <section className="setting-group"><div className="setting-title"><div><strong>Cursor-aware gaze</strong><p>Let Lumine’s eyes follow your cursor.</p></div><Switch checked={cursorGaze} onChange={() => setCursorGaze(!cursorGaze)} label="Toggle cursor-aware gaze" /></div></section>
    <section className="setting-group"><div className="setting-title"><div><strong>Avatar color</strong><p>Show the warm color around Lumine’s face.</p></div><Switch checked={appearance.showAvatarColor} onChange={() => update("showAvatarColor", !appearance.showAvatarColor)} label="Toggle avatar color" /></div></section>
    <section className="setting-group"><div className="setting-title"><div><strong>Interface type</strong><p>Choose the voice of the command space.</p></div></div><div className="font-choices">{FONTS.map(([font, description]) => <button key={font} className={`${font === "Newsreader" ? "serif" : ""} ${appearance.font === font ? "selected" : ""}`} onClick={() => update("font", font)}><b>{font}</b><span>{description}</span></button>)}</div></section>
    <section className="setting-group"><div className="setting-title"><div><strong>Conversation style</strong><p>Give the activity layer its own voice.</p></div></div><div className="font-choices">{FONTS.map(([font, description]) => <button key={`chat-${font}`} className={`${font === "Newsreader" ? "serif" : ""} ${appearance.chatFont === font ? "selected" : ""}`} onClick={() => update("chatFont", font)}><b>{font}</b><span>{description}</span></button>)}</div><div className="bubble-variant-choices">{BUBBLE_VARIANTS.map(([variant, label]) => <button key={variant} className={appearance.chatBubbleVariant === variant ? "selected" : ""} onClick={() => update("chatBubbleVariant", variant)}>{label}</button>)}</div></section>
    <section className="setting-group colors"><div className="setting-title"><div><strong>Custom palette</strong><p>Use a hex value for each visual layer.</p></div><button className="reset-link" onClick={onResetPalette}>Reset</button></div><div className="preset-save"><input value={presetName} placeholder="Name this palette" aria-label="Preset name" onChange={(event) => setPresetName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") saveCurrentPreset(); }} /><button onClick={saveCurrentPreset} disabled={!presetName.trim()}>Save preset</button></div>{Object.keys(presets).length > 0 && <div className="preset-list">{Object.entries(presets).map(([name, preset]) => <div className="preset-row" key={name}><button className="preset-choice" onClick={() => applyPreset(preset)}><span className="preset-swatch" style={{ background: preset.accent }} />{name}</button><button className="preset-delete" onClick={() => deletePreset(name)} aria-label={`Delete ${name} preset`}>×</button></div>)}</div>}<div className="palette-import"><div className="setting-title"><div><strong>Import palettes</strong><p>Paste JSON or upload a .txt file for both themes.</p></div><label className="file-button">Upload .txt<input type="file" accept=".txt,.json,application/json,text/plain" onChange={importFile} /></label></div><textarea value={importText} placeholder={IMPORT_EXAMPLE} aria-label="Palette import JSON" onChange={(event) => { setImportText(event.target.value); setImportMessage(""); }} /><div className="palette-import-actions"><button onClick={() => parseImport(importText)} disabled={!importText.trim()}>Import pasted JSON</button><button onClick={() => setImportText(IMPORT_EXAMPLE)}>Use example</button></div>{importMessage && <p className="import-message">{importMessage}</p>}</div><div className="color-grid">{COLORS.map(([key, label]) => <label key={key}><span>{label}</span><div><input type="color" value={appearance[key]} onChange={(event) => update(key, event.target.value)} /><input type="text" aria-label={`${label} hex color`} value={appearance[key]} maxLength={7} onChange={(event) => { if (/^#[0-9A-Fa-f]{0,6}$/.test(event.target.value)) update(key, event.target.value); }} /></div></label>)}</div><p className="setting-subheading">Conversation palette</p><div className="color-grid">{CHAT_COLORS.map(([key, label]) => <label key={String(key)}><span>{label}</span><div><input type="color" value={String(appearance[key])} onChange={(event) => update(key, event.target.value)} /><input type="text" aria-label={`${label} hex color`} value={String(appearance[key])} maxLength={7} onChange={(event) => { if (/^#[0-9A-Fa-f]{0,6}$/.test(event.target.value)) update(key, event.target.value); }} /></div></label>)}</div></section>
  </div><footer><button className="reset-all-button" onClick={onReset}>Reset all preferences</button><button onClick={onClose}>Done</button></footer></section></div>;
}
