/**
 * Subsequence matching for the command palette.
 *
 * ## Why not a dependency
 *
 * `fuse.js`, `fuzzy` and `cmdk`'s matcher are all perfectly good, and adding a
 * fourth runtime dependency to a list of maybe sixty to do "does 'act' match
 * 'Activity'" is the kind of decision that is never revisited. The whole
 * algorithm is about eighty lines, it has no configuration surface we would
 * expose, and keeping it here means the palette's behaviour is something we can
 * read and change rather than something we tune through someone else's
 * `threshold` option.
 *
 * ## What it is
 *
 * A *subsequence* match: the query's characters must appear in the target in
 * order, but not adjacently. `act` finds "Activity", and so does `aty`. It
 * rejects `xyz` outright. Every command palette people actually use works this
 * way, because it is the only model where typing three letters narrows a list
 * instead of emptying it -- which is what makes a palette feel like a keyboard
 * rather than a search box.
 *
 * ## How results are ordered
 *
 * Among matches, score decides. The intuition is that the *shape* of a match
 * says more than the fact of it:
 *
 * - **A word boundary beats a mid-word hit.** "ct" should find "a**ct**ivity"
 *   before "con**ct**act". This is the single biggest lever, and it is why
 *   `word` finds `Word wrap` rather than something buried inside a longer word.
 * - **A run beats scattered letters.** "set" in "**set**tings" is one
 *   consecutive run; "set" scattered through "system settings export" is three
 *   separate hits for the same three keystrokes. Runs are what a human means.
 * - **Earlier beats later.** All else equal, a match at the start is a shorter
 *   thing to have read.
 * - **A contiguous substring is the best case.** Typing the first letters of a
 *   command and having it float to the top is the behaviour that teaches people
 *   the palette exists.
 *
 * ## The one deliberate absence
 *
 * There is no Levenshtein / edit-distance fallback. It is genuinely good for
 * finding "settigns" -> "Settings", and it is also how a palette starts ranking
 * "Audio" above "Settings" for the query "sett" because the edit distance
 * happens to tie. A command palette is a *navigation* surface: the commands are
 * short, their names are known, and a wrong top hit costs more than a missing
 * one. The user can see the whole list, so recall matters and precision matters
 * more.
 */

/**
 * Score contributions.
 *
 * Powers of roughly 2 apart, so the bonuses compose rather than fighting: two
 * word-boundary hits outweigh one run of three, which outweighs an early hit.
 * The exact numbers matter less than their ordering, and the ordering is what
 * these comments are for.
 */
const BONUS_BOUNDARY = 24;
const BONUS_CAMEL = 18;
const BONUS_CONSECUTIVE = 12;
const BONUS_FIRST_CHAR = 14;
const BONUS_SUBSTRING = 40;

/** What one character of gap costs, per position skipped. */
const PENALTY_GAP = 1.6;

/** A small length penalty, so a 40-character target cannot win on gaps alone. */
const PENALTY_LENGTH = 0.35;

/** Combining marks, which `NFD` leaves behind and which are not letters. */
const COMBINING_MARKS = /[̀-ͯ]/;

/** A hard cap on returned indices, so a pathological query cannot allocate forever. */
const MAX_INDICES = 64;

/** Characters that end a word. */
const SEPARATORS = /[\s\-_/.:,·—–]/;

/** Lowercase once, and drop the diacritics that would otherwise split a word. */
function normalise(value: string): string {
  return value
    .toLowerCase()
    .normalize("NFD")
    .replace(COMBINING_MARKS, "");
}

/** A character that starts a word: the first, or one after a separator. */
function isBoundary(target: string, index: number): boolean {
  if (index === 0) return true;
  return SEPARATORS.test(target[index - 1]);
}

/** A camelCase hump: lower before, upper here. Scored below a real boundary. */
function isCamel(target: string, index: number): boolean {
  if (index <= 0) return false;
  const previous = target[index - 1];
  const current = target[index];
  return previous === previous.toLowerCase() && previous !== previous.toUpperCase() && current === current.toUpperCase() && current !== current.toLowerCase();
}

export type FuzzyMatch = {
  /** Higher is better. Always > 0 for a returned match. */
  score: number;
  /**
   * Indices in the *normalised* target that matched, ascending.
   *
   * Returned rather than just the score so the UI can mark what matched. A
   * palette that filters without showing the match leaves the user guessing why
   * a command they can see was not found -- and "why is this highlighted" is the
   * whole feedback loop of typing a prefix.
   *
   * Normalised rather than original, because the walk happens in normalised
   * space. `highlightRuns` maps back; see the note there.
   */
  indices: number[];
};

/**
 * Score one query against one target.
 *
 * Returns `null` when the query is not a subsequence of the target, which is the
 * signal to filter rather than to rank with a zero -- a zero would sort a real
 * match below a non-match.
 */
