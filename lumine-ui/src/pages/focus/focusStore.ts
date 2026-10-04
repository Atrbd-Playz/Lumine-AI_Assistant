import { DesktopUnavailableError, loadFocus, saveFocus } from "./focusClient";
import { defaultDocument, FOCUS_VERSION } from "./types";
import type { FocusDocument, FocusSnapshot } from "./types";

/**
 * The single writer of the Focus Workspace document.
 *
 * ## Why a module singleton rather than a hook
 *
 * `pages/home/notes/useNotes.ts` documents the failure this avoids: two surfaces for
 * the same notes, each with its own copy of the read-write block, and the user wins
 * or loses a sentence depending on which one they typed in last. On the Focus page
 * the problem is much larger — a timer, a todo list, a music queue and a notes editor
 * are *all* writing one document, and each of them holding its own copy would mean
 * the last widget to save wins and everybody else's edit is gone.
 *
 * So there is one snapshot object, one mutation path, and `useSyncExternalStore` for
 * subscribers. Every widget reads the same object identity, which is also what makes
 * "avoid unnecessary rerenders" achievable at all: a component that selects a slice
 * only rerenders when its own slice changes.
 *
 * ## Two persistence paths, and why that is not a shortcut
 *
 * | What | Where | Why there |
 * | --- | --- | --- |
 * | The bulk document | `focus.json` via Rust | Large, written often, survives a crash atomically |
 * | Timer recovery | `localStorage`, synchronously | ~200 bytes, must survive an *abrupt kill* |
 *
 * `beforeunload` cannot await a Tauri `invoke`, and a timer that must not lose a
 * running block cannot depend on a 250 ms debounce winning a race with a window
 * close. The timer record is therefore synchronous and separate. That split is
 * documented in `timer.ts` too, because two halves of a decision in two files is how
 * one of them drifts.
 *
 * ## What happens on a failed save
 *
 * The error is kept, `status` becomes `"error"`, and the in-memory document is left
 * exactly as the person left it. Nothing is rolled back, because rolling back would
 * discard the edit they just made to fix the error. The next successful save clears
 * it. A save that fails silently is how somebody loses an hour of todos and finds out
 * a week later.
 */

const SAVE_DEBOUNCE_MS = 250;

/** Legacy notes key, read once at migration and then left alone. See `migrateNotes`. */
const LEGACY_NOTES_KEY = "lumine.notes.v1";
const MIGRATION_MARKER_KEY = "lumine.notes.migratedAt";

type Listener = () => void;

let snapshot: FocusSnapshot = {
  status: "loading",
  error: null,
  dirty: false,
  document: defaultDocument(),
};

const listeners = new Set<Listener>();

let loadStarted = false;
let saveTimer: ReturnType<typeof setTimeout> | null = null;
/** The promise for the save currently in flight, if any. */
let saveInFlight: Promise<void> | null = null;
/**
 * Set when a change arrives while a save is running.
 *
 * Without this the last edit before an await is lost: `save(document)` serialises
 * `document` at call time, so an edit made during the round trip would never be
 * written and never reported as unsaved either. The dirty flag alone does not catch
 * it, because the edit clears `dirty` when it schedules the debounce — before the
 * save it was waiting on has finished.
 */
let queuedWhileSaving = false;

// ---------------------------------------------------------------------------
// Subscription
// ---------------------------------------------------------------------------

function emit(): void {
  for (const listener of listeners) listener();
}

function setSnapshot(next: FocusSnapshot): void {
  // Identity is the whole contract: `useSyncExternalStore` compares with `Object.is`,
  // so returning a fresh object for an unchanged value rerenders the world.
  if (
    snapshot.status === next.status &&
    snapshot.error === next.error &&
    snapshot.dirty === next.dirty &&
    snapshot.document === next.document
  ) {
    return;
  }
  snapshot = next;
  emit();
}

/** Subscribe. Returns the unsubscribe, so a `useSyncExternalStore` can return it. */
export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getSnapshot(): FocusSnapshot {
  return snapshot;
}

/**
 * The document alone, for a caller that does not care about status.
 *
 * Identity is preserved between mutations that do not change the document, so this
 * is safe to use as a `useSyncExternalStore` selector.
 */
