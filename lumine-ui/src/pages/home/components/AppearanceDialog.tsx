import { useState } from "react";
import type { Appearance, AppearancePreset, ConversationBubbleVariant, InterfaceFont, ThemeMode, ThemePresetCollection } from "../types";
import { SettingsPageHeader } from "../../settings/components/SettingsPageHeader";
import { Dropdown } from "../../../components/ui/dropdown";
import { Disclosure } from "../../../components/ui/disclosure";
import { Hint } from "../../../components/ui/hint";
import { Field } from "../../settings/components/Field";
import { DEFAULT_THEME_PALETTES } from "../constants";
import { fontStack } from "../utils";
import { Bubble, BubbleContent, BubbleGroup } from "../../../components/ui/bubble";

type ColorKey = "accent" | "icon" | "canvas" | "surface" | "stage" | "text" | "avatar";
type PresetColorKey = ColorKey | "chatSurface" | "chatUser" | "chatAssistant" | "chatText" | "chatAccent";
const COLORS: Array<[ColorKey, string]> = [["accent", "Accent"], ["icon", "Icons"], ["canvas", "Canvas"], ["surface", "Panels"], ["stage", "Command space"], ["text", "Text"], ["avatar", "Avatar glow"]];
const FONTS: Array<[InterfaceFont, string]> = [["Ubuntu", "Friendly clarity"], ["Roboto", "Clean readability"], ["Manrope", "Quiet utility"], ["Newsreader", "Editorial warmth"], ["Space Grotesk", "Technical clarity"], ["DM Mono", "Machine precision"]];
/**
 * The six faces, each row set in the face it names.
 *
 * One list for both the interface and the chat typefaces, because they offer the
 * same six choices and a chooser that shows one subset for one field and a
 * different subset for the other is a question the settings page has no business
 * asking. The `hint` stays in the interface face on purpose: the description
 * talks *about* the typeface, so rendering it in the typeface would make the
 * copy unreadable in exactly the rows where it is most interesting.
 */
const fontOptions = FONTS.map(([font, description]) => ({ value: font, label: font, hint: description, face: fontStack(font) }));
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

