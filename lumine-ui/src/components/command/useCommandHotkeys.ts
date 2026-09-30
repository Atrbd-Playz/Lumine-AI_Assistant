import { useEffect, useRef } from "react";
import { eventBinding, type Command } from "./commands";

/**
 * Global keyboard shortcuts for commands.
 *
 * ## One listener, one map
 *
 * Every command that declares `keys` is bound here, from a single
 * `keydown` listener on `window`. The obvious alternative — a `useEffect` per
 * command — attaches N listeners, and N listeners each doing its own
 * normalisation is how two shortcuts end up on the same chord with the second one
 * silently eating the first one's `preventDefault`. Building the map up front
 * also makes a duplicate binding a *last one wins* at map-build time, which is at
 * least visible in the source, rather than a race between two handlers.
 *
 * ## Why the listener is on `window` and not `document`
 *
 * Because the app runs in a Tauri webview, focus can be anywhere — including on
 * the `<body>` while a webview child has focus, and including inside the palette
 * portal, which is *not* a descendant of the app root. A listener bound to the
 * root component would silently stop working the moment the palette opened, which
 * is the one moment a global shortcut is most likely to be pressed again.
 *
 * ## The rule about text fields
 *
 * A binding with no modifier is ignored when the event came from an editable
 * element; a binding *with* a modifier still fires. Both halves matter:
 *
 * - Without the first half, pressing <kbd>M</kbd> to mute while the composer has
 *   focus types a letter and mutes the microphone.
 * - Without the second half, <kbd>⌘K</kbd> would not open the palette from the
 *   composer, which is where a user most plausibly reaches for it. The same is
 *   true of every ⌘-chord in the app: a text field must never be able to swallow
 *   a command chord, because the modifier is unambiguous about intent.
 *
 * That is the whole reason the binding format distinguishes `mod` from a bare
 * key: without it, "don't fire in a text field" and "fire everywhere" cannot both
 * be true.
 */

type Options = {
  commands: readonly Command[];
  /** Turns every shortcut off without unmounting — used while a modal owns the keyboard. */
  enabled?: boolean;
  /** Notified before a command runs, so a caller can close its own surface first. */
  onBeforeRun?: (command: Command) => void;
  /** Notified when a binding matched but the command is unavailable. */
  onBlocked?: (command: Command) => void;
};

/** True for anything that consumes plain keystrokes. */
function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

export function useCommandHotkeys({ commands, enabled = true, onBeforeRun, onBlocked }: Options) {
  // Held in refs so that changing a command's `run` — which happens on every
  // render, because the commands are built from live state like `muted` — does
  // not re-subscribe. Re-subscribing on every render would drop and re-add the
  // listener sixty times a second during a call, and any keystroke landing in
  // that window would be lost.
  const commandsRef = useRef(commands);
  commandsRef.current = commands;
  const beforeRef = useRef(onBeforeRun);
  beforeRef.current = onBeforeRun;
  const blockedRef = useRef(onBlocked);
  blockedRef.current = onBlocked;

  useEffect(() => {
    if (!enabled) return;

    const onKeyDown = (event: KeyboardEvent) => {
      // A held key fires `keydown` on repeat, and a command that starts a call
      // would then be started sixty times a second.
      if (event.repeat) return;
      const binding = eventBinding(event);
      if (!binding) return;

      const index = commandsRef.current.findIndex((command) => command.keys?.includes(binding));
      if (index < 0) return;
      const command = commandsRef.current[index];

      // A bare key never steals a keystroke from a text field. See the note
      // above; this is the half that is easy to forget.
      if (!binding.includes("+") && isEditable(event.target)) return;

      // Claimed regardless of availability, so an unavailable command's chord
      // does not also trigger the browser's own action for it. <kbd>⌘D</kbd> is
      // "bookmark this page" in a webview; swallowing the press and saying
      // nothing is better than both happening.
      event.preventDefault();
      if (command.available === false) {
        blockedRef.current?.(command);
        return;
      }
      beforeRef.current?.(command);
      command.run();
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}

/**
 * A stable, platform-correct display string for a `mod+…` binding.
 *
 * Exported rather than inlined so the palette's footer, a settings row and a
 * future tooltip cannot disagree about whether this machine's ⌘ is a ⌘. Writing
 * "⌘K" as a literal is the bug this exists to prevent: it is right on macOS and
 * actively misleading on Windows and Linux.
 */
export function describeBinding(binding: string): string {
  const apple = /mac|iphone|ipad|ipod/i.test(navigator.platform);
  return binding
    .split("+")
    .map((part) => {
      switch (part) {
        case "mod":
          return apple ? "⌘" : "Ctrl";
        case "alt":
          return apple ? "⌥" : "Alt";
        case "shift":
          return apple ? "⇧" : "Shift";
        case "ctrl":
          return apple ? "⌃" : "Ctrl";
        case "meta":
          return apple ? "⌘" : "Win";
        case "enter":
          return "↵";
        case "escape":
          return "esc";
        case "arrowup":
          return "↑";
        case "arrowdown":
          return "↓";
        default:
          return part.length === 1 ? part.toUpperCase() : part;
      }
    })
    .join(apple ? "" : "+");
}
