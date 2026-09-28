/**
 * Who owns Lumine, in one place.
 *
 * The Tauri side already declares this: `productName`, `publisher` and
 * `copyright` in `tauri.conf.json`, `authors` in `Cargo.toml`, `author` in
 * `package.json`. Those drive the installed binary, the taskbar entry and the
 * installer's licence page. This module is the same claim for the part of the
 * product those files cannot reach — the About screen.
 *
 * ## Why it is a literal and not a read of `tauri.conf.json`
 *
 * `tauri.conf.json` is compile-time input. Reading it at runtime would mean the
 * About screen reports the *built* values, so anyone running `tauri dev` sees
 * whatever the last release was called rather than what they are looking at.
 * A literal cannot go stale relative to the running code.
 *
 * The values that must agree across all four places, and do:
 *
 * | Claim           | tauri.conf.json        | Cargo.toml  | package.json | here        |
 * | --------------- | ---------------------- | ----------- | ------------ | ----------- |
 * | Name            | `productName`          | `name`      | `name`       | `name`      |
 * | Owner           | `bundle.publisher`     | `authors`   | `author`     | `author`    |
 * | Copyright       | `bundle.copyright`     | —           | —            | `copyright` |
 * | Description     | `bundle.longDescription` | —        | `description`| `description` |
 */
export const APP = {
  /** Product name. Matches `productName` and the Cargo package. */
  name: "Lumine",
  /** The one-line claim shown under the mark. */
  tagline: "A local-first AI companion.",
  /**
   * The app's own statement of ownership. This is the string the user asked to
   * be present wherever the product identifies itself.
   */
  author: "Abdur Rahman Toha",
  copyright: "© 2026 Abdur Rahman Toha. Lumine is his app. All rights reserved.",
  license: "All rights reserved. Not published or distributed.",
  /**
   * Matches `version` in both `tauri.conf.json` and `Cargo.toml`.
   */
  version: "0.1.0",
  description:
    "Lumine is a local-first AI companion by Abdur Rahman Toha. Voice, memory and tools run on your own machine rather than a server he operates: the LiveKit worker, the model providers and the credential store are all yours to point somewhere else.",
} as const;