/** Appearance-only settings. Voice behaviour lives in AI → Voice & Models. */
export function AppearanceDialog({ mode, setMode, cursorGaze, setCursorGaze, appearance, setAppearance, presets, savePreset, importPresets, deletePreset, onResetPalette, onReset, onClose }: { mode: ThemeMode; setMode: (value: ThemeMode) => void; cursorGaze: boolean; setCursorGaze: (value: boolean) => void; appearance: Appearance; setAppearance: (value: Appearance) => void; presets: Record<string, AppearancePreset>; savePreset: (name: string, preset: AppearancePreset) => void; importPresets: (collection: ThemePresetCollection) => void; deletePreset: (name: string) => void; onResetPalette: () => void; onReset: () => void; onClose: () => void }) {
  const [presetName, setPresetName] = useState("");
  const [importText, setImportText] = useState("");
  const [importMessage, setImportMessage] = useState("");
  /**
   * The import disclosure is controlled, because a rejected paste has to be
   * visible.
   *
   * `parseImport` reports its verdict in a paragraph inside the panel, and a
   * message the user cannot see is the same as no message. A paste that fails
   * therefore opens the panel — including a paste made from the file input, where
   * there is no click near the panel to look at.
   */
  const [importOpen, setImportOpen] = useState(false);
  /**
   * Which saved palette is currently applied, if any.
   *
   * Held rather than derived from the colours, because a palette is applied by
   * copying twelve values onto the controls and nothing about those values records
   * where they came from. It is also what the Delete button acts on, which is why
   * it has to stick: a dropdown that snapped back to its placeholder after every
   * pick would have nothing left to delete.
   *
   * Cleared by any individual colour edit, so the label never claims the colours
   * on screen are still someone else's.
   */
  const [applied, setApplied] = useState<string | null>(null);
  const update = <Key extends keyof Appearance>(key: Key, value: Appearance[Key]) => { setAppearance({ ...appearance, [key]: value }); setApplied(null); };
  const presetKeys: PresetColorKey[] = PRESET_KEYS;
  const currentPreset = Object.fromEntries(presetKeys.map((key) => [key, appearance[key]])) as AppearancePreset;
  /**
   * How many of the twelve colours have been moved off the shipped palette.
   *
   * Compared against the palette for the *current* mode, because `onResetPalette`
   * restores `DEFAULT_THEME_PALETTES[mode]` and that is the promise the Reset
   * button makes. Counting against the other mode's defaults would show "9 set"
   * on a dark theme that has never been touched, which is a badge describing a
   * difference the user cannot see and did not make.
   *
   * It is the count the disclosure reports, so a colour that was changed and then
   * forgotten stays findable rather than becoming invisible.
   */
  const baseline = DEFAULT_THEME_PALETTES[mode];
  const tweakedColours = presetKeys.filter((key) => currentPreset[key] !== baseline[key]).length;
  const applyPreset = (name: string, preset: AppearancePreset) => { setAppearance({ ...appearance, ...preset }); setApplied(name); };
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
    // On both outcomes, not just the failure. A successful import that leaves the
    // panel shut reports nothing at all, which reads as the click not registering.
    setImportOpen(true);
  };
  const importFile = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setImportOpen(true);
    file
      .text()
      .then(parseImport)
      .catch(() => setImportMessage("Could not read this file."));
    event.target.value = "";
  };
  return <div className="settings-page"><SettingsPageHeader section="appearance" /><div className="appearance-body">
    <section className="setting-group">
      <Field label="Color mode" help="Light or dark applies to every surface, including the conversation panel and the avatar's glow.">
        <Dropdown label="Color mode" value={mode} onChange={(v) => setMode(v as ThemeMode)} options={[{ value: "light", label: "Light" }, { value: "dark", label: "Dark" }]} />
      </Field>
    </section>
    <section className="setting-group">
      <Field label="Cursor-aware gaze" help="Lumine's eyes follow your pointer. Purely local, and off means the eyes stay on their idle montage.">
        <Switch checked={cursorGaze} onChange={() => setCursorGaze(!cursorGaze)} label="Toggle cursor-aware gaze" />
      </Field>
    </section>
    <section className="setting-group">
      <Field label="Avatar color" help="The warm ring of colour around Lumine's face. Turning it off leaves the face alone and drops the ring.">
        <Switch checked={appearance.showAvatarColor} onChange={() => update("showAvatarColor", !appearance.showAvatarColor)} label="Toggle avatar color" />
      </Field>
    </section>
    <section className="setting-group">
      <Field label="Interface typeface" help="Typeface for the command space, sidebar, and settings.">
        <Dropdown label="Interface typeface" value={appearance.font} onChange={(v) => update("font", v as InterfaceFont)} options={fontOptions} />
      </Field>
    </section>
    <section className="setting-group">
      <Field label="Chat typeface" help="Typeface for the conversation panel.">
        <Dropdown label="Chat typeface" value={appearance.chatFont} onChange={(v) => update("chatFont", v as InterfaceFont)} options={fontOptions} />
      </Field>
    </section>
    <section className="setting-group">
      <Field label="Chat bubble style" help="Bubble treatment for the conversation panel.">
        <Dropdown label="Chat bubble style" value={appearance.chatBubbleVariant} onChange={(v) => update("chatBubbleVariant", v as ConversationBubbleVariant)} options={BUBBLE_VARIANTS.map(([variant, label]) => ({ value: variant, label }))} />
      </Field>
      {/*
       * A live sample, drawn with the same `Bubble` the panel renders with.
       *
       * "Tinted", "Ghost" and "Secondary" are three names for three differences
       * you cannot see until you have seen them, and this is the only chooser in
       * the app whose options are invisible in the trigger itself — a name does
       * not change shape. Six rows of text naming a style is a quiz; six rows
       * each showing the style is a choice. It reuses the real component rather
       * than restyling a div, so the sample cannot drift from the panel.
       */}
      <div className="bubble-preview" aria-hidden="true">
        <BubbleGroup className="w-full">
          <Bubble variant={appearance.chatBubbleVariant} align="end" style={{ fontFamily: fontStack(appearance.chatFont) }}>
            <BubbleContent>I&rsquo;m here — say anything.</BubbleContent>
          </Bubble>
          <Bubble variant={appearance.chatBubbleVariant} align="start" style={{ fontFamily: fontStack(appearance.chatFont) }}>
            <BubbleContent>Listening.</BubbleContent>
          </Bubble>
        </BubbleGroup>
      </div>
    </section>
    <section className="setting-group colors">
      <div className="setting-title">
        <div className="flex items-center gap-1.5"><strong>Custom palette</strong><Hint label="About the palette">Twelve colours, one per layer. Saved presets are stored on this machine and can be pasted in from JSON for both themes.</Hint></div>
        <button className="reset-link" onClick={() => { onResetPalette(); setApplied(null); }}>Reset</button>
      </div>
      <div className="preset-save"><input value={presetName} placeholder="Name this palette" aria-label="Preset name" onChange={(event) => setPresetName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") saveCurrentPreset(); }} /><button onClick={saveCurrentPreset} disabled={!presetName.trim()}>Save preset</button></div>
      {Object.keys(presets).length > 0 && <div className="flex flex-col gap-2"><Dropdown label="Saved palettes" value={applied ?? ""} onChange={(name) => { const preset = presets[name]; if (preset) applyPreset(name, preset); }} placeholder="Apply a saved palette" options={Object.entries(presets).map(([name, preset]) => ({ value: name, label: name, swatch: preset.accent }))} />{applied && <div className="flex items-center gap-2"><p className="field-hint flex-1">{applied} is applied. Change any colour and it becomes yours.</p><button type="button" className="settings-quiet" onClick={() => { deletePreset(applied); setApplied(null); }}>Delete {applied}</button></div>}</div>}
      {/*
       * The two twelve-colour panels and the JSON paste box live behind
       * disclosures, and what stays visible is the answer to "what is this
       * palette" — the name, or a swatch if there is none.
       *
       * The raw controls were most of the page: twenty-four swatches across two
       * grids, plus a textarea whose placeholder is a nine-line JSON document.
       * None of it is wrong, and all of it is a wall that pushed everything above
       * it off a laptop screen. What a person opens Appearance for is the mode
       * and the typeface; the palette is a second visit.
       */}
      <Disclosure label="Custom colours" count={tweakedColours}>
        <div className="color-grid">{COLORS.map(([key, label]) => <label key={key}><span>{label}</span><div><input type="color" value={appearance[key]} onChange={(event) => update(key, event.target.value)} /><input type="text" aria-label={`${label} hex color`} value={appearance[key]} maxLength={7} onChange={(event) => { if (/^#[0-9A-Fa-f]{0,6}$/.test(event.target.value)) update(key, event.target.value); }} /></div></label>)}</div>
        <p className="setting-subheading">Conversation palette</p>
        <div className="color-grid">{CHAT_COLORS.map(([key, label]) => <label key={String(key)}><span>{label}</span><div><input type="color" value={String(appearance[key])} onChange={(event) => update(key, event.target.value)} /><input type="text" aria-label={`${label} hex color`} value={String(appearance[key])} maxLength={7} onChange={(event) => { if (/^#[0-9A-Fa-f]{0,6}$/.test(event.target.value)) update(key, event.target.value); }} /></div></label>)}</div>
      </Disclosure>
      <Disclosure label="Import palettes" open={importOpen} onOpenChange={setImportOpen}>
        <div className="palette-import"><div className="setting-title"><div className="flex items-center gap-1.5"><strong>From a file</strong><Hint label="About importing">A JSON object with a light and/or dark section, each holding named presets. Every preset needs all twelve colours; a missing one is rejected by name.</Hint></div><label className="file-button">Upload .txt<input type="file" accept=".txt,.json,application/json,text/plain" onChange={importFile} /></label></div><textarea value={importText} placeholder={IMPORT_EXAMPLE} aria-label="Palette import JSON" onChange={(event) => { setImportText(event.target.value); setImportMessage(""); }} /><div className="palette-import-actions"><button onClick={() => parseImport(importText)} disabled={!importText.trim()}>Import pasted JSON</button><button onClick={() => setImportText(IMPORT_EXAMPLE)}>Use example</button></div>{importMessage && <p className="import-message">{importMessage}</p>}</div>
      </Disclosure>
    </section>
  </div><footer className="settings-actions"><button type="button" className="settings-secondary" onClick={() => { onReset(); setApplied(null); }}>Reset all appearance</button><button type="button" className="settings-primary" onClick={onClose}>Done</button></footer></div>;
}
