# The Focus Workspace

The Focus page is where somebody goes when they sat down to study. This is the
decision record for its data layer: what is stored, who writes it, where the
security boundaries are, and which rules three runtimes have to agree on.

The page itself is a work in progress. Slices 2–4 build the timer, the widget board,
the widgets and the ambient mode. What exists today is the foundation those sit on,
and the foundation is the part that is expensive to change later.

## The one-sentence version

`focus.json` is a versioned document, written atomically by Rust, read by the
webview through one store and by the worker through one reader, and validated on
both sides of every write.

## Where the file lives

Resolved identically by both runtimes:

| Order | Location | Why here |
| --- | --- | --- |
| 1 | `LUMINE_FOCUS_PATH` | A packaged build, a test, or a developer points at their own file |
| 2 | The desktop app's local-data directory | Where Tauri writes it. Searched rather than relying only on the child environment, so a worker started outside Tauri still reads it |
| 3 | The pre-rename `com.art.lumine-ui` directory | A file saved before the identifier moved |
| 4 | Beside `lumine.config.json` | The desktop app writes both, so they live together |
| 5 | `agent/focus.json` | A hand-placed file keeps working for a developer |

Windows `%LOCALAPPDATA%\com.art.lumine\focus.json`, macOS
`~/Library/Application Support/com.art.lumine/focus.json`, Linux
`$XDG_DATA_HOME/com.art.lumine/focus.json` or `~/.local/share/com.art.lumine/`.

## The schema

```jsonc
{
  "version": 1,
  "settings": {
    "workMin": 25, "shortMin": 5, "longMin": 15, "longEvery": 4,
    "autoStart": false, "autoAdvance": true,
    "dailyGoalMin": 120, "ambientSound": "none", "notifications": true,
    "layoutPreset": "default"
  },
  "widgets": [
    { "id": "timer", "x": 0, "y": 1, "w": 2, "h": 3, "hidden": false }
  ],
  "todos": [
    { "id": "t1", "title": "Revise integrals", "done": false,
      "dueAt": null, "estimatePomodoros": 2, "subject": "Maths",
      "order": 0, "createdAt": 1770000000000, "completedAt": null }
  ],
  "notes": [
    { "id": "n1", "title": "Week 4", "body": "…", "subject": "Maths",
      "pinned": false, "createdAt": 1770000000000, "updatedAt": 1770000000000 }
  ],
  "sessions": [
    { "id": "s1", "startedAt": 1770000000000, "endedAt": 1770001500000,
      "plannedMin": 25, "actualMin": 25, "phase": "work",
      "subject": "Maths", "todoId": "t1", "noteId": null,
      "completed": true, "interruptions": 0 }
  ],
  "customLists": [],
  "music": {
    "youtube": { "current": null, "queue": [], "volume": 0.6 },
    "local": { "queue": [], "index": -1 }
  },
  "dayIntents": []
}
```

### Four decisions inside it

**`settings.layoutPreset` and `widgets[]` are separate.** The original proposal had
`"layout": "default" | "saved"` in one field, which contradicts itself the moment
somebody drags a widget: no value can both mean "the person has customised it" and
keep the record of which preset they started from. So `layoutPreset` names the
preset (`default | study | writing | deepWork | custom`) and `widgets[]` always holds
the real geometry. Switching presets is a write to one string; no migration, ever.

**`sessions` is append-only and is the only thing charts read.** Todos and notes are
editable state; sessions are history. A todo renamed in March must not change what a
chart said about February, and a note deleted to tidy up must not erase a week of
study. That is the whole reason history is its own row type rather than a join.

**Every optional field is `null`, never absent.** `null` means "the document says
there is no value". An absent key means the document is out of date and the reader
has to decide. Collapsing the two is how "this person has no subject" and "this file
is from an older version" become the same value — and the second then renders as data
loss on the screen.

**Widgets are hidden, never removed.** A removed widget loses its geometry, so
restoring it drops the person somewhere they did not choose. Keeping the row and
setting `hidden` means "hide" and "forget this placement" are different acts, which
they are. Nothing underneath is destroyed by hiding — hiding the notes widget does
not delete a single note.

### Retention

