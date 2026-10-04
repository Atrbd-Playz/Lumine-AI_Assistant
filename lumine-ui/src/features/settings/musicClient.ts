/**
 * The music library, as a *permission* rather than a library browser.
 *
 * ## What this file no longer does, and why
 *
 * It used to carry the whole library across the IPC boundary: `searchMusic`
 * returned ranked rows, `MusicFolder.tracks` carried every track in the folder,
 * and the Music settings page drew them. That is gone, and the reason is a
 * measured complaint — *"there's so much loading in the music settings"* — which
 * traced to opening the page starting a Python interpreter, and the page then
 * asking for the same folder's contents a second time with an empty query.
 *
 * Two requests to render a settings screen, for data nobody asked to see, is the
 * wrong shape for this product. Lumine is a voice assistant: the library is
 * something she *searches*, not something a person scrolls. So nothing here
 * fetches a track list, and nothing on the music path draws one.
 *
 * What is left is the only part that genuinely needs the folder: **granting and
 * revoking access to it**, which is a permission question and now lives on the
 * Permissions screen rather than on a Music tab of its own.
 *
 * ## The count is still real, and still costs a subprocess
 *
 * `inspectMusicFolder` remains, because a mistargeted folder is silent — point
 * Lumine at `D:\Music` when the music is in `D:\Music\Library` and every request
 * answers "I couldn't find that". The count is the only place that mistake is
 * visible, and it is paid **once, on the screen where the folder is chosen**,
 * never on the home page.
 *
 * ## Cancelling is not a failure
 *
 * `pickMusicFolder` resolves to `null` when the dialog is dismissed. That is a
 * decision, not an error, and the page has nothing to report — which is why this
 * returns a value rather than rejecting, and why `null` is a first-class answer
 * here rather than an empty object the page has to notice.
 */

import { invoke } from "@tauri-apps/api/core";
import { saveConfig, getConfig } from "./aiConfigClient";
import type { ConfigDocument, MusicSettings } from "./aiConfigTypes";

/** One subfolder, as a playlist. A playlist needs its count to be choosable. */
export type MusicPlaylist = {
  name: string;
  tracks: number;
};

/**
 * What is in a chosen folder — **as a number, not a list**.
 *
 * The `tracks` field that used to be here is gone, along with `MusicHit` and
 * `MusicSearch`: three types that existed only to move filenames into React state
 * nobody reads. What survives is what answers "did I point this at the right
 * place?", which is the count, the folder names, and the formats.
 */
export type MusicFolder = {
  /** The full path. Stored in the document; never spoken aloud. */
  path: string;
  /** The folder's own name — what the screen leads with. */
  name: string;
  trackCount: number;
  folderCount: number;
  /** Subfolders with their counts. */
  folders: MusicPlaylist[];
  /** Extension to how many tracks, so the screen can name the formats. */
  formats: [string, number][];
  allowed: string[];
  /** False only when the folder itself could not be read. An empty folder is `true` with zero. */
  ok: boolean;
  error: string | null;
};

/**
 * Open the native folder picker.
 *
 * Resolves to `null` when the person cancelled.
 */
export function pickMusicFolder(): Promise<MusicFolder | null> {
  return invoke<MusicFolder | null>("pick_music_folder");
}

/**
 * Count what is in a folder the document already names.
 *
 * Separate from the picker because a folder changes after it is chosen — filled,
 * emptied, moved, or renamed — and a stale count shown as current is worse than
 * no count at all.
 */
export function inspectMusicFolder(path: string, recursive = true): Promise<MusicFolder> {
  return invoke<MusicFolder>("inspect_music_folder", { path, recursive });
}

/**
 * Queue a folder's tracks, in order, into the user's own media player.
 *
 * The Music **widget's** button, and the reason it lives here rather than in the
 * voice path is that it is a *different process*: the widget runs in the app and
 * the worker's tools are not callable from there. What is shared is the queue
 * builder — `agent/media_queue.py` calls the same `tools.media.queue.build` the
 * `play_media` tool does — because a widget that queued differently from the
 * voice path would be the same class of bug as a search box that ranked
 * differently from the matcher.
 *
 * One hand-over, not one launch per track. That distinction is the whole reason
 * "play my vocal songs" is audible now instead of nine players over each other.
 */
export function queueMusic(folder = "", recursive = true): Promise<MusicQueue> {
  return invoke<MusicQueue>("queue_music", { folder, recursive });
}

