import type { IconName } from "../home/types";

export type SettingsSection =
  | "appearance"
  | "voice"
  | "models"
  | "providers"
  | "diagnostics"
  | "about";

/**
 * A second level of navigation, for a section that is really several screens.
 *
 * Models is the reason this exists. Speech recognition, the language model, speech
 * synthesis and turn detection were four fields inside one scrolling column, which
 * meant a person hunting for the voice had to read past the transcription model and
 * the token cap to reach it — and a stage's own settings appeared in a different
 * place from the model that owns them. One tab per stage puts a stage, the model
 * that serves it and that model's settings in the same column.
 */
export type SettingsTab = {
  id: string;
  label: string;
  /** Shown as the `Hint` on the tab strip, not as text under the title. */
  help: string;
};

export type SettingsSectionDefinition = {
  id: SettingsSection;
  label: string;
  description: string;
  icon: IconName;
  group: "General" | "AI" | "System";
  /** Empty for a section that is a single screen. */
  tabs: SettingsTab[];
};

/**
 * Only sections that have something real behind them.
 *
 * An empty "Notifications" or "Privacy" page teaches a user the app has features
 * it does not, so those are left out until they exist rather than stubbed. Voice
 * and Models are separate sections for the same reason: voice is what Lumine
 * sounds like and which profile is speaking, models is which engines do the work,
 * and collapsing them produced one screen too long to read.
 */
export const SETTINGS_SECTIONS: SettingsSectionDefinition[] = [
  {
    id: "appearance",
    label: "Appearance",
    description: "Colour mode, type, and Lumine's presence",
    icon: "spark",
    group: "General",
    tabs: [],
  },
  {
    id: "voice",
    label: "Voice",
    description: "Which profile speaks, and how it behaves",
    icon: "waveform",
    group: "AI",
    tabs: [],
  },
  {
    id: "models",
    label: "Models",
    description: "The speech stack, one stage at a time",
    icon: "tools",
    group: "AI",
    tabs: [
      {
        id: "stt",
        label: "Speech to text",
        help: "The model that turns what you say into text for the language model.",
      },
      {
        id: "llm",
        label: "Language",
        help: "The model that decides what Lumine says, and how hard it thinks first.",
      },
      {
        id: "tts",
        label: "Speech",
        help: "The model that speaks the reply, its voice, and how fast.",
      },
      {
        id: "vad",
        label: "Turn detection",
        help: "How Lumine decides you have finished speaking, and what interrupts her.",
      },
    ],
  },
  {
    id: "providers",
    label: "Providers",
    description: "Credentials for the services Lumine speaks through",
    icon: "check",
    group: "AI",
    tabs: [],
  },
  {
    id: "diagnostics",
    label: "Diagnostics",
    description: "What each model can do, and whether it is reachable",
    icon: "clock",
    group: "System",
    tabs: [],
  },
  {
    id: "about",
    label: "About Lumine",
    description: "Who made Lumine, and what it is",
    icon: "info",
    group: "System",
    tabs: [],
  },
];

/** Sections in display order, grouped. */
export function groupedSections(): { group: string; sections: SettingsSectionDefinition[] }[] {
  const groups: { group: string; sections: SettingsSectionDefinition[] }[] = [];
  for (const section of SETTINGS_SECTIONS) {
    const existing = groups.find((entry) => entry.group === section.group);
    if (existing) existing.sections.push(section);
    else groups.push({ group: section.group, sections: [section] });
  }
  return groups;
}

export function findSection(id: SettingsSection): SettingsSectionDefinition | undefined {
  return SETTINGS_SECTIONS.find((entry) => entry.id === id);
}

/** Where the dialog currently is: a section, and a tab within it if it has any. */
export type SettingsRoute = {
  section: SettingsSection;
  tab: string | null;
};

/**
 * Normalise a route, so a section that has just been given tabs cannot be shown
 * with none of them selected.
 *
 * The tab is `null` for a section without sub-tabs, and always resolves to one of
 * a section's own tabs otherwise. That is why a caller cannot hand the dialog a
 * tab id that section does not have — which is the only real risk here, since the
 * tab lives in component state while the section lives in the parent's.
 */
export function normaliseRoute(section: SettingsSection, tab: string | null): SettingsRoute {
  const definition = findSection(section);
  if (!definition || definition.tabs.length === 0) return { section, tab: null };
  const match = definition.tabs.find((entry) => entry.id === tab) ?? definition.tabs[0];
  return { section, tab: match.id };
}
