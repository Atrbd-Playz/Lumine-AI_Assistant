import { useState } from "react";
import type {
  Appearance,
  AppearancePreset,
  ConversationBubbleVariant,
  InterfaceFont,
  ThemeMode,
  ThemePresetCollection,
} from "../types";
import { SettingsPageHeader } from "../../settings/components/SettingsPageHeader";
import { Dropdown } from "../../../components/ui/dropdown";
import { Disclosure } from "../../../components/ui/disclosure";
import { Hint } from "../../../components/ui/hint";
import { DEFAULT_THEME_PALETTES } from "../constants";
import { fontStack } from "../utils";
import { Bubble, BubbleContent, BubbleGroup } from "../../../components/ui/bubble";

/* -------------------------------------------------------------------------- *
 * The palette, declared once
 * -------------------------------------------------------------------------- */

/**
 * One list, and everything else is derived from it.
 *
 * The twelve colours used to be spelled out three times: `COLORS` for the
 * interface grid, `CHAT_COLORS` for the conversation grid, and `PRESET_KEYS` for
 * the import validator. Three lists that must agree is three places for a colour
 * to be added to two of them, and the failure is silent — the validator accepts
 * a preset missing the colour the grid never showed, or the grid offers a colour
 * the validator rejects. `PRESET_KEYS` is now `ALL_COLORS.map(...)`, so the rows
 * on screen, the grid of swatches, and the thing import insists on are the same
 * twelve by construction.
 *
 * Each row also carries what the colour is *for*. A list of twelve swatches
 * labelled "canvas", "stage" and "surface" is a list of twelve things to try in
 * turn; saying which surface each one paints is the difference between tuning a
 * palette and guessing at one.
 */
type PaletteKey = keyof AppearancePreset;

type ColorRow = readonly [PaletteKey, string, string];

const INTERFACE_COLORS: readonly ColorRow[] = [
  ["accent", "Accent", "Buttons, active states, links"],
  ["icon", "Icons", "Sidebar and control glyphs"],
  ["canvas", "Canvas", "The page behind everything"],
  ["surface", "Panels", "Cards, rail, and command bar"],
  ["stage", "Stage", "The space Lumine occupies"],
  ["text", "Text", "Headings and body copy"],
  ["avatar", "Avatar glow", "The ring around Lumine’s face"],
];

const CHAT_COLORS: readonly ColorRow[] = [
  ["chatSurface", "Chat surface", "The conversation panel’s background"],
  ["chatUser", "Your bubbles", "What you said"],
  ["chatAssistant", "Lumine’s bubbles", "What Lumine said"],
  ["chatText", "Chat text", "The words inside both"],
  ["chatAccent", "Chat accent", "Timestamps and reactions"],
];

const ALL_COLORS: readonly ColorRow[] = [...INTERFACE_COLORS, ...CHAT_COLORS];

/** The twelve keys an imported preset must carry, in the order the grid shows. */
const PRESET_KEYS: PaletteKey[] = ALL_COLORS.map(([key]) => key);

const FONTS: ReadonlyArray<readonly [InterfaceFont, string]> = [
  ["Ubuntu", "Friendly clarity"],
  ["Roboto", "Clean readability"],
  ["Manrope", "Quiet utility"],
  ["Newsreader", "Editorial warmth"],
  ["Space Grotesk", "Technical clarity"],
  ["DM Mono", "Machine precision"],
];

/**
 * One list for both the interface and the chat typefaces.
 *
 * They offer the same six choices, and a chooser that showed a different subset
 * per field would be a question the settings page has no business asking. The
 * `hint` stays in the interface face on purpose: the description talks *about*
 * the typeface, so rendering it in the typeface would make the copy unreadable
 * in exactly the rows where it is most interesting.
 */
const fontOptions = FONTS.map(([font, description]) => ({
  value: font,
  label: font,
  hint: description,
  face: fontStack(font),
}));

