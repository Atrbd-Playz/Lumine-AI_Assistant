import { useCallback, useEffect, useState } from "react";

import { Hint } from "../../../components/ui/hint";
import { Switch } from "../../../components/ui/switch";
import { getConfig } from "../../../features/settings/aiConfigClient";
import type { MusicSettings } from "../../../features/settings/aiConfigTypes";
import {
  describeLibrary,
  inspectMusicFolder,
  pickMusicFolder,
  saveMusicSettings,
  type MusicFolder,
} from "../../../features/settings/musicClient";
import { Field } from "../components/Field";
import { SettingsPageHeader } from "../components/SettingsPageHeader";

/**
 * What Lumine is allowed to reach on this computer.
 *
 * ## Why the Music tab became this screen
 *
 * It was a **library browser** — a search box, a playlist rail, and every track in
 * the folder — and the reported complaint was *"there's so much loading in the
 * music settings"*. Two separate problems lived behind that one sentence:
 *
 * - It started a Python interpreter on open, and then asked for the same folder's
 *   contents a *second* time with an empty query. Two round trips to render a
 *   settings screen.
 * - Most of what it drew was **not a setting**. A filename is not a preference. It
 *   was a second way of browsing music that Lumine already has a better way of
 *   browsing — she searches by what you *said*, tolerates spelling, and reads
 *   "vocal" as a version rather than as part of a title. A text box matching
 *   characters was strictly worse at the only job the widget could do.
 *
 * So the list is gone. What is left is the part that genuinely is a permission:
 * **which folder she may read**, and whether she may play from it. That is a
 * question about authority, it belongs next to the other capability switches, and
 * it is the only part that needed the screen to be a screen at all.
 *
 * Music itself is a **widget** on the home page now, beside Weather and News.
 *
 * ## The count is the reason this screen is allowed to load at all
 *
 * Pointing Lumine at `D:\Music` when the music is in `D:\Music\Library` is
 * completely silent: every voice request answers "I couldn't find that", and the
 * Music widget says only the folder's name. A count is the only thing on this
 * screen that can catch it, so exactly one inspection happens — here, once, on the
 * screen where the folder is chosen. The home page never asks.
 */
export function PermissionsPage() {
  const [settings, setSettings] = useState<MusicSettings>({});
  const [folder, setFolder] = useState<MusicFolder | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const library = (settings.library ?? "").trim();
  const enabled = settings.enabled !== "off";

  /** Read the folder once per change, and never on the home page. */
  const inspect = useCallback(async (path: string, isRecursive: boolean) => {
    if (!path) {
      setFolder(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      setFolder(await inspectMusicFolder(path, isRecursive));
    } catch (cause) {
      setFolder(null);
      setError(cause instanceof Error ? cause.message : "That folder could not be read.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const { document } = await getConfig();
        if (!live) return;
        const saved = document.media ?? {};
        setSettings(saved);
        await inspect((saved.library ?? "").trim(), saved.recursive !== false);
      } catch (cause) {
        if (live) {
          setError(cause instanceof Error ? cause.message : "Your settings could not be read.");
          setLoading(false);
        }
      }
    })();
    return () => {
      live = false;
    };
  }, [inspect]);

  /** Write the section and re-read what the worker will actually do with it. */
  const write = useCallback(
    async (patch: MusicSettings) => {
      setSaving(true);
      setError(null);
      try {
        await saveMusicSettings(patch);
        setSettings(patch);
        await inspect((patch.library ?? "").trim(), patch.recursive !== false);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "That could not be saved.");
      } finally {
        setSaving(false);
      }
    },
    [inspect],
  );

  const choose = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      // Cancelling resolves to `null` and is a decision, not a failure — so this
      // returns without touching the document or showing anything.
      const chosen = await pickMusicFolder();
      if (!chosen) return;
      await write({ ...settings, library: chosen.path });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "That folder could not be used.");
    } finally {
      setBusy(false);
    }
  }, [settings, write]);

  const state = describeLibrary(settings, folder, loading);

  return (
    <div className="settings-page">
      <SettingsPageHeader section="permissions" />

      <div className="settings-section">
        <h2 className="settings-section-title">Your music</h2>
        <p className="field-hint text-faint text-[11.5px] leading-[1.5]">
          One folder on this computer. Lumine reads the names inside it to find a song you ask for, then hands
          the file — or an ordered queue of them — to whatever player you already use. She never changes, moves or
          deletes anything, and nothing about your music leaves the machine.
        </p>
      </div>

      <section className="settings-block mt-4">
        <div className="settings-block-head flex items-center gap-1.5">
          <h2>The folder she may read</h2>
          <Hint label="What this is">
            The only place Lumine will look for music. Choose the folder your songs are actually in rather than
            its parent — she searches inside it, so pointing her one level too high is the mistake that makes
            her say she cannot find things you can see.
          </Hint>
        </div>

        <div className="flex flex-wrap items-center gap-2 mt-2">
          <button
            type="button"
            className="settings-primary"
            onClick={() => void choose()}
            disabled={busy || saving}
          >
            {busy ? "Choosing…" : library ? "Choose a different folder" : "Choose a folder"}
          </button>
          {library && (
            <>
              <button
                type="button"
                className="settings-secondary"
                onClick={() => void write({ ...settings, library: "" })}
                disabled={busy || saving}
              >
                Revoke access
              </button>
              <span className="music-path font-mono text-[11.5px] text-faint break-all">{library}</span>
            </>
          )}
        </div>

        <p
          className={`permission-state permission-state--${state.tone} mt-3`}
          role={state.tone === "error" ? "alert" : "status"}
        >
          {state.text}
        </p>
      </section>

      <section className="settings-block mt-4">
        <div className="settings-block-head flex items-center gap-1.5">
          <h2>What she may do with it</h2>
        </div>

        <Switch
          label="Let Lumine play from it"
          value={enabled ? "true" : "false"}
          onChange={(next) => void write({ ...settings, enabled: next === "true" ? "on" : "off" })}
          help="On by default, because the boundary is small: one folder you chose, and no verb that moves, renames or deletes anything. One thing is outside her reach — a song she starts keeps playing after the call ends, because the player's own window owns it. Skip, pause and stop are that window's controls."
          disabled={saving || !library}
        />

        <Field
          label="Look inside subfolders"
          className="mt-3"
          help="On by default, and usually what you want: a folder called 'vocal' is very often inside a folder called 'Music'. Turn it off if you want only the tracks sitting directly in the folder you chose."
        >
          <Switch
            label="Look inside subfolders"
            hideLabel
            value={settings.recursive !== false ? "true" : "false"}
            onChange={(next) => void write({ ...settings, recursive: next === "true" })}
            disabled={saving || !library}
          />
        </Field>
      </section>

      {error && (
        <p className="validation is-error mt-3" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}