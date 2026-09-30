import { Icon } from "../components/Icon";

/**
 * The music card, and its honest answer.
 *
 * There is no player. Connecting one means an OAuth dance with a streaming
 * service, a credential that cannot live in the browser bundle, and an audio
 * source the app's own CSP does not currently permit — none of which is a
 * weekend of work, and all of which would be a lie to postpone. So this is a
 * designed empty state rather than a hidden card: it says what is missing in a
 * line, instead of pretending a control exists or vanishing quietly.
 *
 * It sits last in the band on purpose. Four cards whose subject is absence would
 * be a page about what the app cannot do, and this is the only one of them.
 */
export function MusicWidget() {
  return (
    <section className="widget-card widget--music">
      <header className="widget-head flex items-center justify-between gap-2.5 min-w-0">
        <span className="widget-title text-faint text-[10.5px] font-semibold tracking-[0.09em] uppercase">Music</span>
        <span className="widget-aside inline-flex items-center gap-1.5 min-w-0 overflow-hidden text-faint text-[11px] text-ellipsis whitespace-nowrap"><Icon name="music" size={15} weight="fill" /></span>
      </header>
      <div className="widget-body">
        <p className="widget-empty-line m-0 text-foreground text-[14px] font-semibold tracking-[-0.02em]">Nothing playing</p>
        <p className="widget-caption faint">No service connected.</p>
      </div>
      <footer className="widget-foot flex items-center gap-2 mt-auto pt-0.5">
        <button type="button" className="widget-button" disabled>
          <Icon name="music" size={14} />
          Connect a service
        </button>
      </footer>
    </section>
  );
}