export function fuzzyMatch(query: string, target: string): FuzzyMatch | null {
  const needle = normalise(query).trim();
  const haystack = normalise(target);
  if (!needle) return { score: 0, indices: [] };
  if (!haystack) return null;
  if (needle.length > haystack.length) return null;

  // A contiguous substring is a special case worth rewarding outright, because
  // "the query appears inside the target" is the strongest possible signal and
  // the greedy walk below would only find it by luck, one character at a time.
  const substringAt = haystack.indexOf(needle);
  if (substringAt >= 0) {
    return {
      score:
        BONUS_SUBSTRING +
        BONUS_CONSECUTIVE * needle.length +
        (isBoundary(haystack, substringAt) ? BONUS_BOUNDARY : 0) +
        (substringAt === 0 ? BONUS_FIRST_CHAR : 0) -
        substringAt * PENALTY_GAP,
      indices: Array.from({ length: needle.length }, (_, offset) => substringAt + offset).slice(0, MAX_INDICES),
    };
  }

  // Greedy forward walk.
  //
  // A single greedy pass finds *a* match rather than the best one, and the
  // difference is visible: for "onl" against "Open Notification Line", greedy
  // takes the `o` in "Open" and then has to reach all the way to "Notification",
  // while a right-to-left pass would take the `o` in "Notification" and score a
  // far tighter run. This scores what greedy found, which is right often enough
  // and cheap enough that a search over every alignment is not worth it.
  const indices: number[] = [];
  let score = 0;
  let at = 0;
  let previousIndex = -1;

  for (const character of needle) {
    let found = -1;
    for (let index = at; index < haystack.length; index += 1) {
      if (haystack[index] === character) {
        found = index;
        break;
      }
    }
    if (found < 0) return null;

    if (previousIndex >= 0 && found === previousIndex + 1) {
      score += BONUS_CONSECUTIVE;
    } else if (previousIndex >= 0) {
      score -= (found - previousIndex - 1) * PENALTY_GAP;
    }
    if (found === 0) score += BONUS_FIRST_CHAR;
    if (isBoundary(haystack, found)) score += BONUS_BOUNDARY;
    else if (isCamel(haystack, found)) score += BONUS_CAMEL;

    indices.push(found);
    previousIndex = found;
    at = found + 1;
  }

  score -= haystack.length * PENALTY_LENGTH;
  return { score: Math.max(1, score), indices: indices.slice(0, MAX_INDICES) };
}

export type FuzzyResult<T> = {
  item: T;
  score: number;
  indices: number[];
};

/** A result plus where it came from, so ties can be broken without a cast. */
type Scored<T> = { item: T; score: number; indices: number[]; position: number };

/**
 * Filter and rank a list.
 *
 * A no-op query returns everything, in its original order, with no indices -- so
 * the palette opens on a sensible list rather than on a search for "". The
 * original order is preserved rather than re-sorted, because the caller's order
 * *is* the intended order: commands are declared grouped by what they are, and
 * that grouping is worth more than an arbitrary alphabetical sort when nothing
 * has been typed.
 */
export function fuzzyFilter<T>(query: string, items: readonly T[], toText: (item: T) => string): Array<FuzzyResult<T>> {
  const needle = query.trim();
  if (!needle) {
    return items.map((item) => ({ item, score: 0, indices: [] as number[] }));
  }

  const scored: Array<Scored<T>> = [];
  items.forEach((item, position) => {
    const match = fuzzyMatch(needle, toText(item));
    // A stable tiebreak on declaration order, so two commands with equal scores
    // never swap places between renders -- a list that reshuffles while you are
    // arrowing through it is the fastest way to make a palette feel haunted.
    if (match) scored.push({ item, score: match.score, indices: match.indices, position });
  });

  scored.sort((a, b) => b.score - a.score || a.position - b.position);
  return scored.map(({ item, score, indices }) => ({ item, score, indices }));
}

/**
 * Normalise `text` while remembering where each character came from.
 *
 * Built by walking the *original* string and normalising one character at a
 * time, rather than normalising the whole string and lining the results up
 * afterwards. Those are not the same thing: `"e" + U+0301` normalises to two
 * code units, so a length-preserving assumption -- which mapping back from
 * normalised indices to original positions requires -- is false for exactly the
 * characters most likely to appear in a person's saved palette name.
 *
 * The map is what lets a match found in normalised space be highlighted in the
 * original, which is the difference between marking "Act" in "Activity" and
 * marking the wrong four letters whenever a diacritic is involved.
 */
function normaliseWithMap(text: string): number[] {
  const sourceIndex: number[] = [];
  for (let index = 0; index < text.length; index += 1) {
    for (const character of text[index].toLowerCase().normalize("NFD")) {
      if (!COMBINING_MARKS.test(character)) sourceIndex.push(index);
    }
  }
  return sourceIndex;
}

/**
 * Split `text` into matched and unmatched runs, for highlighting.
 *
 * Returns the whole string as one unmatched run when nothing matched, so a
 * caller can render the result unconditionally.
 */
export function highlightRuns(text: string, indices: readonly number[]): Array<{ text: string; match: boolean }> {
  if (indices.length === 0) return [{ text, match: false }];

  const sourceIndex = normaliseWithMap(text);
  const matched = new Set<number>();
  for (const normalisedPosition of indices) {
    const originalPosition = sourceIndex[normalisedPosition];
    // Out of range only if the caller passed indices for a *different* string.
    // Skipped rather than thrown on, because a highlight is decoration and a
    // decoration must never be able to take down the palette.
    if (originalPosition !== undefined) matched.add(originalPosition);
  }
  if (matched.size === 0) return [{ text, match: false }];

  const runs: Array<{ text: string; match: boolean }> = [];
  let current = "";
  let currentMatch = matched.has(0);
  for (let index = 0; index < text.length; index += 1) {
    const isMatch = matched.has(index);
    if (isMatch === currentMatch) {
      current += text[index];
      continue;
    }
    runs.push({ text: current, match: currentMatch });
    current = text[index];
    currentMatch = isMatch;
  }
  if (current) runs.push({ text: current, match: currentMatch });
  return runs;
}
