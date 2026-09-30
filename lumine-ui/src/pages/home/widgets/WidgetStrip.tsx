import { Icon } from "../components/Icon";
import { WeatherWidget } from "./WeatherWidget";
import { NewsWidget } from "./NewsWidget";
import { MusicWidget } from "./LocalWidgets";
import type { NotesApi } from "../notes/useNotes";

/**
 * The everyday band, under the stage and above the call.
 *
 * ## Why this is a band and not a column
 *
 * It was a 300px rail on the right, which is the one place on a screen where a
 * column of things competes directly with the thing the page is for. A band
 * underneath has a different relationship with the avatar: it is where the eye
 * goes *after*, it takes height the stage was never going to use anyway, and it
 * can be as wide as the content needs — which a news list and a weather card both
 * do, and a 300px column does not.
 *
 * ## The order, and why news is wider
 *
 * Weather first because it is the one thing read without deciding to read it.
 * News second and double-width, because a headline needs about 45 characters a
 * line to stay two lines and a 240px column gives it a line and a half. Notes
 * third: a preview of what was written last, and the door into the surface that
 * writes it. Music last, honest about not being wired up.
 *
 * Nothing here reaches the voice session, for the same reason the rail did not —
 * a widget that only works during a call is a call accessory, not an everyday
 * thing.
 */
export function WidgetStrip({
  notes,
  onOpenNotes,
  onWriteNote,
}: {
  notes: NotesApi;
  onOpenNotes: () => void;
  onWriteNote: () => void;
}) {
  const latest = notes.ordered[0];
  const preview = latest ? latest.body.split(/\n/).filter((line) => line.trim()).slice(0, 3) : [];

  return (
    <section className="widget-strip" aria-label="Everyday widgets">
      <div className="widget-strip-inner">
        <WeatherWidget />

        <NewsWidget />

        <section className="widget-card widget--notes">
          <header className="widget-head flex items-center justify-between gap-2.5 min-w-0">
            <span className="widget-title text-faint text-[10.5px] font-semibold tracking-[0.09em] uppercase">Notes</span>
            {latest && (
              <span className="widget-aside inline-flex items-center gap-1.5 min-w-0 overflow-hidden text-faint text-[11px] text-ellipsis whitespace-nowrap">
                {new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(latest.updatedAt))}
              </span>
            )}
          </header>
          <div className="widget-body">
            {preview.length > 0 ? (
              <button
                type="button"
                className="widget-note-preview"
                onClick={onOpenNotes}
                title="Open notes"
                /* Named, because the alternative name is three lines of whatever
                   was written last. A button whose accessible name is the body
                   text of a note is a link a screen reader reads the whole note
                   out to reach, and this is the only control in the strip whose
                   visible text is not its label. */
                aria-label="Open notes"
              >
                {preview.map((line, index) => (
                  <span key={index}>{line}</span>
                ))}
              </button>
            ) : (
              <p className="widget-caption faint">Nothing written yet.</p>
            )}
          </div>
          <footer className="widget-foot flex items-center gap-2 mt-auto pt-0.5">
            <button type="button" className="widget-button" onClick={latest ? onOpenNotes : onWriteNote}>
              <Icon name={latest ? "note" : "plus"} size={14} />
              {latest ? "Open" : "Write"}
            </button>
          </footer>
        </section>

        <MusicWidget />
      </div>
    </section>
  );
}
