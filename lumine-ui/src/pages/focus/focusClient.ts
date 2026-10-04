import { invoke } from "@tauri-apps/api/core";
import type { FocusDocument } from "./types";
import type { FocusStats } from "./focusStatsContract";

/**
 * Every Tauri call the Focus Workspace makes.
 *
 * Modelled on `features/settings/aiConfigClient.ts`, including the in-flight
 * deduplication, and for the same reason: `main.tsx` mounts inside
 * `React.StrictMode` and React 19 runs a mount effect twice in development *on
 * purpose*. The first `load_focus` is still running when the second starts and is
 * then thrown away, so without this the app launches, reads the document twice, and
 * throws one answer away.
 *
 * ## These are not the settings client's commands
 *
 * The AI Control Center's commands are a different module with a different
 * document and a different lifecycle. Sharing a client would mean the Focus page
 * imports the whole AI surface to read a pomodoro, and a change to the in-flight map
 * would silently affect voice-adjacent reads. Two small clients is the smaller
 * coupling.
 */

/** Thrown outside the desktop shell. Named so a screen can say so rather than hang. */
export class DesktopUnavailableError extends Error {
  constructor() {
    super("The Focus workspace needs the Lumine desktop app. Run `npm run tauri dev`.");
    this.name = "DesktopUnavailableError";
  }
}

function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/**
 * One in-flight promise per command, shared by whoever asks at the same time.
 *
 * Deduplication, **not** caching: the entry is removed the moment the promise
 * settles, so nothing here can ever hand back a stale document. The desktop side
 * owns the per-process cache that does that job.
 */
const inFlight = new Map<string, Promise<unknown>>();

function requestKey(command: string, args?: Record<string, unknown>): string {
  if (!args) return command;
  const entries = Object.entries(args)
    .filter(([, value]) => value !== undefined)
    .sort(([left], [right]) => left.localeCompare(right));
  return entries.length === 0 ? command : `${command}:${JSON.stringify(entries)}`;
}

async function call<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  if (!isTauri()) throw new DesktopUnavailableError();
  const key = requestKey(command, args);
  const running = inFlight.get(key);
  if (running) return running as Promise<T>;

  const promise = invoke<T>(command, args);
  inFlight.set(key, promise);
  try {
    return await promise;
  } finally {
    // Cleared only if it is still ours. A later caller may already have replaced
    // the slot, and deleting that would un-share the request it is waiting on.
    if (inFlight.get(key) === promise) inFlight.delete(key);
  }
}

/**
 * Read the focus document, or receive a shipped default when there is none.
 *
 * A first visit is not an error, so Rust answers with `default_document()` rather
 * than a null the frontend would have to turn into one. What it will *not* do is
 * repair a document it cannot parse: that arrives as a rejection carrying a sentence,
 * and `focusStore` surfaces it.
 */
export function loadFocus(): Promise<FocusDocument> {
  return call<FocusDocument>("load_focus");
}

/**
 * Persist the document. Resolves only after Rust has read it back.
 *
 * The read-back is why this returns `Promise<void>` and not `Promise<boolean>`:
 * Rust does not report success until `load` has accepted the file, so a resolve here
 * means the bytes are genuinely on disk.
 */
export function saveFocus(document: FocusDocument): Promise<void> {
  return call<void>("save_focus", { document });
}

/**
 * Pre-aggregated statistics, for callers that do not hold the document.
 *
 * `utcOffsetMinutes` is east-positive and the caller passes
 * `eastPositiveOffsetMinutes()`. Rust defaults it to `0` rather than guessing — a
 * forgotten argument then reads UTC, which is a visible day shift rather than a
 * number that is quietly plausible.
 */
export function fetchFocusStats(days = 7, subject = ""): Promise<FocusStats> {
  return call<FocusStats>("focus_stats", {
    days,
    utcOffsetMinutes: eastPositiveOffset(),
    ...(subject ? { subject } : {}),
  });
}

/**
 * This machine's offset east of UTC.
 *
 * Read once per module load rather than per call so every bucket in one report uses
 * the same offset: sampling per row could put two sessions either side of a DST
 * boundary into two different days *within one response*.
 */
let cachedOffset: number | null = null;
function eastPositiveOffset(): number {
  if (cachedOffset === null) cachedOffset = -new Date().getTimezoneOffset();
  return cachedOffset;
}

/**
 * Authorise one folder for in-app playback, revoking the previous one.
 *
 * Returns `true` on success. The command validates, denies the old root and then
 * allows the new one — in that order — so a failed switch cannot leave the previous
 * folder readable. See `focus_commands.rs` for why that sequence is the security
 * boundary rather than a detail.
 */
export function allowMusicRoot(path: string): Promise<boolean> {
  return call<boolean>("allow_music_root", { path });
}

/** Withdraw playback access. Separate from `allowMusicRoot` so it can stand alone. */
export function revokeMusicRoot(): Promise<boolean> {
  return call<boolean>("revoke_music_root");
}

/** The authorised folder, or null. A read, so the UI can say "none chosen". */
export function grantedMusicRoot(): Promise<string | null> {
  return call<string | null>("granted_music_root");
}

/**
 * Check one track against the library and return its validated absolute path.
 *
 * The path comes back rather than a URL: `convertFileSrc` is pure string encoding
 * with no check in it, so the decision about *which* files may be handed to an
 * `<audio>` element has to be made in Rust where it can be tested. Returning a URL
 * from here would mean two places that know Tauri's URL shape, and the frontend's
 * copy would be the one that drifted.
 */
export function resolveFocusTrack(path: string, library: string): Promise<string> {
  return call<string>("resolve_focus_track", { path, library });
}