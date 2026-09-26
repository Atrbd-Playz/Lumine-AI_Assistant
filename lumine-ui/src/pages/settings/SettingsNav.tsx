import type { IconName } from "../home/types";

export type SettingsSection = "appearance" | "voice" | "providers" | "diagnostics";

export type SettingsSectionDefinition = {
  id: SettingsSection;
  label: string;
  description: string;
  icon: IconName;
  group: "General" | "AI" | "System";
};

/**
 * Only sections that have something real behind them.
 *
 * An empty "Notifications" or "Privacy" page teaches a user the app has features
 * it does not, so those are left out until they exist rather than stubbed.
 */
export const SETTINGS_SECTIONS: SettingsSectionDefinition[] = [
  {
    id: "appearance",
    label: "Appearance",
    description: "Color mode, palette, and Lumine's presence",
    icon: "spark",
    group: "General",
  },
  {
    id: "voice",
    label: "Voice & Models",
    description: "The active speech stack and its profiles",
    icon: "waveform",
    group: "AI",
  },
  {
    id: "providers",
    label: "Providers",
    description: "Credentials for the services Lumine speaks through",
    icon: "tools",
    group: "AI",
  },
  {
    id: "diagnostics",
    label: "Diagnostics",
    description: "Runtime health and configuration checks",
    icon: "check",
    group: "System",
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