export function getDocument(): FocusDocument {
  return snapshot.document;
}

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

/**
 * Read the document. Safe to call from every widget that needs it.
 *
 * Idempotent, and deliberately *not* cached-then-forgotten: `loadStarted` is a
 * latch, not a cache, because the only thing that could invalidate the document is
 * `save_focus`, and every write in this module goes through the store anyway.
 */
export function ensureLoaded(): void {
  if (loadStarted) return;
  loadStarted = true;
  void load();
}

async function load(): Promise<void> {
  setSnapshot({ ...snapshot, status: "loading", error: null });
  try {
    const loaded = await loadFocus();
    const document = migrateNotes(loaded);
    setSnapshot({
      status: "ready",
      error: null,
      dirty: false,
      document,
    });
    // A migration is a write, and it goes through the same debounced path as any
    // other so the first paint is not blocked behind it.
    if (document !== loaded) markDirty();
  } catch (error) {
    const offline = error instanceof DesktopUnavailableError;
    setSnapshot({
      ...snapshot,
      status: offline ? "unavailable" : "error",
      // The desktop message is already the whole answer; repeating it in a toast on
      // a browser preview is noise.
      error: offline ? null : messageOf(error),
      // Deliberately keeping the default document: a page that refuses to render
      // because it could not read a file is worse than one that renders with an
      // honest "could not load" banner, and the banner is `status === "error"`.
    });
  }
}

/** Retry after a failed load. The one action the error state offers. */
export function reload(): void {
  loadStarted = false;
  ensureLoaded();
}

function messageOf(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "Your focus data could not be read.";
}

// ---------------------------------------------------------------------------
// Mutation
// ---------------------------------------------------------------------------

/**
 * Apply one change.
 *
 * The updater receives the current document and returns the next one, so a caller
 * never reads a stale copy. Everything immutable: every array and row is rebuilt
 * rather than pushed into, because `useSyncExternalStore` compares identity and a
 * mutated array would leave subscribers looking at the same object.
 */
export function update(mutate: (document: FocusDocument) => FocusDocument): void {
  ensureLoaded();
  const next = mutate(snapshot.document);
  if (next === snapshot.document) return;
  setSnapshot({ ...snapshot, document: next, dirty: true });
  markDirty();
}

/** A convenience for the common "change one setting" case. */
export function setSettings(patch: Partial<FocusDocument["settings"]>): void {
  update((document) => ({ ...document, settings: { ...document.settings, ...patch } }));
}

function markDirty(): void {
  if (saveTimer !== null) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = null;
    void flush();
  }, SAVE_DEBOUNCE_MS);
}

/**
 * Write now rather than at the end of the debounce.
 *
 * Called on `visibilitychange` and on a timer state change. The reason it cannot be
 * the *only* path is `beforeunload`: a Tauri `invoke` is asynchronous and the
 * window will be gone before it resolves. The timer does not rely on it (see the
 * module docstring), but a bulk document that is one edit from being lost is still
 * worth protecting, and this is the best available timing.
 */
export function flush(): Promise<void> {
  if (saveTimer !== null) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
  if (!snapshot.dirty) return saveInFlight ?? Promise.resolve();
  return write();
}

async function write(): Promise<void> {
  // A save already running: let it finish, then write again if anything changed.
  // Chaining rather than joining is the point — joining would return the older
  // document's success and report it as this one's.
  if (saveInFlight) {
    queuedWhileSaving = true;
    return saveInFlight;
  }

  const document = snapshot.document;
  setSnapshot({ ...snapshot, status: "saving", dirty: false });

  const attempt = saveFocus(document)
    .then(() => {
      // `...snapshot` is load-bearing: a save result replaces the *status*, not the
      // document. Omitting it would throw away everything the person just saved, and
      // it would do so at the exact moment the save succeeded.
      setSnapshot({ ...snapshot, status: "ready", error: null, dirty: snapshot.dirty });
    })
    .catch((error: unknown) => {
      // `dirty` goes back to true: the edit is still unsaved, and saying otherwise
      // is how a person closes the app believing their todos went somewhere.
      setSnapshot({ ...snapshot, status: "error", error: messageOf(error), dirty: true });
    })
    .finally(() => {
      saveInFlight = null;
      if (queuedWhileSaving) {
        queuedWhileSaving = false;
        markDirty();
      }
    });

  saveInFlight = attempt;
  return attempt;
}

