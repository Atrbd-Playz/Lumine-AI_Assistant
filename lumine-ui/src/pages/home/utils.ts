/** The `#252525` half of this choice, as a relative luminance. */
const DARK_FOREGROUND_LUMINANCE = 0.0185; // #252525, sRGB -> linear

/**
 * Returns a readable foreground for a user-selected background color.
 *
 * The rule used to be `luminance > 0.48`, which is not a contrast boundary at
 * all — it is a guess, and it guessed wrong for anything in the middle. White
 * stops reaching 4.5:1 once the background passes luminance 0.183, and #252525
 * does not reach it until 0.258, so the old threshold picked white for the whole
 * band between: Lumine's own default accent (#e86f3d, luminance 0.289) got white
 * at 3.1:1 where dark would have given 4.9:1, and a stage colour of #8b8882 got
 * white at 3.4:1 where dark would have given 4.5:1. Both are the app telling a
 * user their text is fine when it is not.
 *
 * `minimumContrast` is now what decides. White is offered first so the call
 * button — a graphic, and a deliberate white-on-orange — keeps its face; dark is
 * taken when white cannot clear the bar, and when neither can (the impossible
 * band between 0.183 and 0.258) the better of the two is returned rather than
 * either one arbitrarily.
 */
export function getReadableTextColor(
  background: string,
  minimumContrast = 3,
): "#ffffff" | "#252525" {
  const normalized = background.replace("#", "");
  if (normalized.length !== 6) return "#ffffff";
  const value = Number.parseInt(normalized, 16);
  if (Number.isNaN(value)) return "#ffffff";
  const channels = [(value >> 16) & 255, (value >> 8) & 255, value & 255].map((channel) => {
    const normalizedChannel = channel / 255;
    return normalizedChannel <= 0.03928
      ? normalizedChannel / 12.92
      : Math.pow((normalizedChannel + 0.055) / 1.055, 2.4);
  });
  const luminance = channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  const white = 1.05 / (luminance + 0.05);
  const dark = (luminance + 0.05) / (DARK_FOREGROUND_LUMINANCE + 0.05);
  if (white >= minimumContrast) return "#ffffff";
  if (dark >= minimumContrast) return "#252525";
  return dark >= white ? "#252525" : "#ffffff";
}

function getLuminance(color: string) {
  const normalized = color.replace("#", "");
  if (normalized.length !== 6) return null;
  const value = Number.parseInt(normalized, 16);
  if (Number.isNaN(value)) return null;
  const channels = [(value >> 16) & 255, (value >> 8) & 255, value & 255].map((channel) => {
    const normalizedChannel = channel / 255;
    return normalizedChannel <= 0.03928
      ? normalizedChannel / 12.92
      : Math.pow((normalizedChannel + 0.055) / 1.055, 2.4);
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

/** Keeps custom foreground colors readable without changing the user's other palette choices. */
export function getReadableForeground(color: string, backgrounds: string[], fallback: string, minimumContrast = 3) {
  const foreground = getLuminance(color);
  const isReadable = foreground !== null && backgrounds.every((background) => {
    const backgroundLuminance = getLuminance(background);
    if (backgroundLuminance === null) return false;
    const lighter = Math.max(foreground, backgroundLuminance);
    const darker = Math.min(foreground, backgroundLuminance);
    return (lighter + 0.05) / (darker + 0.05) >= minimumContrast;
  });
  return isReadable ? color : fallback;
}

export function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

/**
 * A CSS `font-family` for one of the six named interface faces.
 *
 * It lives here rather than in `Home.tsx` because there is a second caller now:
 * the appearance settings draw every typeface option *in its own face*, and a
 * chooser that samples a face with a stack built somewhere else is a chooser that
 * quietly disagrees with the app the moment one of the two is edited.
 *
 * The fallback is a real part of the answer, not defensive noise. Each face is
 * loaded from a webfont that can be absent — offline, blocked, or a machine that
 * never fetched it — and the generic after the name is what the browser reaches
 * for instead. A trailing `sans-serif` on a serif face would be wrong, which is
 * why the generic is chosen per family rather than appended to all of them.
 */
export function fontStack(font: string): string {
  switch (font) {
    case "Newsreader":
      return "Newsreader, serif";
    case "DM Mono":
      return "DM Mono, monospace";
    case "Space Grotesk":
      return "Space Grotesk, sans-serif";
    case "Roboto":
      return "Roboto, sans-serif";
    case "Ubuntu":
      return "Ubuntu, sans-serif";
    case "Manrope":
      return "Manrope, sans-serif";
    default:
      // An uncatalogued face. The name is still worth trying — a stored profile
      // may name a font this build has not heard of — but sans-serif has to be
      // there or the browser uses its default serif for a UI.
      return `"${font}", sans-serif`;
  }
}

/**
 * The app's clock format: two-digit hour and minute, in the reader's locale.
 *
 * The zero-padding is the whole point. The corner clock is set in tabular
 * numerals so the figure does not shuffle while it ticks, and `9:57` to `10:00`
 * moves that figure by a digit every morning while `09:57` to `10:00` does not.
 * It is also what the activity log already printed, which leaves the transcript
 * as the one surface rendering `3:07` beside a stage reading `03:07` and giving
 * the reader no way to tell whether those are the same minute.
 */
export const CLOCK_FORMAT: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit" };

/**
 * Every time the UI prints, from one place.
 *
 * Three surfaces each assembled their own `Intl.DateTimeFormat`: the stage
 * clock, the transcript row, the activity log. The helper is not about the
 * bytes — it is that the disagreement was invisible until two of them stood side
 * by side, which happens only on the one screen that has all three.
 *
 * `options` exists so a surface can ask for more than a glance needs. The log
 * wants seconds because it is a record of events being ordered; the clock and
 * the transcript do not, because nobody reading a conversation needs to know
 * which of two messages in the same minute came first. Asking for them is now
 * the explicit act it always should have been.
 *
 * An instant that does not parse prints nothing rather than `Invalid Date`. The
 * transcript is the only caller that can hand over something unparseable, and
 * it keeps its own string branch — see `ConversationMessage`.
 */
export function formatClock(at: Date | number, options: Intl.DateTimeFormatOptions = CLOCK_FORMAT): string {
  const date = at instanceof Date ? at : new Date(at);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, options).format(date);
}