const BUBBLE_VARIANTS: ReadonlyArray<readonly [ConversationBubbleVariant, string]> = [
  ["default", "Primary"],
  ["secondary", "Secondary"],
  ["muted", "Muted"],
  ["tinted", "Tinted"],
  ["outline", "Outline"],
  ["ghost", "Ghost"],
];

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

const HEX = /^#[0-9a-fA-F]{6}$/;
/** What a field may hold on the way to a colour: `#`, then up to six digits. */
const HEX_IN_PROGRESS = /^#[0-9a-fA-F]{0,6}$/;

const MODES: ReadonlyArray<readonly [ThemeMode, string]> = [
  ["light", "Light"],
  ["dark", "Dark"],
];

/* -------------------------------------------------------------------------- *
 * Presentational pieces
 * -------------------------------------------------------------------------- */

function Card({
  title,
  help,
  children,
  actions,
}: {
  title: string;
  help?: string;
  children: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <section className="ap-card">
      <header className="ap-card-head">
        <h2 className="ap-card-title">
          {title}
          {help && <Hint>{help}</Hint>}
        </h2>
        {actions}
      </header>
      {children}
    </section>
  );
}

/**
 * A labelled row with its control on the right.
 *
 * The page used to stack every setting as label-above-control inside its own
 * full-width band, which made two on/off switches occupy as much height as the
 * palette editor and gave the eye nothing to group by. A row puts the name and
 * the control on one line, so a group of related switches reads as a list
 * instead of as separate sections.
 */
function Row({
  label,
  description,
  children,
}: {
  label: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div className="ap-row">
      <div className="ap-row-text">
        <span className="ap-row-label">{label}</span>
        <span className="ap-row-desc">{description}</span>
      </div>
      <div className="ap-row-control">{children}</div>
    </div>
  );
}

function Switch({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      className={`ap-switch ${checked ? "is-on" : ""}`}
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={onChange}
    >
      <span className="ap-switch-knob" />
    </button>
  );
}

/**
 * A miniature of the app, painted in the palette it was given.
 *
 * This is the reason the page can be tuned rather than guessed at. Twelve
 * swatches in a grid show twelve colours; what a person needs to see is whether
 * the *result* holds together — whether the stage is too close to the canvas, or
 * the text disappears into the panel. So every one of the twelve is placed on the
 * surface it actually paints, and a palette that is wrong looks wrong here
 * rather than in the window behind the dialog.
 *
 * `compact` is the same drawing at card size, used for the light/dark chooser.
 */
function PaletteProof({
  palette,
  font,
  compact = false,
}: {
  palette: AppearancePreset;
  font: InterfaceFont;
  compact?: boolean;
}) {
  return (
    <div
      className={`ap-proof ${compact ? "ap-proof-compact" : ""}`}
      style={{ background: palette.canvas, fontFamily: fontStack(font) }}
      aria-hidden="true"
    >
      <div className="ap-proof-rail" style={{ background: palette.surface }}>
        <span className="ap-proof-dot" style={{ background: palette.icon }} />
        <span className="ap-proof-dot" style={{ background: palette.icon }} />
        <span className="ap-proof-dot is-accent" style={{ background: palette.accent }} />
      </div>
      <div className="ap-proof-main">
        <div className="ap-proof-stage" style={{ background: palette.stage }}>
          <span
            className="ap-proof-halo"
            style={{ background: palette.avatar, boxShadow: `0 0 12px 2px ${palette.avatar}80` }}
          />
        </div>
        <div className="ap-proof-copy">
          <span className="ap-proof-line is-strong" style={{ background: palette.text }} />
          <span className="ap-proof-line" style={{ background: palette.text }} />
        </div>
        <div className="ap-proof-chat" style={{ background: palette.chatSurface }}>
          <span className="ap-proof-bubble" style={{ background: palette.chatUser }}>
            <span className="ap-proof-line is-tight" style={{ background: palette.chatText }} />
          </span>
          <span className="ap-proof-bubble" style={{ background: palette.chatAssistant }}>
            <span className="ap-proof-line is-tight" style={{ background: palette.chatText }} />
          </span>
          <span className="ap-proof-dot is-tiny" style={{ background: palette.chatAccent }} />
        </div>
      </div>
    </div>
  );
}