/**
 * The lifecycle wiring.
 *
 * `visibilitychange` rather than `beforeunload`, because that is the event Tauri
 * reliably delivers before a window goes away, and because a handler registered on
 * `beforeunload` cannot await. Both call `flush`, which is a no-op when nothing is
 * dirty.
 *
 * Installed on first import rather than by a component, because the store has to be
 * alive before the first widget mounts — a timer left running by a user who then
 * closed the tab is exactly the case this is for.
 */
let lifecycleInstalled = false;

export function installLifecycle(): () => void {
  if (lifecycleInstalled) return () => {};
  lifecycleInstalled = true;

  const onVisibility = () => {
    if (document.visibilityState === "hidden") void flush();
  };
  // `pagehide` covers the macOS/Linux close path, where `beforeunload` may not fire.
  const onPageHide = () => void flush();

  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("pagehide", onPageHide);
  return () => {
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("pagehide", onPageHide);
    lifecycleInstalled = false;
  };
}

// ---------------------------------------------------------------------------
// Legacy notes migration
// ---------------------------------------------------------------------------

type LegacyNote = { id?: unknown; body?: unknown; updatedAt?: unknown };

/**
 * Import notes from `useNotes.ts` the first time the Focus page loads.
 *
 * ## What it does *not* do
 *
 * **It does not delete the legacy key.** That key is still the home page's notes
 * storage until Slice 2 rewires that surface, and deleting it would take the
 * existing notes with it. So the key stays, a marker records that the import
 * happened, and the second run is a no-op.
 *
 * ## Why the marker is a separate key rather than "any notes already present"
 *
 * "Already has notes" is ambiguous: a person who imported, then deleted every note
 * in Focus, would look exactly like somebody who had never imported, and their next
 * launch would import the whole legacy list again. A marker is the only thing that
 * distinguishes *never migrated* from *migrated and since emptied*.
 */
export function migrateNotes(loaded: FocusDocument): FocusDocument {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(LEGACY_NOTES_KEY);
    if (raw === null) return loaded;
    if (window.localStorage.getItem(MIGRATION_MARKER_KEY) !== null) return loaded;
  } catch {
    // A full disk, a private window, a disabled profile. The import is an
    // improvement, not a requirement, so declining it is the correct answer.
    return loaded;
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return loaded;
  }
  if (!Array.isArray(parsed)) return loaded;

  const legacy = parsed.filter(
    (row): row is LegacyNote => Boolean(row) && typeof (row as LegacyNote).body === "string",
  );
  if (legacy.length === 0) {
    markMigrationDone();
    return loaded;
  }

  const imported: FocusDocument["notes"] = legacy.map((row, index) => {
    const body = String(row.body);
    const updatedAt = Number(row.updatedAt) || Date.now();
    return {
      // A stable id for a row that may not have one. Derived from its position and
      // timestamp rather than random, so re-running a failed import produces the same
      // ids instead of duplicating every note under fresh ones.
      id: typeof row.id === "string" && row.id ? row.id : `legacy-${index}-${updatedAt}`,
      // The legacy format had no title; its first line *was* the title, which is
      // exactly the rule `NotesSurface` already used to label the list.
      title: firstLine(body),
      body,
      subject: null,
      pinned: false,
      // `createdAt` is not recoverable and is not invented: the update time is the
      // one fact the old format actually held.
      createdAt: updatedAt,
      updatedAt,
    };
  });

  markMigrationDone();
  return { ...loaded, version: FOCUS_VERSION, notes: imported };
}

function markMigrationDone(): void {
  try {
    window.localStorage.setItem(MIGRATION_MARKER_KEY, String(Date.now()));
  } catch {
    // Same reason as above: a marker we cannot write costs one duplicate import at
    // worst, which is far cheaper than refusing to open the page.
  }
}

function firstLine(body: string): string {
  const line = body.split(/\n/, 1)[0]?.trim() ?? "";
  return line === "" ? "Untitled" : line.slice(0, 46);
}