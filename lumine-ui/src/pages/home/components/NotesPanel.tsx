import { Icon } from "./Icon";
import { NotesSurface } from "./NotesSurface";
import type { NotesApi } from "../notes/useNotes";

/**
 * The notes aside, opened by its own switch in the stage's topbar.
 *
 * It is the same shape and the same column as the transcript — one right-hand
 * aside at `--conversation-width`, one at a time — because "the thing beside the
 * avatar" is a single slot in this layout. Two of them open at once would be two
 * panels fighting over one track, and the reader would have to close one to find
 * the other behind it. The switches are therefore exclusive: opening notes closes
 * the transcript and the other way round, which is what a pair of toggles driving
 * one slot should do.
 *
 * This is not a second copy of the Focus page. It is the same `NotesSurface` at
 * a narrower width with the list folded away, and the timer is not here at all —
 * a countdown running in a panel nobody has open is a countdown nobody can stop.
 */
export function NotesPanel({
  open,
  onOpenChange,
  notes,
  activeId,
  onSelect,
  onCreate,
  onSave,
  onDelete,
  onFocusPage,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  notes: NotesApi;
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onSave: (id: string, body: string) => void;
  onDelete: (id: string) => void;
  onFocusPage: () => void;
}) {
  return (
    <aside
      id="notes-panel"
      className={`notes-panel${open ? " is-open" : ""}`}
      aria-label="Notes"
      hidden={!open}
    >
      <header className="conversation-head notes-panel-head">
        <div>
          <div className="conversation-brand flex items-center gap-2 text-foreground text-[15px] font-bold tracking-[-0.035em]">
            <Icon name="note" size={17} />
            <span>Notes</span>
          </div>
        </div>
        <div className="conversation-head-actions flex items-center gap-0.5">
          <button
            type="button"
            className="panel-icon"
            onClick={onFocusPage}
            aria-label="Open the Focus page"
            title="Open the Focus page"
          >
            <Icon name="timer" size={16} />
          </button>
          <button
            type="button"
            className="panel-icon"
            onClick={() => onOpenChange(false)}
            aria-label="Close notes"
            title="Close notes"
          >
            <Icon name="close" size={16} />
          </button>
        </div>
      </header>

      <div className="notes-panel-body min-w-0 min-h-0 grid">
        <NotesSurface
          notes={notes}
          activeId={activeId}
          onSelect={onSelect}
          onCreate={onCreate}
          onSave={onSave}
          onDelete={onDelete}
          compact
        />
      </div>
    </aside>
  );
}
