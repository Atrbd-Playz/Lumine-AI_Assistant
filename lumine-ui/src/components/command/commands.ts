import type { IconName } from "../../pages/home/types";

/**
 * A thing the palette can do.
 *
 * ## Why this is a type and a builder rather than a constant
 *
 * A `COMMANDS` array as a module constant cannot work, and the reason is
 * instructive: every command here does something to live application state — it
 * navigates, opens a dialog, toggles a setting. A module that exported the list
 * would have to import the store, the router and the voice manager, which makes
 * it impossible to render without a session, impossible to test without
 * mounting the app, and impossible to extend from a feature without editing
 * another feature's file.
 *
 * So the registry is a *builder*: `Home.tsx` passes in the actions it can
 * perform, and gets back a plain array of data. The palette knows nothing about
 * any of them, and the list is a value that can be memoised, filtered and
 * asserted on.
 *
 * ## Why a command can be unavailable
 *
 * `available` exists because the alternative is lying. "Start call" while a call
 * is already up, or "Mute" on a call that is not connected, are both things the
 * palette can offer. Hiding them makes the palette feel like it is missing
 * things; showing them as enabled makes them do nothing when pressed, which is
 * worse. A dimmed row with the reason beside it is what a well-behaved menu
 * does.
 */
export type Command = {
  /** Stable across renames. Used as the React key and by the verify script. */
  id: string;
  /** What the row says. */
  label: string;
  /** One line under the label, or the reason it is unavailable. */
  hint?: string;
  /** The group caption. Rows are grouped and never interleaved. */
  group: CommandGroup;
  icon: IconName;
  /**
   * Keyboard shortcut, in the display form the app uses: `"⌘K"`, `"Ctrl+Shift+D"`.
   *
   * A *display* string and not a parsed binding, because the palette only ever
   * shows it and never matches on it -- matching is done against the `keys`
   * array below. Deriving one from the other is a parser nobody would trust and
   * a second place for a shortcut to be wrong.
   */
  shortcut?: string;
  /**
   * Normalised key bindings that trigger this command globally.
   *
   * Lowercase `event.key` (or a named key like `"escape"`), optionally with
   * `mod+` for ⌘ on macOS and Ctrl everywhere else. A palette bound to `mod+k`
   * is one declaration that is right on both platforms, which is the only way a
   * shortcut can be correct on the machine it is typed on.
   */
  keys?: string[];
  /** False greys the row out and gives the reason. `run` is not called. */
  available?: boolean;
  /**
   * Why it is unavailable. Shown as the row's hint, and required whenever
   * `available` is false -- enforced by `buildCommands` below rather than by
   * convention.
   */
  unavailableReason?: string;
  run: () => void;
};

export type CommandGroup = "Call" | "Navigate" | "Appearance" | "Session" | "App";

/** A command whose availability was computed, so the invariant is checked once. */
type Draft = Omit<Command, "available" | "unavailableReason"> & {
  available?: boolean;
  unavailableReason?: string;
};

/**
 * Turns drafts into commands, and refuses the dishonest one.
 *
 * A command that is unavailable without a reason is the case worth catching: it
 * renders as a dimmed row that says nothing, which is exactly the "this does
 * nothing and the app will not tell me why" experience the whole `available`
 * mechanism exists to prevent.
 *
 * Throwing at module scope would be worse than useless here — a bad command
 * would take the whole app down at import — so the reason is defaulted to
 * something true instead, and the situation is made loud in development.
 */
export function buildCommands(drafts: readonly Draft[]): Command[] {
  return drafts.map((draft) => {
    if (draft.available === false && !draft.unavailableReason) {
      if (isDev()) {
        console.warn(`[CommandPalette] "${draft.id}" is unavailable with no reason; the row will say so generically.`);
      }
      return { ...draft, unavailableReason: "Not available right now." };
    }
    return draft;
  });
}

/**
 * Whether this is a development build.
 *
 * `import.meta.env?.DEV` with the optional chain, which is not defensive coding
 * for Vite — it always defines the object — but for the one context where
 * `commands.ts` is loaded outside a bundle: `scripts/verify-command-surface.mts`,
 * which imports this file directly under Node to assert the rule above. A plain
 * `import.meta.env.DEV` throws there, and the throw would be the first thing the
 * check saw, which is a confusing way to learn that a pure builder is coupled to
 * the bundler.
 */
function isDev(): boolean {
  return Boolean(import.meta.env?.DEV);
}

/**
 * How a command's haystack is built.
 *
 * The label is always included, so a command is findable by its own name. The
 * hint and the shortcut are included too, and deliberately: a user who
 * remembers "the one with the clock icon" or "the one bound to Ctrl+D" is
 * describing something real, and the palette is the one surface that can act on
 * that description.
 *
 * Kept as a function rather than inlined at the call site so the palette and
 * anything that pre-filters the list cannot disagree about what is searchable.
 */
export function commandHaystack(command: Command): string {
  return [command.label, command.hint ?? "", (command.keys ?? []).join(" "), command.group].filter(Boolean).join(" ");
}

/** Whether the platform's primary modifier is ⌘ rather than Ctrl. */
function isAppleLike(): boolean {
  // `navigator.platform` is deprecated, but it is the only synchronous answer at
  // keydown time and `userAgentData` is not available in every engine Tauri
  // ships on. A deprecated read here is cheaper than a modifier that fires the
  // wrong command on half the platforms.
  return /mac|iphone|ipad|ipod/i.test(navigator.platform);
}

/**
 * Normalise a keyboard event into the form `keys` is written in.
 *
 * `mod` resolves to ⌘ on Apple platforms and Ctrl everywhere else, so `mod+k` is
 * one declaration that is right on the machine it is typed on.
 *
 * The other modifier is kept under its *own* name. Collapsing Ctrl and ⌘ into
 * one token — which is the tempting simplification — means that on macOS a
 * literal Ctrl+K fires whatever ⌘K is bound to. That is a real shortcut
 * (Ctrl+⌘+Space among others) being silently reassigned, and it is invisible
 * until someone presses the wrong one.
 *
 * Returns `null` for a bare modifier press, so holding Shift does not fire the
 * command bound to the unmodified key.
 */
export function eventBinding(event: KeyboardEvent): string | null {
  const key = event.key.toLowerCase();
  // A lone modifier is not a chord, whatever else is held.
  if (["control", "meta", "alt", "shift"].includes(key)) return null;

  const apple = isAppleLike();
  const parts: string[] = [];
  if (apple ? event.metaKey : event.ctrlKey) parts.push("mod");
  if (apple ? event.ctrlKey : event.metaKey) parts.push(apple ? "ctrl" : "meta");
  if (event.altKey) parts.push("alt");
  if (event.shiftKey) parts.push("shift");
  parts.push(key);
  return parts.join("+");
}
