import { SETTINGS_SECTIONS, type SettingsSection } from "../SettingsNav";

type SettingsPageHeaderProps = {
  /** Which section this page is. Drives the title, so the two cannot drift. */
  section: SettingsSection;
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
 *
 * There is deliberately no line of prose under the title. The rail already
 * describes every section, and a second description directly under the heading
 * was the same sentence twice — once where you look to find things and once
 * where you have already found them. Anything worth saying about a control lives
 * in that control's own `Hint`.
 */
export function SettingsPageHeader({ section, actions }: SettingsPageHeaderProps) {
  const definition = SETTINGS_SECTIONS.find((entry) => entry.id === section);

  return (
    <header className="settings-page-head">
      <div className="settings-page-head-text min-w-0">
        <p className="eyebrow">{definition?.group ?? "Settings"}</p>
        <h1>{definition?.label ?? "Settings"}</h1>
      </div>
      {actions ? <div className="settings-page-head-actions flex items-center gap-2 shrink-0 pt-1">{actions}</div> : null}
    </header>
  );
}
