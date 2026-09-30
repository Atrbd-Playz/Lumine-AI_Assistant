import { useCallback, useEffect, useState } from "react";

/** One saved note. The id is what a list and an editor agree on, not the text. */
export type Note = {
  id: string;
  body: string;
  updatedAt: number;
};

const STORAGE_KEY = "lumine.notes.v1";

/**
 * The notes list, and the only place that list is written.
 *
 * ## Why this is not `localStorage` in two components
 *
 * There are two surfaces for the same notes — the right-hand aside on the home
 * stage, and the Focus page — and each of them used to have been a copy of the
 * same read-write block. Two writers to one key is a race the user wins by typing
 * quickly in one surface after having typed in the other, and the loser is the
 * sentence they just wrote. So the key has one reader, one writer, and both
 * surfaces subscribe to it.
 *
 * ## Why a list of notes rather than one document
 *
 * The old widget was a single textarea. It is the right shape for a scratchpad
 * and the wrong shape for anything a person comes back to: there is no way to
 * keep two things apart, and the way to lose the first is to write the second.
 * A list costs one `id` per row and gives the thing a list gives — pick, edit,
 * delete, and a "most recent" that is a sort rather than a scroll position.
 *
 * ## Why the failure is silent
 *
 * `localStorage` throws on a full disk, a private window, a disabled profile.
 * A note app that raises on its own storage is worse than one that forgot, so
 * every access is wrapped and a value that will not parse comes back as an empty
 * list rather than as an exception inside `useState`'s initializer, where React
 * has no recovery path at all.
 */
function read(): Note[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((entry): entry is Note =>
        Boolean(entry) &&
        typeof (entry as Note).id === "string" &&
        typeof (entry as Note).body === "string",
      )
      .map((entry) => ({ id: entry.id, body: entry.body, updatedAt: Number(entry.updatedAt) || 0 }));
  } catch {
    return [];
  }
}

function write(notes: Note[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(notes));
  } catch {
    // Deliberately swallowed. Losing this session's edit is the cheap version of
    // the failure; taking the page down with it is not.
  }
}

const newId = () => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

export type NotesApi = {
  notes: Note[];
  /** Newest first. The order a list is read in, decided once. */
  ordered: Note[];
  create: () => string;
  save: (id: string, body: string) => void;
  remove: (id: string) => void;
};

export function useNotes(): NotesApi {
  const [notes, setNotes] = useState<Note[]>(read);

  // A second surface mounting while the first holds unsaved text would otherwise
  // overwrite it with whatever it last read. Re-reading on another window's storage
  // event is what keeps the two honest; the same-tab case cannot happen because the
  // two never mount together.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === STORAGE_KEY) setNotes(read());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const create = useCallback(() => {
    const note: Note = { id: newId(), body: "", updatedAt: Date.now() };
    setNotes((previous) => {
      const next = [note, ...previous];
      write(next);
      return next;
    });
    return note.id;
  }, []);

  const save = useCallback(
    (id: string, body: string) => {
      setNotes((previous) => {
        const existing = previous.find((note) => note.id === id);
        // No invented row: saving into a note that was deleted underneath this
        // editor would resurrect it with one line and no history.
        if (!existing) return previous;
        const next = previous.map((note) =>
          note.id === id ? { ...note, body, updatedAt: Date.now() } : note,
        );
        write(next);
        return next;
      });
    },
    [],
  );

  const remove = useCallback(
    (id: string) => {
      setNotes((previous) => {
        const next = previous.filter((note) => note.id !== id);
        write(next);
        return next;
      });
    },
    [],
  );

  const ordered = [...notes].sort((a, b) => b.updatedAt - a.updatedAt);

  return { notes, ordered, create, save, remove };
}
