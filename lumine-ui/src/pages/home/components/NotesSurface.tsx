import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import type { Note, NotesApi } from "../notes/useNotes";

/**
 * The notes surface: a list, and the text of whichever note is picked.
 *
 * It is one component on two screens — the right-hand aside beside the stage, and
 * the Focus page — because they are the same two panes at different widths. Two
 * implementations would be two places to fix the "saving into a note that was
 * deleted underneath the editor" case, and one of them would be fixed later.
 *
 * Nothing here owns storage. The list arrives as a prop, because two live writers
 * to one key is a race the reader wins by typing, and the loser is the sentence
 * they just wrote.
 *
 * Saving is on every change and has no Save button. A button for a local list is
 * a control whose failure mode is "I pressed it and nothing happened", and the
 * write it performs is a synchronous one to this machine.
 */
export function NotesSurface({
  notes,
  activeId,
  onSelect,
  onCreate,
  onSave,
  onDelete,
  /** Drops the list. The aside is 380px wide, and a list is what it cannot afford. */
  compact = false,
}: {
  notes: NotesApi;
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onSave: (id: string, body: string) => void;
  onDelete: (id: string) => void;
  compact?: boolean;
}) {
  const active: Note | null = notes.ordered.find((note) => note.id === activeId) ?? notes.ordered[0] ?? null;
  const [draft, setDraft] = useState(active?.body ?? "");
  const areaRef = useRef<HTMLTextAreaElement>(null);
  const activeIdKey = active?.id ?? null;
  const activeBody = active?.body ?? "";

  // The draft follows the *picked note*, not the text. Writing the incoming body
  // into the draft on every store update would fight the caret mid-word; this runs
  // only when the note being edited is a different one.
  useEffect(() => {
    setDraft(activeBody);
  }, [activeIdKey, activeBody]);

  // A note created from the button is a note to write in, so the caret goes there.
  // One frame's wait for the same reason the transcript's composer waits: on the
  // opening frame the element may not have been measured yet.
  useEffect(() => {
    if (activeIdKey === null || activeBody !== "") return;
    const frame = requestAnimationFrame(() => areaRef.current?.focus());
    return () => cancelAnimationFrame(frame);
    // Only the identity of the note, deliberately: a note that becomes empty while
    // being written is not a note that just asked for focus.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeIdKey]);

  if (notes.ordered.length === 0) {
    return (
      <div className="notes-surface notes-surface--empty">
        <span className="notes-empty-mark"><Icon name="note" size={22} /></span>
        <p>No notes yet</p>
        <button type="button" className="widget-button primary" onClick={onCreate}>
          <Icon name="plus" size={14} />
          Write one
        </button>
      </div>
    );
  }

  const commit = (next: string) => {
    setDraft(next);
    if (active) onSave(active.id, next);
  };

  return (
    <div className={`notes-surface${compact ? " is-compact" : ""}`}>
      {!compact && (
        <nav className="notes-list" aria-label="Saved notes">
          {notes.ordered.map((note) => (
            <button
              key={note.id}
              type="button"
              className={`notes-list-item${note.id === activeIdKey ? " is-active" : ""}`}
              aria-current={note.id === activeIdKey ? "true" : undefined}
              onClick={() => onSelect(note.id)}
            >
              <span className="notes-list-title overflow-hidden text-[12.5px] font-semibold text-ellipsis whitespace-nowrap">{titleOf(note.body)}</span>
              <span className="notes-list-date text-faint text-[10.5px]">{formatDay(note.updatedAt)}</span>
            </button>
          ))}
          <button type="button" className="notes-new" onClick={onCreate}>
            <Icon name="plus" size={14} />
            New note
          </button>
        </nav>
      )}

      <div className="notes-editor min-w-0 min-h-0 grid grid-rows-[auto_minmax(0,_1fr)]">
        <header className="notes-editor-head">
          <span className="notes-editor-title overflow-hidden text-foreground text-[13px] font-bold text-ellipsis whitespace-nowrap">{active ? titleOf(active.body) : "Note"}</span>
          <span className="notes-editor-actions flex shrink-0 gap-0.5">
            {compact && (
              <button type="button" className="panel-icon" onClick={onCreate} aria-label="New note" title="New note">
                <Icon name="plus" size={15} />
              </button>
            )}
            <button
              type="button"
              className="panel-icon"
              onClick={() => active && onDelete(active.id)}
              aria-label="Delete this note"
              title="Delete this note"
            >
              <Icon name="trash" size={15} />
            </button>
          </span>
        </header>
        <textarea
          ref={areaRef}
          className="notes-area"
          value={draft}
          onChange={(event) => commit(event.target.value)}
          placeholder="Write it down before it goes…"
          spellCheck={false}
          aria-label="Note text"
        />
      </div>
    </div>
  );
}

/** The first line, so a list has a name without asking for one. */
function titleOf(body: string): string {
  const line = body.split(/\n/, 1)[0]?.trim() ?? "";
  return line === "" ? "Untitled" : line.slice(0, 46);
}

function formatDay(timestamp: number): string {
  if (!timestamp) return "";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(timestamp));
}
