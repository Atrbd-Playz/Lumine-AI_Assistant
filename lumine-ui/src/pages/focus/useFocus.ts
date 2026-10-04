import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import { ensureLoaded, getSnapshot, installLifecycle, subscribe, update } from "./focusStore";
import type { FocusDocument, FocusSnapshot } from "./types";
import { defaultDocument } from "./types";

/**
 * Subscribing to the Focus Workspace document.
 *
 * ## Why `useSyncExternalStore` and not `useState`
 *
 * The store is a module singleton outside React, so React has no way to learn that it
 * changed. `useState` would mean copying the document into component state and
 * re-implementing the notification by hand, which is how two copies drift. The
 * external-store contract is exactly this problem, so it is used directly.
 *
 * ## The property that matters most on this page
 *
 * A running timer ticks every second. If a subscribed value changes identity on
 * every store change — because a selector builds a fresh array, say — then
 * `useSyncExternalStore` believes the store changed and rerenders the whole board
 * once a second. So `useFocusSelector` **caches** its selection and returns the
 * previous object when the comparator says the value is equal. That cache is the
 * whole difference between a page that is smooth and a page that makes a laptop fan
 * spin up, and it is why `isEqual` exists rather than being left at `Object.is`.
 */

/** The server snapshot is the same function; Tauri does not render on a server. */
const SERVER_SNAPSHOT = (): FocusSnapshot => getSnapshot();

/** Read the whole snapshot: status, error and document. */
export function useFocusSnapshot(): FocusSnapshot {
  useFocusSubscription();
  return useSyncExternalStore(subscribe, getSnapshot, SERVER_SNAPSHOT);
}

/** Read just the document. Safe for a widget that does not care about status. */
export function useFocusDocument(): FocusDocument {
  return useFocusSelector((snapshot) => snapshot.document);
}

/**
 * Read one slice of the document.
 *
 * `isEqual` defaults to `Object.is`, which is right for scalars and for references.
 * Pass a comparator for anything derived — a filtered list, a sorted list — or the
 * cached selection will be a new object on every store change and the subscription
 * will be pointless.
 *
 * `select` runs during render and must be pure, which it is: the document is
 * immutable and every derivation is a pure function of it.
 */
export function useFocusSelector<T>(
  select: (snapshot: FocusSnapshot) => T,
  isEqual: (left: T, right: T) => boolean = Object.is,
): T {
  useFocusSubscription();

  // Held in refs so a caller may pass an inline arrow without the subscription
  // changing identity every render — which would tear down and re-establish the
  // store subscription on every keystroke in a text field.
  const selectRef = useRef(select);
  selectRef.current = select;
  const isEqualRef = useRef(isEqual);
  isEqualRef.current = isEqual;

  const cache = useRef<{ value: T } | null>(null);

  const getSelection = useCallback(() => {
    const next = selectRef.current(getSnapshot());
    const held = cache.current;
    // Returning the *previous* value when it is equal is what lets React bail out
    // of the re-render. Returning `next` unconditionally — even when equal — would
    // make every store change a re-render for every subscriber.
    if (held && isEqualRef.current(held.value, next)) return held.value;
    cache.current = { value: next };
    return next;
  }, []);

  return useSyncExternalStore(subscribe, getSelection, getSelection);
}

/** Subscribe, and load if nobody has. Every focus hook does this exactly once. */
function useFocusSubscription(): void {
  useEffect(() => {
    ensureLoaded();
    return installLifecycle();
  }, []);
}

/**
 * Read the document and a mutator for it, as one call.
 *
 * The mutator is `useCallback`-stable, so it is safe in a dependency array — which
 * matters because a widget that puts an inline arrow in its deps will re-run an
 * effect on every keystroke.
 */
export function useFocus(): [
  FocusDocument,
  (mutate: (document: FocusDocument) => FocusDocument) => void,
] {
  const document = useFocusDocument();
  const mutate = useCallback((next: (document: FocusDocument) => FocusDocument) => update(next), []);
  return [document, mutate];
}

/**
 * The document, falling back to the shipped defaults while loading.
 *
 * For a widget that renders its own skeleton anyway and would rather not branch on
 * `status`. Note what this is *not* for: a chart must show an empty state rather than
 * a row of zeros, and a fallback that silently supplies a document makes the
 * difference between those two impossible to express.
 */
export function useFocusDocumentOrDefaults(): FocusDocument {
  const snapshot = useFocusSnapshot();
  return snapshot.status === "loading" ? defaultDocument() : snapshot.document;
}