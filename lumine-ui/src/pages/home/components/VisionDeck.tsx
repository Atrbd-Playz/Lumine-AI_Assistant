import { useVisualState } from "../../../lib/visualState";
import { SelfView } from "./SelfView";
import { BrowserPreview, type BrowserPreviewProps } from "./BrowserPreview";
import { DesktopPreview, type DesktopPreviewProps } from "./DesktopPreview";
import type { ActiveMedia } from "../../../features/voice/useLocalMedia";

/**
 * The one corner of the stage that answers "what is Lumine looking at right now".
 *
 * ## Why this component exists
 *
 * There used to be three of these, and each was its own absolutely-positioned
 * card anchored to the same corner:
 *
 * ```text
 * .self-view        top: 84px; right: var(--stage-gutter)
 * .browser-preview  top: 84px; right: var(--stage-gutter)
 * .desktop-preview  top: 84px; right: var(--stage-gutter)
 * ```
 *
 * plus a fourth surface, a full-width `.share-strip`, that printed *the same
 * sentence* the self-view's badge printed. The stylesheet even tried to separate
 * them — `.self-view:has(+ .browser-preview)` was written twice — but the markup
 * renders `BrowserPreview` *before* `SelfView`, so `+` never matched and the
 * cards overlapped exactly. The one rule that did fire, `.desktop-preview ~
 * .browser-preview { top: 234px }`, pushed a card past the bottom of a stage
 * that is `overflow: hidden` and only 342px tall at the 800x600 window this app
 * opens at, so the overflow was taken silently.
 *
 * A hand-computed `top` per card is the shape of that bug, and it recurs every
 * time a surface is added. This deck replaces it with a flex column anchored by
 * `top` and `bottom` — the edges, not a running total — so a new surface costs
 * one row and no arithmetic.
 *
 * ## One picture slot, and who gets it
 *
 * The deck shows **one** picture: the user's own capture, when there is one.
 * A 16:9 card is ~103px tall at the deck's 184px floor and the whole column has
 * **202px** at the 800x600 window this app opens at (measured — see the block
 * in `index.css`), so two pictures plus their bars come to ~272px and cannot
 * coexist. Hence the browser is a *picture* only when the user is sharing
 * nothing, and a two-line **row** otherwise. The claim survives the change of
 * shape; only the pixels go.
 *
 * The desktop surface is always a row, because it never had a picture to show:
 * it reads the window's accessibility tree, so what exists is a name, a count
 * and a generation number drawn inside a card sized for a screenshot.
 *
 * ## One status sentence, and where it lives
 *
 * `SelfView` owns it, as the header of the picture it describes. The old
 * `.share-strip` said "Lumine can see this screen" under the stage's header and
 * the card's badge said the same thing in the same words — one fact rendered
 * twice on every screen that showed it, and on the two states where the
 * fallbacks differed, one fact stated two *different* ways.
 *
 * The browser's own claim is not that sentence restated: it is a different fact
 * about a different surface (a page *her* browser is driving, which the user
 * cannot see), and dropping it would leave a screenshot's caption with the
 * load-bearing half left out. Two surfaces, two facts, each stated once.
 */
export type VisionDeckProps = {
  /** The one live capture, or null. Never both camera and screen. */
  preview: ActiveMedia | null;
  /** Whether that capture is actually published to the room. */
  isPublished: boolean;
  /** The latest page screenshot from the worker's browser, or null. */
  browserPreview: BrowserPreviewProps["preview"];
  /** The latest desktop observation, or null. */
  desktopState: DesktopPreviewProps["state"];
};

export function VisionDeck({ preview, isPublished, browserPreview, desktopState }: VisionDeckProps) {
  // The worker's own answer to "is a picture actually arriving", and the only
  // answer available from this side of the pipe: a live share and a frozen one
  // look identical to the process holding the track. `null` until the first
  // `visual` record, which `visionShareText` treats as "say nothing new" rather
  // than as a claim. It lives here rather than in `MainSpace` because the only
  // thing that reads it is the card that states it.
  const vision = useVisualState();

  if (!preview && !browserPreview && !desktopState) return null;

  return (
    <div className="vision-deck">
      {/*
        The picture, and the deck's one status sentence as its header.

        Rendered whenever there is a capture, which is also the only case where
        the browser cannot be a picture — see the module docstring.
      */}
      {preview && (
        <SelfView
          stream={preview.stream}
          source={preview.source}
          isPublished={isPublished}
          phase={vision?.phase}
        />
      )}
      {/*
        The browser: the picture when the deck has a free picture slot, a row
        when the capture has taken it.

        `preview` is the test rather than a prop the caller chose, because the
        slot is decided by what is on screen, not by what the caller wanted.
      */}
      {browserPreview && (
        <BrowserPreview preview={browserPreview} variant={preview ? "row" : "picture"} />
      )}
      {/*
        The desktop window, last, because it is the surface the user most needs
        to see named — it is the one that moves their pointer — but it is one
        line and the two above it carry the pictures.
      */}
      <DesktopPreview state={desktopState} />
    </div>
  );
}