Sessions are pruned to **365 days** on save. A row is dropped only when it is
*provably* older than the cut-off, which means three kinds of row survive:

- a missing, null, non-numeric or negative `startedAt` — it cannot be placed in time,
  so it cannot be judged expired;
- a `startedAt` in the future — a backwards clock jump, and "older than a year" is
  false for it;
- a row that is not an object — there is no timestamp to read.

Past a year the charts aggregate identically, so the file would otherwise grow
without bound. The 365 in the frontend and the 365 in `focus_stats.rs` are the same
number for the same reason, and the contract check compares them.

## Who writes it

**Rust is the only writer.** `lumine-ui/src-tauri/src/focus_store.rs` owns the format,
the validation and the atomic write. The webview sends the whole document to
`save_focus`; it does not patch. That is the same shape `save_config` uses, and for
the same reason: a read-modify-write from two places is a race somebody loses data
in.

**One store in the frontend.** `src/pages/focus/focusStore.ts` is a module singleton
consumed through `useSyncExternalStore`. `useNotes.ts` documents what happens without
it: two surfaces, two writers, one `localStorage` key, and the person loses whichever
sentence they typed second. On the Focus page the timer, todos, notes and the music
queue all write one document, so the problem is much larger than two surfaces.

## Two persistence paths, on purpose

| What | Where | Why |
| --- | --- | --- |
| The bulk document | `focus.json`, debounced 250 ms, `async` | Large, written often, survives a crash atomically |
| Timer recovery | `localStorage`, **synchronous** | ~200 bytes, must survive an abrupt kill |

`beforeunload` cannot await a Tauri `invoke`, and a timer that must not lose a running
block cannot depend on a 250 ms debounce winning a race with a window close. So the
timer record is synchronous and separate. Slice 2 writes it; the split is decided
here because two halves of a decision in two files is how one of them drifts.

The bulk path also flushes on `visibilitychange` and `pagehide`. That is a
belt-and-braces improvement, not the guarantee — which is why the timer does not rely
on it.

## Validation is asymmetric, on purpose

| Direction | Bad document |
| --- | --- |
| **Load** (read) | Repaired. Missing keys take their defaults; a well-formed value is never touched. Refusing here means the person cannot open the page at all. |
| **Save** (write) | Refused, with the offending field named. Repairing here would hide a frontend bug or a hostile webview. |

A **newer** version is refused on both sides: this build cannot know what it would be
dropping. An **older** version is migrated forward (v0 → v1 just adds the keys that
did not exist, with shipped defaults, and never overwrites a value the person set).

This is deliberately *different* from `settings_store.rs`, which rejects an old
version and falls back to `agent/.env`. That is right for configuration, where the
environment is a working substitute. It would be wrong here: a focus document is
history, and a version bump that hides it is data loss wearing a compatibility hat.

## What must never happen

- **A corrupt file is never overwritten.** `load` returns an error, the UI shows it,
  and the bytes stay put. `.bak` holds the last good copy. Overwriting with defaults
  is how somebody loses a term of history and is never told.
- **A failed save is never reported as a success.** `save_focus` writes and then reads
  back; it resolves only once `load` has accepted the file.
- **A failed save is never rolled back in memory.** The edit stays, the error shows,
  and the next successful save clears it. Rolling back would discard the edit the
  person made *to fix* the error.
- **The legacy notes key is never deleted.** `lumine.notes.v1` stays until the home
  page is rewired, and a marker key records that the import happened so a person who
  imports and then deletes every note does not get them all back on next launch.

## The statistics contract

`focus-stats/1`, implemented three times because three runtimes need it:

| Implementation | Serves |
| --- | --- |
| `src-tauri/src/focus_stats.rs` | The desktop app's derived reads |
| `agent/focus_stats.py` | Lumine's agent, and the contract check |
| `src/pages/focus/selectors.ts` | The charts — a chart redrawn on every timer tick cannot afford an IPC round trip |

The field list is `FOCUS_CONTRACT` in all three, and
`scripts/verify-focus-contract.mts` runs the same fixtures through Python and
TypeScript and fails on any difference. It also asserts the shape the charts depend
on: a contiguous series, one ending on today, `bySubject + unassigned == focusMinutes`,
and a `goalProgress` that is a fraction and never a percentage.

