/**
 * Every shape in the Focus Workspace document.
 *
 * ## Three copies of this schema exist, and that is a known risk
 *
 * | Where | Role |
 * | --- | --- |
 * | `src-tauri/src/focus_store.rs` | The writer, and the validator that refuses a bad document |
 * | `agent/focus_store.py` | The reader the worker will eventually use |
 * | this file | What the webview believes it is holding |
 *
 * A fourth copy — the *statistics* contract — lives in `focusStatsContract.ts`,
 * because it is a different thing with different consumers and deserves its own
 * file rather than a second half of this one.
 *
 * Three hand-written copies is a real risk and the mitigation is not a comment:
 * `scripts/verify-focus-contract.mts` reads the Rust and Python shapes and fails if
 * they and this one disagree about which keys exist. A field added here without
 * being added there is a red check, not a silent divergence.
 *
 * ## Every optional field is `| null`, never `?`
 *
 * `null` means "the document says it has no value". An absent key means the document
 * is out of date and the reader has to decide what to do. Collapsing those two cases
 * with `?.` is how "the person has no subject" and "this file is from an older
 * version" become the same value, and the second one then reads as data loss on the
 * screen.
 */

/** The schema version this build writes. Mirrors `focus_store::FOCUS_VERSION`. */
export const FOCUS_VERSION = 1;

/** Which board preset is active. `custom` means the geometry has been moved. */
export type LayoutPreset = "default" | "study" | "writing" | "deepWork" | "custom";

/**
 * The legal values, as a value rather than as prose.
 *
 * `focus_store.rs` holds the same five names in `LAYOUT_PRESETS` and *refuses a save*
 * carrying anything else — including `custom`, which is a perfectly ordinary value
 * once a person has dragged a widget. Exported so the settings control and the
 * verification script read one list instead of two.
 */
export const LAYOUT_PRESETS: readonly LayoutPreset[] = [
  "default",
  "study",
  "writing",
  "deepWork",
  "custom",
];

/** The pomodoro phase. Not a display string — `constants` hold the wording. */
export type FocusPhase = "work" | "short" | "long";

export type FocusSettings = {
  workMin: number;
  shortMin: number;
  longMin: number;
  /** Rounds of work before a long break. Four is what the method is built on. */
  longEvery: number;
  autoStart: boolean;
  autoAdvance: boolean;
  dailyGoalMin: number;
  ambientSound: string;
  notifications: boolean;
  /**
   * Which preset is selected — **not** where the widgets are.
   *
   * The original proposal had `"layout": "default" | "saved"` here, which contradicts
   * itself the moment somebody moves a widget: there is no value that means "the
   * person has customised it" *and* keeps the record of which preset they started
   * from. Splitting it is the whole fix. `settings.layoutPreset` names the preset,
   * and `widgets[]` always carries the real geometry, so switching presets is a
   * write to this field and a read of the preset table — never a migration.
   */
  layoutPreset: LayoutPreset;
};

/**
 * One widget's place on the board, in **grid units**, never pixels.
 *
 * `x`/`y` are cell coordinates and `w`/`h` are spans on a four-column grid. Pixels
 * would tie the layout to one window size, and the board already has two layouts —
 * desktop and compact — which cannot both be expressed in pixels.
 */
export type WidgetPlacement = {
  id: WidgetId;
  x: number;
  y: number;
  /** 1..4 on the four-column grid. Enforced in Rust and by `useWidgetLayout`. */
  w: number;
  /** 1..6. Tall enough for the notes editor, short enough to keep the board on one screen. */
  h: number;
  /**
   * Hidden rather than absent.
   *
   * A removed widget loses its geometry, so restoring it drops the person back
   * somewhere they did not choose. Keeping the row and setting a flag means
   * "hide" and "delete this widget's placement" are different acts, which they are.
   * Nothing underneath is ever destroyed by hiding.
   */
  hidden: boolean;
};

/** Every widget the board can hold. Closed union so a typo is a compile error. */
export type WidgetId =
  | "timer"
  | "todos"
  | "notes"
  | "music"
  | "charts"
  | "intentions"
  | "quickadd";

export type FocusTodo = {
  id: string;
  title: string;
  done: boolean;
  /** Epoch millis, or null for "no due date". */
  dueAt: number | null;
  /** How many pomodoros the person thinks this will take. null means unestimated. */
  estimatePomodoros: number | null;
  subject: string | null;
  /** Manual sort position. Ascending. Ties are broken by `createdAt`. */
  order: number;
  createdAt: number;
  completedAt: number | null;
};

export type FocusNote = {
  id: string;
  title: string;
  body: string;
  subject: string | null;
  pinned: boolean;
  createdAt: number;
  updatedAt: number;
};

