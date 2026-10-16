import { Icon } from "./Icon";

/**
 * What the desktop tool last looked at, as one line of the vision deck.
 *
 * ## Why this is a row and never a picture
 *
 * There is no picture. The desktop path reads the window's **accessibility
 * tree**, so what exists here is a name, a count and a generation number, and
 * the previous card drew them inside a 176px box with a bar and three pills —
 * the same chrome a screenshot gets, around nothing that needed it.
 *
 * It used to be worse than wasteful. This card was absolutely positioned at
 * the same `top` as the self-view and the browser preview, so all three sat in
 * one corner on top of each other, and the rule meant to separate them was
 * written for a sibling order the markup does not have (see `VisionDeck`).
 *
 * ## Why the generation is shown
 *
 * It is what the model clicks against. A user approving an action wants to know
 * which look of the screen it was planned against, and the number is the only
 * thing that answers that — "a moment ago" cannot, because two observations a
 * second apart are both "a moment ago" and only one of them has the Save button
 * in it.
 *
 * ## Why the DPI warning stays at full size
 *
 * `dpiAligned` is false when this system will not let Lumine read screen
 * positions the same way she clicks them, and the consequence is that **every
 * click lands at the scale factor's distance from where it was aimed**. It is
 * rare, it is silent, and a user who has not been told will assume the feature
 * is broken rather than mis-scaled — so it is a sentence, not a tint, and it
 * says what to do about it.
 */
export type DesktopPreviewProps = {
  /** The latest observation, or null when the desktop tool has not looked yet. */
  state: {
    generation: number;
    window: string;
    process: string;
    addressable: boolean;
    dpiAligned: boolean;
    elements: number;
    ageMs?: number;
  } | null;
};

/** Seconds, or "just now". Rounded up, because "0s old" reads as broken. */
function ageLabel(ageMs: number | undefined): string | null {
  if (ageMs === undefined) return null;
  const seconds = Math.ceil(ageMs / 1000);
  if (seconds < 3) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.round(minutes / 60)}h ago`;
}

export function DesktopPreview({ state }: DesktopPreviewProps) {
  if (!state) return null;

  const age = ageLabel(state.ageMs);
  const stale = state.ageMs !== undefined && state.ageMs > 20_000;

  /*
   * The generation is permanent; the third slot is whichever secondary fact
   * matters for *this* state — because at the deck's floor all three do not fit.
   *
   * Measured with the deck at its 184px width floor, which leaves 146px of
   * track for column 2:
   *
   *     screen #7 · 12 controls · 3s ago   171px   ellipsises
   *     screen #7 · 12 controls            132px
   *     screen #107 · 12 controls          144px
   *     screen #7 · 47s ago                116px
   *     screen #7 · no names               125px
   *
   * The generation wins the slot outright because it is the one thing nothing
   * else can supply: `docs` calls it what the model clicks against, and a
   * generation cut to `scre…` — which is what happened when it shared the
   * *detail* line with the claim — is neither readable nor reconstructable.
   *
   * Count and age then take turns. `age` is the expendable one: a fresh age of
   * "3s ago" answers a question nobody asked, while a stale one is the only
   * thing that can say why the tint is on. The count is named in AGENTS.md as
   * this row's job ("lists what she can click"), so it keeps the slot until
   * something actually goes old.
   */
  const generation = `screen #${state.generation}`;
  let meta: string;
  if (!state.addressable) {
    meta = `${generation} · no names`;
  } else if (stale && age) {
    meta = `${generation} · ${age}`;
  } else {
    meta = `${generation} · ${state.elements} ${state.elements === 1 ? "control" : "controls"}`;
  }

  return (
    <aside className="vision-row vision-row--desktop" aria-label="The window Lumine is working in">
      <span className="vision-row-icon" aria-hidden="true">
        <Icon name="monitor" size={13} />
      </span>
      <span className="vision-row-title">{state.window || "your desktop"}</span>
      {/*
        The claim, alone on its line and in the same shape the browser row
        uses: this is the one place the user is told which of the two surfaces
        Lumine is currently working from.

        It lost `· screen #N` deliberately — see the `meta` computation above.
        The claim and the generation together are 202px against a 146px track
        at the deck's floor, so sharing a line meant the *generation* was what
        the ellipsis ate, and that is the fact worth least to a reader.
      */}
      <span className="vision-row-detail">Lumine is looking at it</span>
      <span
        className={
          "vision-row-meta" + (stale || !state.addressable ? " is-warn" : "")
        }
      >
        {meta}
      </span>
      {!state.dpiAligned ? (
        <p className="vision-row-warning">
          This system would not let Lumine read screen positions the same way she clicks
          them, so clicks may land slightly off. Scaling the display to 100% usually fixes
          it.
        </p>
      ) : null}
    </aside>
  );
}
