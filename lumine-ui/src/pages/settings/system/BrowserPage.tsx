import { useEffect, useState } from "react";

import { Dropdown } from "../../../components/ui/dropdown";
import { getConfig, saveConfig } from "../../../features/settings/aiConfigClient";
import type { ConfigDocument, PersonalBrowserEngine } from "../../../features/settings/aiConfigTypes";
import { Field } from "../components/Field";
import { SettingsPageHeader } from "../components/SettingsPageHeader";

const ENGINE_OPTIONS = [
  {
    value: "auto",
    label: "Automatic",
    hint: "Use Brave when installed; otherwise use Playwright Chromium.",
  },
  {
    value: "brave",
    label: "Brave",
    hint: "Use Brave’s existing profile named Lumine.",
  },
  {
    value: "chromium",
    label: "Playwright Chromium",
    hint: "Use the browser installed by Playwright.",
  },
] as const;

export function BrowserPage() {
  const [document, setDocument] = useState<ConfigDocument | null>(null);
  const [engine, setEngine] = useState<PersonalBrowserEngine>("auto");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let live = true;
    void getConfig()
      .then(({ document: current }) => {
        if (!live) return;
        setDocument(current);
        const savedEngine = current.desktop?.browsers?.personalEngine;
        setEngine(
          savedEngine === "brave" || savedEngine === "chromium" ? savedEngine : "auto",
        );
      })
      .catch((cause: unknown) => {
        if (live) {
          setError(cause instanceof Error ? cause.message : "The browser setting could not be read.");
        }
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, []);

  const save = async () => {
    if (!document || saving) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    const next: ConfigDocument = {
      ...document,
      desktop: {
        ...document.desktop,
        browsers: {
          ...document.desktop?.browsers,
          personalEngine: engine,
        },
      },
    };
    try {
      await saveConfig(next);
      setDocument(next);
      setSaved(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The browser setting could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  const canSave = document !== null && !loading && !saving && engine !== (document.desktop?.browsers?.personalEngine ?? "auto");

  return (
    <div className="settings-page">
      <SettingsPageHeader section="browser" />

      <section className="settings-block mt-4">
        <div className="settings-block-head">
          <h2>Lumine’s personal browser</h2>
          <p className="field-hint text-faint text-[11.5px] leading-[1.5]">
            This controls pages opened by Lumine’s browser tools. It does not change which browser opens your own links.
          </p>
        </div>

        <Field
          label="Browser engine"
          help="Brave uses its existing profile named Lumine, including its saved sign-ins, settings, and extensions."
          hint="Close Brave before Lumine starts it. Brave does not allow the same profile to be used by two running browser processes."
          className="mt-3"
        >
          <Dropdown
            label="Personal browser engine"
            value={engine}
            options={ENGINE_OPTIONS}
            onChange={(value) => {
              if (value === "auto" || value === "brave" || value === "chromium") {
                setEngine(value);
                setSaved(false);
              }
            }}
            disabled={loading || saving}
          />
        </Field>

        <p className="field-hint text-faint text-[11.5px] leading-[1.5] mt-3">
          Lumine does not apply its own ad filter. Brave’s default Shields handle ads; YouTube may still show some ads or interrupt playback.
          Close Lumine’s browser before reopening it for this choice to take effect.
        </p>

        <div className="flex items-center gap-3 mt-4">
          <button type="button" className="settings-primary" onClick={() => void save()} disabled={!canSave}>
            {saving ? "Saving…" : "Save browser"}
          </button>
          {saved && <span role="status" className="field-hint text-faint text-[11.5px]">Saved for the next browser start.</span>}
        </div>

        {loading && <p className="validation is-pending mt-3">Loading browser settings…</p>}
        {error && <p className="validation is-error mt-3" role="alert">{error}</p>}
      </section>
    </div>
  );
}