/**
 * One completed or interrupted pomodoro. **Append-only.**
 *
 * This is the only thing the charts read, and it is deliberately *not* derived from
 * todos or notes. A todo that is renamed, completed or deleted a month later must
 * not retroactively change what a chart said on the day. That is the whole reason
 * history is a separate row type rather than a join.
 */
export type FocusSession = {
  id: string;
  startedAt: number;
  endedAt: number;
  plannedMin: number;
  /**
   * Minutes actually elapsed, which is not `plannedMin` when a session was paused,
   * abandoned, or expired while the app was closed.
   */
  actualMin: number;
  phase: FocusPhase;
  /** null is a real value: the person studied without naming a subject. */
  subject: string | null;
  /** The todo in progress, *if any*. A dangling id is tolerated on read. */
  todoId: string | null;
  noteId: string | null;
  completed: boolean;
  interruptions: number;
};

export type CustomListItem = { id: string; text: string; done: boolean };
export type CustomList = { id: string; name: string; items: CustomListItem[] };

/** One day's intentions. Capped at three per day, in `focus_store.rs`. */
export type DayIntention = {
  /** Local `YYYY-MM-DD`. A string key, so no timezone maths is needed to group by it. */
  date: string;
  text: string;
  done: boolean;
};

/**
 * The timer needs no recovery record here, and that is a decision.
 *
 * A running timer has to survive an abrupt kill, and `beforeunload` cannot await a
 * Tauri `invoke`. So the *timer* keeps its own small record in `localStorage`,
 * written synchronously — see `timer.ts` in Slice 2. It is deliberately **not** part
 * of this document: a 200-byte durability record does not belong in a file that is
 * also written on a 250 ms debounce, and mixing the two would mean the timer's
 * survival depends on the bulk save finishing.
 */
export type FocusMusic = {
  youtube: { current: string | null; queue: string[]; volume: number };
  local: { queue: string[]; index: number };
};

export type FocusDocument = {
  version: number;
  settings: FocusSettings;
  widgets: WidgetPlacement[];
  todos: FocusTodo[];
  notes: FocusNote[];
  sessions: FocusSession[];
  customLists: CustomList[];
  music: FocusMusic;
  dayIntents: DayIntention[];
};

/**
 * What the store is doing, for a status line and for `beforeunload` handling.
 *
 * `unavailable` is the state outside the Tauri shell, and it is distinct from
 * `error`: there is nothing wrong, there is simply no desktop to read from, and a
 * page that shows a red banner for "you opened this in a browser" is lying about
 * a fault it cannot have.
 */
export type FocusStatus =
  | "loading"
  | "ready"
  | "saving"
  | "error"
  | "unavailable";

export type FocusSnapshot = {
  status: FocusStatus;
  /** The last error, or null. Kept after a successful save so the UI can explain itself. */
  error: string | null;
  /** True while a save is queued or in flight. */
  dirty: boolean;
  document: FocusDocument;
};

/** A row the selectors read. Fields are already narrowed by the loader. */
export type DayTotal = { date: string; focusMinutes: number; sessions: number };
export type SubjectTotal = { subject: string; focusMinutes: number; sessions: number };

/** The shipped defaults. Mirrors `focus_store::default_document` exactly. */
export function defaultSettings(): FocusSettings {
  return {
    workMin: 25,
    shortMin: 5,
    longMin: 15,
    longEvery: 4,
    // Auto-start ships **off**, and the reason is worth recording because the
    // original proposal said on. A timer that starts itself the instant a break
    // ends is a timer that begins counting while the person is still walking to the
    // kettle. It is a preference, and the wrong default is worse than no default.
    autoStart: false,
    autoAdvance: true,
    dailyGoalMin: 120,
    ambientSound: "none",
    notifications: true,
    layoutPreset: "default",
  };
}

export function defaultDocument(): FocusDocument {
  return {
    version: FOCUS_VERSION,
    settings: defaultSettings(),
    widgets: [
      { id: "intentions", x: 0, y: 0, w: 4, h: 1, hidden: false },
      { id: "timer", x: 0, y: 1, w: 2, h: 3, hidden: false },
      { id: "todos", x: 2, y: 1, w: 2, h: 3, hidden: false },
      { id: "notes", x: 0, y: 4, w: 2, h: 3, hidden: false },
      { id: "music", x: 2, y: 4, w: 2, h: 2, hidden: false },
      { id: "charts", x: 2, y: 6, w: 2, h: 2, hidden: false },
    ],
    todos: [],
    notes: [],
    sessions: [],
    customLists: [],
    music: {
      youtube: { current: null, queue: [], volume: 0.6 },
      local: { queue: [], index: -1 },
    },
    dayIntents: [],
  };
}