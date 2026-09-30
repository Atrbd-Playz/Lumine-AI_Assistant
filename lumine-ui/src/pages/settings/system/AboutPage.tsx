import { SettingsPageHeader } from "../components/SettingsPageHeader";
import { APP } from "../../../features/branding";

/**
 * Who made this, and what it is.
 *
 * ## Why the details are literals here and not read from Tauri
 *
 * `productName`, `version` and the publisher all exist in `tauri.conf.json` and
 * are reachable through the Tauri API, so this page could read them. It does not,
 * on purpose: those values are only correct *after a rebuild*, so a page that
 * reported them would show a stale name to anyone running `tauri dev`, which is
 * how a version string ends up lying. A literal here is true the moment it is
 * read, and `npm run build` fails loudly if the two drift enough to matter.
 *
 * The mark is the app's own icon, served from `public/`, so the About screen and
 * the taskbar are guaranteed to be the same image.
 */
export function AboutPage() {
  return (
    <div className="settings-page">
      <SettingsPageHeader section="about" actions={<img className="about-mark about-mark-sm" src="/lumine-logo-512.png" width={40} height={40} alt="" aria-hidden="true" />} />

      <div className="settings-section">
        <div className="about-identity flex items-center gap-5">
          <img
            className="about-mark"
            src="/lumine-logo-512.png"
            width={96}
            height={96}
            alt=""
            aria-hidden="true"
          />
          <div className="about-identity-text min-w-0">
            <p className="about-name">{APP.name}</p>
            <p className="about-tagline">{APP.tagline}</p>
            <p className="about-version">Version {APP.version}</p>
          </div>
        </div>
      </div>

      <div className="settings-section">
        <h2 className="settings-section-title">Ownership</h2>
        <dl className="about-facts grid gap-3.5 m-0">
          <div className="about-fact">
            <dt>Created and owned by</dt>
            <dd>{APP.author}</dd>
          </div>
          <div className="about-fact">
            <dt>Copyright</dt>
            <dd>{APP.copyright}</dd>
          </div>
          <div className="about-fact">
            <dt>Licence</dt>
            <dd>{APP.license}</dd>
          </div>
        </dl>
      </div>

      <div className="settings-section">
        <h2 className="settings-section-title">What Lumine is</h2>
        <p className="field-hint text-faint text-[11.5px] leading-[1.5]">{APP.description}</p>
      </div>
    </div>
  );
}
