import { useState } from "react";
import { NotesSurface } from "./home/components/NotesSurface";
import { Pomodoro } from "./home/components/Pomodoro";
import type { NotesApi } from "./home/notes/useNotes";

/**
 * The Focus page: the timer and the notes, together and nowhere else.
 *
 * ## Why these two moved off the home stage
 *
 * A countdown you have to keep an eye on and a paragraph you have to keep your
 * mind in are both the opposite of the home stage's job, which is to be looked at
 * and left alone. They were in the everyday rail as two more cards among four, and
 * a card you have to *use* sitting beside three you only *check* means the rail
 * has two tempos in it and you have to read all six to find out which is which.
 *
 * So the checking cards stay on home and the two that need a decision come here.
 * The home stage keeps one door to this page — a note's first line, and the rail's
 * own Focus item — so nothing that was reachable became unreachable.
 *
 * `notes` arrives as a prop for the same reason the panel's does: one writer to one
 * key, and the home page's note preview is reading it at the same time.
 */
export function FocusPage({ notes }: { notes: NotesApi }) {
  const [activeId, setActiveId] = useState<string | null>(null);

  return (
    <main className="focus-page" aria-labelledby="focus-title">
      <header className="workspace-header focus-header">
        <div>
          <p className="eyebrow">Lumine workspace</p>
          <h1 id="focus-title">Focus</h1>
        </div>
      </header>

      <div className="focus-grid">
        <Pomodoro />
        <section className="focus-notes" aria-label="Notes">
          <NotesSurface
            notes={notes}
            activeId={activeId}
            onSelect={setActiveId}
            onCreate={() => setActiveId(notes.create())}
            onSave={notes.save}
            onDelete={(id) => {
              notes.remove(id);
              setActiveId((current) => (current === id ? null : current));
            }}
          />
        </section>
      </div>
    </main>
  );
}