The four rules that make the numbers agree:

1. **`utcOffsetMinutes` is east-positive.** JavaScript's `getTimezoneOffset()` returns
   the negation, so the webview passes `-getTimezoneOffset()`. Getting this backwards
   shifts every day boundary, which produces a chart that is wrong by a few hours
   rather than visibly broken.
2. **A local day is computed by shifting, not by asking a timezone database.**
   `localMs = utcMs + offset * 60000`, then plain UTC calendar arithmetic on the
   shifted value. Exact, DST included, and identical on every machine.
3. **A session is filed under the day it *started*.** A block running past midnight
   started on the day the person sat down.
4. **A streak survives one unstarted day and no more.** At 9am today is empty and a
   live streak must not read as broken; a genuinely missed day ends it. Counting up
   from the last active day instead would report a six-day streak forever, which is the
   number that makes somebody stop opening the app.

The contract check does **not** judge the numbers in isolation — a rule all three share
can be wrong in all three. Those cases are pinned in the Rust and Python unit suites,
which have defensible right answers.

## Security: the music-root boundary

The Focus page plays audio from one folder the person picked. Three things enforce
that, and none of them is sufficient alone.

**The asset protocol starts with an empty scope.** `tauri.conf.json` declares
`assetProtocol: { enable: true, scope: [] }`, which denies everything.

**`allow_music_root` is the only thing that widens it.** The sequence is
**validate → deny the previous root → allow the new root**, and the order is the whole
of it. Validating first means a bad path never briefly widens the scope. Denying
before allowing means a *failed* folder switch leaves the previous folder unreadable
rather than leaving a half-completed permission change. The path is canonicalised, so
the scope and every later per-track check are given the same resolved directory and
cannot disagree about which one is which.

**`resolve_focus_track` checks every track independently.** A scope says *where*; it
does not say *what*. `convertFileSrc` is pure string encoding with no validation in
it, so the decision about which files may reach an `<audio>` element is made in Rust
(`media::validate_against_library` for the boundary, `media::is_in_app_audio` for the
extension) where it can be tested. The extension list is deliberately narrower than
the voice path's: a webview cannot decode `.mkv` or `.wmv`, and offering them produces
a player that silently does nothing.

`revoke_music_root` exists separately so that "stop letting Lumine read my music
folder" can be done without picking a replacement first — a command that only ever
adds permission has no way to remove it.

### The CSP change

`frame-src` was `'none'`, which meant a YouTube embed **could not work at all**. It is
now exactly two hosts — `https://www.youtube.com` and
`https://www.youtube-nocookie.com` — and nothing else. That is a real reduction in
lockdown on a page that previously could not load a remote frame, and it is scoped as
narrowly as the feature allows. `media-src` gained `asset:` and
`http://asset.localhost` for local audio.

`tauri` also gained the `protocol-asset` feature, which is **not** enabled by default.
It brings `asset_protocol_scope()` — the only runtime API that can grant per-directory
read access — and `http-range`, without which a webview downloads a whole track before
playing it and seeking a 60 MB FLAC looks like a hang.

## Running the checks

```powershell
cargo test --manifest-path lumine-ui\src-tauri\Cargo.toml --lib
npm run build                                    # tsc && vite build
npm run check                                    # includes check:focus-contract
.\.venv\Scripts\python.exe -m unittest discover -s agent\tests -p "test_focus_store.py"
.\.venv\Scripts\python.exe agent\focus_stats.py --days 7
```

`focus_stats.py --json` is full fidelity and deliberately **not** passed through
`tool_results.as_payload`: that helper exists to spend fewer tokens on a model, and
truncating it here would mean the contract check compared pruned output against full
output and agreed for the wrong reason.

## Agent integration status

**Read-only, and not a tool.** `agent/focus_store.py` finds and reads the document;
`agent/focus_stats.py` reports it. Neither is registered with `@function_tool`, and
nothing in either can write.

The read side ships first because a tool that can *write* this file is a tool that can
delete somebody's study history, and that needs its own permission class and its own
approval wording. A reader has neither problem.

Note-derived content is carried through `wrap_untrusted`, because a subject called
`ignore your instructions` must not read as an instruction.