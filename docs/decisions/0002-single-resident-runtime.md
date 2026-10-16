# 0002 — One resident runtime process, with the island as its own entry point

- **Status:** Proposed (Phase 1 must confirm the process split before building)
- **Date:** 2026-10-05

## Context

[0001](0001-companion-not-application.md) requires a runtime that outlives the
dashboard. That leaves two shapes:

**(A) The existing Tauri binary becomes the runtime.** It gains a tray icon, an
autostart entry, a hidden-at-boot start, and a second window for the island. It
already owns `agent_manager`, the settings store, the keyring, the credential
injection and the `LUMINE_EVENT` bridge, so Phase 1 would reuse nearly all of it.

**(B) A new Rust runtime binary** owns wake, lifecycle, the agent manager and the
island. The Tauri app becomes one more client of it, or is not started at all
during ordinary use.

(A) is cheaper and honest about how small this product is. (B) is the cleaner
separation, and it is what the vision's layer diagram literally describes.

The uncomfortable part of (A) is that Tauri creates a webview for every window it
builds, including a hidden one. A dashboard webview kept alive at boot to host a
tray icon is exactly the "permanently open Electron/Tauri application" the vision
refuses — paid for in memory for the sake of a 40-pixel island.

## Decision

**One resident runtime process owns layers 0–3 and 9a. The island is a separate
webview with its own entry point and its own bundle entry — never the dashboard's
React tree.**

The process split is explicitly *undecided* and is the first item in the Phase 1
plan. What is decided:

- **One** resident process, not two supervisors watching each other.
- The island **must not import** dashboard modules. If a future import from
  `pages/` or `features/settings` appears in the island, the boundary
  has been crossed. (`FocusPage` was also named here; the Focus workspace has
  since been removed in full.)
- The island carries its own small state-to-visual `Record` table rather than
  reusing `pages/home/Presence.tsx`, so adding a lifecycle state fails the island's
  build instead of silently rendering the wrong face.
- Audio in the island follows the precedent `audioRegistry.ts` once set: one owner
  for the process, no React in the module that is on the wake path. That module has
  since been removed with the Focus workspace, so the island must establish the
  pattern anew rather than import it.

## Why not the alternative

Rejected for now: the island as a second window of the dashboard's React
application, or the wake service implemented in the webview.

- The dashboard bundle is large and its first chunk would be the island's first
  paint, on the one path where latency is measured in milliseconds.
- A hidden webview kept alive at boot is a permanent cost for a tray icon.
- Wake detection in a webview is unreliable across sleep/wake and machine
  transitions, and it cannot survive the renderer being recycled.

## Consequences

- The Tauri binary probably becomes the runtime. If measurement during Phase 1
  shows the idle dashboard webview costs more than the island's whole budget, this
  record is superseded by a separate runtime binary — and that is a *good* outcome,
  discovered by measurement rather than by argument.
- The dashboard must keep working when it is not the process owner. That is a real
  constraint on `agent_manager` ownership.
- `agent_manager.rs` moves conceptually from "the app's worker child" to "the
  runtime's worker child". No behaviour change required; the ownership boundary
  does.

## Revisit when

Phase 1 measures idle resident memory and wake-to-first-pixel latency with the
dashboard webview alive versus not alive.