# 0006 — The island is the primary voice interaction surface

- **Status:** Accepted
- **Date:** 2026-10-05

## Context

The vision names a top-centre island/notch surface that appears on wake and retracts
when the interaction ends, and states that it should become **one of Lumine's
primary interaction surfaces**. It also forbids copying another project's identity
— [coucou](https://github.com/louis-cfm/coucou) is an architectural and
interaction-model reference only. No character, asset, branding or visual identity
is taken from it.

Nothing of the sort exists: the app declares a single ordinary window in
`tauri.conf.json`, the frontend imports no Tauri window API at all, and there is no
second window, no tray, and no always-on-top or transparent capability.

The design risk is not building the island. It is building it as a *second copy of
the full UI* — a small window that imports the dashboard, so it inherits its
bundle, its state vocabulary, and every rule that only makes sense when a human is
sitting in front of a settings page.

## Decision

**The island is the primary surface for voice interaction. It renders the lifecycle,
the tool progress, and the permission request. It has its own entry point, its own
small bundle, and its own state-to-visual tables.**

1. **Its own entry point**, never the dashboard's React tree. An import from
   `pages/` into the island is a boundary violation.
2. **It is not click-through when it matters.** Acknowledgement is visible;
   click-through applies only when it is genuinely idle.
3. **It never takes focus on appearing.** Stealing focus from a user mid-sentence is
   the single worst thing this surface could do.
4. **It renders permission requests**, and it renders the *same sentence the gate
   speaks*. `permission_request.question` already exists and is already the exact
   string the voice layer uses — two wordings would let someone approve one action
   and believe they had approved another.
5. **It has one job per state**, from a total `Record`, so a new lifecycle state
   fails its build rather than rendering the wrong face.
6. **It ends.** Retract on idle timeout, on sleep, and on dismiss. It is not a
   permanent window that happens to be small.
7. **It ducks what the user is listening to** — the behaviour `useVoiceDucking.ts`
   once implemented for the Focus workspace's player, since removed — and can be
   muted by the same policy that mutes her voice.

## Why not the alternative

Rejected: put voice interaction in the full UI and treat the island as decoration.
That makes the full UI a prerequisite, which is precisely what
[0001](0001-companion-not-application.md) refuses, and it means a permission
question appears in a window the user did not open.

Rejected: a webview-based island with no visual affordances at all (audio only).
Status legibility is a feature — "is she listening?" is answered faster by a face
than by a status bar, and the avatar system already renders states.

## Consequences

- **[built] and reusable for free:** the permission card payload (`id`, `tool`,
  `action`, `action_class`, `summary`, `target`, `question`, `expires_at`), the
  `lumine.tool` progress records with `index`/`total`, and `voice_line.speak_line`
  for acknowledging a wake without the model. The island is largely a presentation
  change.
- **[built] and reusable:** the avatar engine and its emotion/activity tables.
- New capability needed: a second Tauri window with `transparent`,
  `alwaysOnTop`, `decorations: false`, `skipTaskbar`, `visible: false`, plus its own
  capability entry. None of those exist today.
- New capability needed: a dedicated CSS layer that is not the dashboard's. The
  existing lesson applies — a Tailwind utility silently outranking `index.css` is
  why `.presence` needed an explicit comment.
- A guard test is warranted: assert the island's bundle does not import dashboard
  modules. Cheap, and the failure mode is a slow island forever.

## Revisit when

The island has to do something a 600px surface cannot do well — which, so far, it
has not.