/** What a queue actually contains, and where it went. */
export type MusicQueue = {
  ok: boolean;
  /** How many tracks are in the queue. */
  queued: number;
  /** The folder it was built from. */
  folder: string;
  /** True only when the hand-over succeeded. */
  error: string | null;
  /**
   * Where the queue went, in the user's own words.
   *
   * Carried rather than written by the widget, because the widget does not know
   * it: skip and stop are the *player's* controls over its own list, and a button
   * that said "playing" while the user hunted for the window would be lying.
   */
  note: string;
  /** The first track, as a name. Never the whole queue — a list is not a summary. */
  first: string;
};

/**
 * Write the `media` section, keeping every other key in the document.
 *
 * A whole-document write rather than a patch, because `save_config` is the only
 * writer and it replaces the file. Reading it back first is what stops this from
 * silently dropping a profile or a credential someone set on another screen — the
 * failure that would look like "settings keep reverting".
 */
export async function saveMusicSettings(patch: MusicSettings): Promise<ConfigDocument> {
  const { document } = await getConfig();
  // Replaced rather than merged: a key the caller left out is one they are
  // clearing, and merging would make "switch it off" impossible to express.
  const next: ConfigDocument = { ...document, media: patch };
  await saveConfig(next);
  return next;
}

/**
 * What the screen should say about a library.
 *
 * Pure, and separate from the component, because the interesting cases are all
 * about *which* sentence to use and every one of them was a bug first: an
 * unconfigured library and a mis-pointed one are both "she will not find
 * anything", and they need different advice.
 */
export function describeLibrary(
  settings: MusicSettings | undefined,
  folder: MusicFolder | null,
  busy: boolean,
): { tone: "off" | "ready" | "empty" | "error" | "loading"; text: string } {
  if (busy) return { tone: "loading", text: "Looking inside that folder…" };

  const library = (settings?.library ?? "").trim();
  if (!library) {
    return {
      tone: "off",
      text: "Lumine cannot see any music folder yet. Pick one and she will.",
    };
  }

  if (folder && !folder.ok) {
    return {
      tone: "error",
      text: folder.error ?? "That folder could not be read.",
    };
  }

  const on = settings?.enabled !== "off";
  if (!folder) {
    return on
      ? { tone: "ready", text: `Lumine will play from ${folderNameOf(library)}.` }
      : { tone: "off", text: `${folderNameOf(library)} is chosen, but music is switched off.` };
  }

  // Both problems are reported together when both are true, because they have
  // different fixes and a person who is only told one will fix that one and see
  // nothing change.
  const offNote = on ? "" : " Music is also switched off, so she will not play anything yet.";

  if (folder.trackCount === 0) {
    return {
      tone: on ? "empty" : "off",
      text: `Nothing playable in ${folder.name}. Lumine looks for ${folder.allowed.join(", ")}.${offNote}`,
    };
  }

  const shape = `${folder.trackCount} track${folder.trackCount === 1 ? "" : "s"}`;
  const playlists =
    folder.folderCount > 0
      ? `, in ${folder.folderCount} folder${folder.folderCount === 1 ? "" : "s"}`
      : "";
  const formats = formatList(folder.formats);
  return {
    tone: on ? "ready" : "off",
    text: `${shape}${playlists}${formats}.${offNote}`,
  };
}

/**
 * The folder's own name, from a path string alone.
 *
 * **No subprocess, and that is the point.** The Music widget needs to say which
 * folder Lumine has permission for, and it needs to say it the instant the home
 * page appears. Asking the worker for that name costs a Python interpreter on
 * every mount, which is the loading this file was rewritten to remove — and the
 * last path segment is already in the string the document gave us.
 */
export function folderNameOf(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

/**
 * The formats as a short clause, biggest first.
 *
 * Capped at three, and it says so when there are more: "mp3, flac and 4 more" is
 * a fact a person can act on, and a list of nine extensions is not.
 */
function formatList(formats: [string, number][]): string {
  if (formats.length === 0) return "";
  const sorted = [...formats].sort((left, right) => right[1] - left[1]);
  const shown = sorted.slice(0, 3).map(([extension]) => extension.replace(/^\./, "").toUpperCase());
  const rest = sorted.length - shown.length;
  const named = shown.length === 1 ? shown[0] : `${shown.slice(0, -1).join(", ")} and ${shown[shown.length - 1]}`;
  return rest > 0 ? ` — ${named} and ${rest} more` : ` — ${named}`;
}