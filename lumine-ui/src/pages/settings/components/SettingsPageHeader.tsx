import { SETTINGS_SECTIONS, type SettingsSection } from "../SettingsNav";

type SettingsPageHeaderProps = {
  /** Which section this page is. Drives the title, so the two cannot drift. */
  section: SettingsSection;
  /** One line on what this screen is for. */
  description: string;
  /** Optional controls that belong in the header, right-aligned. */
  actions?: React.ReactNode;
};

/**
 * The heading every settings page opens with.
 *
 * The title comes from the section definition rather than being typed into each
 * page. That is not only less to keep in sync: an error path that renders a
 * fallback for several sections at once used to hardcode one section's name, so
 * Providers and Diagnostics could both announce themselves as "Voice & Models"
 * while the rail said otherwise. Reading the label from the section makes that
 * class of mistake impossible.
 */
export function SettingsPageHeader({ section, description, actions }: SettingsPageHeaderProps) {
  const definition = SETTINGS_SECTIONS.find((entry) => entry.id === section);

  return (
    <header className="settings-page-head">
      <div className="settings-page-head-text">
        <p className="eyebrow">{definition?.group ?? "Settings"}</p>
        <h1>{definition?.label ?? "Settings"}</h1>
        <p>{description}</p>
      </div>
      {actions ? <div className="settings-page-head-actions">{actions}</div> : null}
    </header>
  );
}