/**
 * A typeface, shown in itself.
 *
 * "Newsreader" and "Roboto" are names nobody holds in their head long enough to
 * choose between, so both of these fields set their own label and their sample
 * sentence in the selected face. The sample is a real sentence rather than an
 * "Aa", because the thing being chosen is how six words will look in a
 * paragraph, and a two-glyph specimen cannot show that.
 */
function TypeSpecimen({
  label,
  font,
  onChange,
}: {
  label: string;
  font: InterfaceFont;
  onChange: (font: InterfaceFont) => void;
}) {
  return (
    <div className="ap-type">
      <span className="ap-type-label">{label}</span>
      <Dropdown
        label={`${label} typeface`}
        value={font}
        onChange={(value) => onChange(value as InterfaceFont)}
        options={fontOptions}
      />
      <p className="ap-type-sample" style={{ fontFamily: fontStack(font) }}>
        Good evening, Master.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- *
 * The page
 * -------------------------------------------------------------------------- */

/** Appearance-only settings. Voice behaviour lives in AI → Voice & Models. */
export function AppearanceDialog({
  mode,
  setMode,
  cursorGaze,
  setCursorGaze,
  appearance,
  setAppearance,
  presets,
  savePreset,
  importPresets,
  deletePreset,
  onResetPalette,
  onReset,
  onClose,
}: {
  mode: ThemeMode;
  setMode: (value: ThemeMode) => void;
  cursorGaze: boolean;
  setCursorGaze: (value: boolean) => void;
  appearance: Appearance;
  setAppearance: (value: Appearance) => void;
  presets: Record<string, AppearancePreset>;
  savePreset: (name: string, preset: AppearancePreset) => void;
  importPresets: (collection: ThemePresetCollection) => void;
  deletePreset: (name: string) => void;
  onResetPalette: () => void;
  onReset: () => void;
  onClose: () => void;
}) {
  const [presetName, setPresetName] = useState("");
  const [saving, setSaving] = useState(false);
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
   * it has to stick: a chooser that snapped back to its placeholder after every
   * pick would have nothing left to delete.
   *
   * Cleared by any individual colour edit, so the label never claims the colours
   * on screen are still someone else's.
   */
  const [applied, setApplied] = useState<string | null>(null);

  /**
   * Which of the twelve colours the editor below the swatches is showing.
   *
   * The alternative was twenty-four controls on the page at once — a swatch and a
   * hex field for each colour, across two grids — and it was the least usable part
   * of the tab: each hex field was ten pixels of monospace, the grids were two
   * columns of near-identical rows, and there was no way to see what any of them
   * did. One colour at a time, chosen from a strip that still shows all twelve, is
   * fewer controls and more information.
   */
  const [selected, setSelected] = useState<PaletteKey>("accent");

  /**
   * The hex field's own text while it is being typed into.
   *
   * Held separately from `appearance` so a half-typed `#e8` is allowed to sit in
   * the field. Committing every keystroke straight onto the colour fought the
   * person typing: the native swatch beside it snapped to black on `#`, and
   * clearing the field was impossible because a valid intermediate value was
   * required to keep it. The draft is dropped as soon as the value is a real
   * colour, and on blur if it never became one.
   */
  const [hexDraft, setHexDraft] = useState<string | null>(null);

  const update = <Key extends keyof Appearance>(key: Key, value: Appearance[Key]) => {
    setAppearance({ ...appearance, [key]: value });
    setApplied(null);
    if (key === selected) setHexDraft(null);
  };

  const currentPreset = Object.fromEntries(
    PRESET_KEYS.map((key) => [key, appearance[key]]),
  ) as AppearancePreset;

  /**
   * How many of the twelve colours have been moved off the shipped palette.
   *
   * Compared against the palette for the *current* mode, because `onResetPalette`
   * restores `DEFAULT_THEME_PALETTES[mode]` and that is the promise the Reset
   * button makes. Counting against the other mode's defaults would show "9 set"
   * on a dark theme that has never been touched, which is a badge describing a
   * difference the user cannot see and did not make.
   */
  const baseline = DEFAULT_THEME_PALETTES[mode];
  const tweakedColours = PRESET_KEYS.filter((key) => currentPreset[key] !== baseline[key]).length;

  const selectedRow = ALL_COLORS.find(([key]) => key === selected) ?? ALL_COLORS[0];
  const selectedValue = currentPreset[selected];
  const selectedLabel = selectedRow[1];
  const selectedDescription = selectedRow[2];
  const shownHex = hexDraft ?? selectedValue;

  /**
   * Both the selected colour and the draft are cleared by anything global.
   *
   * A reset that left the swatch strip pointing at `chatSurface` and the hex field
   * holding `#1a1a1a` would be showing a colour that no longer exists, next to an
   * editor for a different one, with nothing on screen saying the page had been
   * reset at all. The state that describes the palette has to be reset with the
   * palette.
   */
  const clearLocalPaletteState = () => {
    setSelected("accent");
    setHexDraft(null);
    setApplied(null);
  };

  const applyPreset = (name: string, preset: AppearancePreset) => {
    setAppearance({ ...appearance, ...preset });
    setApplied(name);
    setHexDraft(null);
  };

  const saveCurrentPreset = () => {
    const name = presetName.trim();
    if (!name) return;
    savePreset(name, currentPreset);
    setPresetName("");
    setSaving(false);
    setApplied(name);
  };

  const parseImport = (text: string) => {
    try {
      const parsed = JSON.parse(text) as unknown;
      if (!parsed || typeof parsed !== "object") throw new Error("The root must be an object.");
      const collection: ThemePresetCollection = {};
      for (const theme of ["light", "dark"] as const) {
        const themeValue = (parsed as Record<string, unknown>)[theme];
        if (themeValue === undefined) continue;
        if (!themeValue || typeof themeValue !== "object" || Array.isArray(themeValue)) {
          throw new Error(`${theme} must contain named presets.`);
        }
        const presetsForTheme: Record<string, AppearancePreset> = {};
        for (const [name, value] of Object.entries(themeValue as Record<string, unknown>)) {
          if (!name.trim() || !value || typeof value !== "object" || Array.isArray(value)) {
            throw new Error(`Preset ${name || "without a name"} is invalid.`);
          }
          const preset = value as Record<string, unknown>;
          if (!PRESET_KEYS.every((key) => typeof preset[key] === "string" && HEX.test(preset[key] as string))) {
            throw new Error(`Preset ${name} needs all 12 six-digit hex colors.`);
          }
          presetsForTheme[name.trim()] = Object.fromEntries(
            PRESET_KEYS.map((key) => [key, preset[key]]),
          ) as AppearancePreset;
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

  const presetNames = Object.keys(presets);

  return (
    <div className="settings-page ap-page">
      <SettingsPageHeader section="appearance" />

      <div className="ap-stack">
        {/* ---------------------------------------------------------------- */}
        <Card
          title="Colour mode"
          help="Light or dark applies to every surface at once, including the conversation panel and the ring around Lumine’s face."
        >
          {/*
           * Two cards rather than a dropdown.
           *
           * This is the most-used control on the page and it was a dropdown
           * asking a two-option question, which costs a click to read the current
           * answer and a second to change it. Each card draws that mode’s
           * miniature, so the choice is made by looking rather than by reading.
           *
           * The other pickers on this page stay in `Dropdown`. The difference is
           * that a typeface, a bubble style and a palette have names you cannot
           * infer from a picture, whereas "which of these two looks like the app
           * I want" is a visual question, and a list of two words cannot answer it.
           */}
          <div className="ap-themes">
            <div className="ap-modes">
              {MODES.map(([value, label]) => {
                // The active mode is drawn in the palette actually on screen. The
                // other is drawn in its shipped default, which is what switching to
                // it would give unless it has been customised.
                const palette = value === mode ? currentPreset : DEFAULT_THEME_PALETTES[value];
                return (
                  <button
                    key={value}
                    type="button"
                    className={`ap-mode ${value === mode ? "is-active" : ""}`}
                    aria-pressed={value === mode}
                    onClick={() => setMode(value)}
                  >
                    <PaletteProof palette={palette} font={appearance.font} compact />
                    <span className="ap-mode-name">{label}</span>
                  </button>
                );
              })}
            </div>

            {/*
             * The live result, beside the choice rather than under it.
             *
             * Stacked, the two cards and this preview were 340px of column, and
             * the active card was already a small copy of this drawing. Beside
             * them the pair answers two different questions at the same height:
             * the cards are "which one", this is "what mine looks like now". The
             * proof grows to fill the column, so the preview is the large object
             * the page is about instead of a fixed 133px strip.
             */}
            <div className="ap-preview">
              <span className="ap-preview-label">Preview</span>
              <PaletteProof palette={currentPreset} font={appearance.font} />
            </div>
          </div>
        </Card>

        {/* ---------------------------------------------------------------- */}
        <Card title="Typeface" help="Sets the app, and the conversation panel, in the two faces you choose here.">
          <div className="ap-types">
            <TypeSpecimen label="Interface" font={appearance.font} onChange={(font) => update("font", font)} />
            <TypeSpecimen label="Chat" font={appearance.chatFont} onChange={(font) => update("chatFont", font)} />
          </div>
        </Card>

        {/* ---------------------------------------------------------------- */}
        <Card title="Conversation" help="How Lumine’s messages are drawn in the conversation panel.">
          <div className="ap-field">
            <span className="ap-field-label">
              Bubble style
              <Hint>Tinted, Ghost and Secondary differ in a way no label can convey — the sample below is the only part of this setting that shows the choice.</Hint>
            </span>
            <Dropdown
              label="Chat bubble style"
              value={appearance.chatBubbleVariant}
              onChange={(value) => update("chatBubbleVariant", value as ConversationBubbleVariant)}
              options={BUBBLE_VARIANTS.map(([variant, label]) => ({ value: variant, label }))}
            />
          </div>

          {/*
           * A live sample, drawn with the same `Bubble` the panel renders with.
           *
           * Reusing the real component rather than restyling a div is the point:
           * a sample hand-built to resemble a bubble drifts from the panel the
           * first time either is edited, and the sample is the only evidence the
           * person has.
           */}
          <div className="ap-bubble-preview">
            <BubbleGroup className="w-full">
              <Bubble
                variant={appearance.chatBubbleVariant}
                align="end"
                style={{ fontFamily: fontStack(appearance.chatFont) }}
              >
                <BubbleContent>I&rsquo;m here — say anything.</BubbleContent>
              </Bubble>
              <Bubble
                variant={appearance.chatBubbleVariant}
                align="start"
                style={{ fontFamily: fontStack(appearance.chatFont) }}
              >
                <BubbleContent>Listening.</BubbleContent>
              </Bubble>
            </BubbleGroup>
          </div>
        </Card>

        {/* ---------------------------------------------------------------- */}
        <Card title="Presence">
          <Row label="Cursor-aware gaze" description="Lumine’s eyes follow your pointer. Off leaves them on their idle montage.">
            <Switch checked={cursorGaze} onChange={() => setCursorGaze(!cursorGaze)} label="Cursor-aware gaze" />
          </Row>
          <Row label="Avatar colour" description="The warm ring around Lumine’s face. Off leaves the face alone and drops the ring.">
            <Switch
              checked={appearance.showAvatarColor}
              onChange={() => update("showAvatarColor", !appearance.showAvatarColor)}
              label="Avatar colour"
            />
          </Row>
        </Card>

        {/* ---------------------------------------------------------------- */}
        <Card
          title="Colour"
          help="Twelve colours, one per layer. Pick one to edit it; the strip below always shows all twelve. Saved palettes stay on this machine."
          actions={
            <button
              type="button"
              className="ap-ghost-button"
              onClick={() => {
                onResetPalette();
                clearLocalPaletteState();
              }}
            >
              {/* Scoped to this card's twelve colours, unlike the footer's "Reset
                  all appearance", which is why it is named for colours. */}
              Reset colours
            </button>
          }
        >
          {[INTERFACE_COLORS, CHAT_COLORS].map((rows) => (
            <div key={rows[0][0]} className="ap-swatch-group">
              <p className="ap-subhead">{rows === INTERFACE_COLORS ? "Interface" : "Conversation"}</p>
              {/*
               * One track per colour, in a single line.
               *
               * `auto-fit` chose six tracks for the seven interface colours, so the
               * seventh sat alone on a second row with five empty cells beside it —
               * the exact raggedness this row exists to avoid. A fixed count
               * cannot orphan a swatch.
               *
               * Two counts, because "cannot orphan" and "fits" pull against each
               * other at the window's 940px minimum: seven columns of 67px clip
               * "Lumine's bubbles" to an ellipsis. The narrow count splits the
               * seven into 4+3 and leaves the five whole, and both come from the
               * same `rows` so neither can drift from what is actually rendered.
               */}
              <div
                className="ap-swatches"
                style={
                  {
                    "--ap-count": rows.length,
                    "--ap-count-narrow": rows.length > 6 ? 4 : rows.length,
                  } as React.CSSProperties
                }
              >
                {rows.map(([key, label, description]) => (
                  <button
                    key={key}
                    type="button"
                    className={`ap-swatch ${selected === key ? "is-active" : ""}`}
                    aria-pressed={selected === key}
                    aria-label={`${label} — ${description}`}
                    onClick={() => {
                      setSelected(key);
                      setHexDraft(null);
                    }}
                  >
                    <span className="ap-swatch-chip" style={{ background: currentPreset[key] }} />
                    <span className="ap-swatch-name">{label}</span>
                    {/*
                     * The value under every swatch, not just the selected one.
                     *
                     * A palette is text as much as it is colour — the hex is what
                     * gets copied into a design tool, matched against a mockup, or
                     * pasted into an import file. Making it visible for all twelve
                     * means reading a value never requires selecting a colour
                     * first, which is the whole point of a strip that shows them
                     * all rather than one at a time.
                     */}
                    <span className="ap-swatch-hex">{currentPreset[key].toUpperCase()}</span>
                  </button>
                ))}
              </div>
            </div>
          ))}

          {/*
           * One editor, for whichever colour is selected.
           *
           * The native swatch is the large target and the hex field sits beside
           * it for the value people copy out of a design tool. Neither is
           * duplicated, because a second hex box next to the first is two answers
           * to "what is this colour" and they can disagree.
           */}
          <div className="ap-editor">
            <label className="ap-editor-picker" style={{ background: selectedValue }}>
              <input
                type="color"
                value={selectedValue}
                aria-label={`${selectedLabel} colour picker`}
                onChange={(event) => update(selected, event.target.value)}
              />
            </label>
            <div className="ap-editor-text">
              <span className="ap-editor-name">{selectedLabel}</span>
              <span className="ap-editor-desc">{selectedDescription}</span>
              <input
                className="ap-editor-hex"
                value={shownHex}
                maxLength={7}
                spellCheck={false}
                aria-label={`${selectedLabel} hex value`}
                onChange={(event) => {
                  const next = event.target.value;
                  // Reject the keystroke rather than accepting it and correcting
                  // it later. A field that silently refuses to show a `g` is
                  // easier to use than one that shows it, ignores it, and then
                  // throws the whole line away on blur.
                  if (!HEX_IN_PROGRESS.test(next)) return;
                  setHexDraft(next);
                  if (HEX.test(next)) update(selected, next);
                }}
                onBlur={() => setHexDraft(null)}
              />
            </div>
          </div>

          <p className="ap-subhead">Saved palettes</p>
          {presetNames.length > 0 ? (
            <>
              <div className="ap-presets">
                {presetNames.map((name) => (
                  <button
                    key={name}
                    type="button"
                    className={`ap-preset ${applied === name ? "is-active" : ""}`}
                    aria-pressed={applied === name}
                    onClick={() => applyPreset(name, presets[name])}
                  >
                    <span className="ap-preset-dots" aria-hidden="true">
                      <span style={{ background: presets[name].canvas }} />
                      <span style={{ background: presets[name].surface }} />
                      <span style={{ background: presets[name].accent }} />
                    </span>
                    <span className="ap-preset-name">{name}</span>
                  </button>
                ))}
              </div>
              {applied && (
                <p className="ap-applied">
                  <span>
                    <strong>{applied}</strong> is applied. Change any colour and it becomes yours.
                  </span>
                  <button
                    type="button"
                    className="settings-quiet"
                    onClick={() => {
                      deletePreset(applied);
                      setApplied(null);
                    }}
                  >
                    Delete {applied}
                  </button>
                </p>
              )}
            </>
          ) : (
            <p className="ap-empty">No saved palettes for {mode === "light" ? "light" : "dark"} yet.</p>
          )}

          {saving ? (
            <div className="ap-preset-save">
              <input
                value={presetName}
                placeholder="Name this palette"
                aria-label="Palette name"
                autoFocus
                onChange={(event) => setPresetName(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") saveCurrentPreset();
                  if (event.key === "Escape") {
                    setSaving(false);
                    setPresetName("");
                  }
                }}
              />
              <button type="button" className="ap-solid-button" onClick={saveCurrentPreset} disabled={!presetName.trim()}>
                Save
              </button>
              <button
                type="button"
                className="ap-ghost-button"
                onClick={() => {
                  setSaving(false);
                  setPresetName("");
                }}
              >
                Cancel
              </button>
            </div>
          ) : (
            <div className="ap-preset-save">
              <button type="button" className="ap-ghost-button" onClick={() => setSaving(true)}>
                Save current as…
              </button>
              {tweakedColours > 0 && (
                <span className="ap-tweaked">
                  {tweakedColours} of 12 changed from the {mode} default
                </span>
              )}
            </div>
          )}
        </Card>

        {/* ---------------------------------------------------------------- */}
        <Card title="Import">
          <Disclosure label="Import palettes from JSON" open={importOpen} onOpenChange={setImportOpen}>
            <div className="ap-import">
              <div className="ap-import-head">
                <span className="ap-import-label">
                  From a file
                  <Hint>A JSON object with a light and/or dark section, each holding named presets. Every preset needs all twelve colours; a missing one is rejected by name.</Hint>
                </span>
                <label className="ap-ghost-button">
                  Choose file
                  <input type="file" accept=".txt,.json,application/json,text/plain" onChange={importFile} />
                </label>
              </div>
              <textarea
                value={importText}
                placeholder={IMPORT_EXAMPLE}
                aria-label="Palette import JSON"
                onChange={(event) => {
                  setImportText(event.target.value);
                  setImportMessage("");
                }}
              />
              <div className="ap-import-actions">
                <button
                  type="button"
                  className="ap-solid-button"
                  onClick={() => parseImport(importText)}
                  disabled={!importText.trim()}
                >
                  Import pasted JSON
                </button>
                <button type="button" className="ap-ghost-button" onClick={() => setImportText(IMPORT_EXAMPLE)}>
                  Use example
                </button>
              </div>
              {importMessage && <p className="ap-import-message">{importMessage}</p>}
            </div>
          </Disclosure>
        </Card>
      </div>

      <footer className="settings-actions">
        <button type="button" className="settings-secondary" onClick={() => { onReset(); clearLocalPaletteState(); }}>
          Reset all appearance
        </button>
        <button type="button" className="settings-primary" onClick={onClose}>
          Done
        </button>
      </footer>
    </div>
  );
}
