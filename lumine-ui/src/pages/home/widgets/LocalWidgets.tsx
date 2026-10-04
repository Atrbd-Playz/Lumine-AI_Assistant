import { useCallback, useEffect, useState } from "react";

import { Icon } from "../components/Icon";
import { getConfig } from "../../../features/settings/aiConfigClient";
import { folderNameOf, queueMusic } from "../../../features/settings/musicClient";

/**
 * The music card, connected.
 *
 * ## What it is now
 *
 * A widget like Weather and News: it says which folder Lumine has permission for
 * and offers the one coarse action that does not need a list. There is **no track
 * list and no search box here, on purpose.** Both used to exist — as a settings
 * tab — and the reported complaint was *"there's so much loading in the music
 * settings"*. Half of that was two Python subprocesses to open a screen; the other
 * half was that most of the screen was a list of filenames, which is not a
 * setting and is not what this card is for.
 *
 * Choosing *which* folder she may read is a permission, so it lives in Settings →
 * Permissions. Everything here is something you would do without leaving the home
 * page.
 *
 * ## It reads nothing from the worker
 *
 * The folder's name is the last segment of a path string the saved document
 * already gave us, so **this card starts no subprocess and asks for no count.**
 * That is the whole of the loading fix on the home screen, and it is why the card
 * can be on the page at all: a widget that called `inspect_music_folder` on every
 * mount would put a ~400 ms interpreter start in front of the avatar.
 *
 * ## "Play all" queues; it does not launch
 *
 * One button, one queue, and the player's own window owns the order from there.
 * It used to start every file at once — nine players on top of each other and
 * nothing audible — because `os.startfile` takes one file and there was no list to
 * hand over. The line under the button reports what was queued and says where the
 * controls are, because a widget that said "playing" while the user hunted for the
 * window would be lying.
 */
export function MusicWidget({ onOpenPermissions }: { onOpenPermissions: () => void }) {
  const [library, setLibrary] = useState<string | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  /**
   * One read of the document, coalesced with every other caller and cached by the
   * desktop layer — so the home page costs one `invoke` for the whole session, not
   * one per widget mount.
   */
  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const { document } = await getConfig();
        if (!live) return;
        setLibrary((document.media?.library ?? "").trim() || null);
        setEnabled(document.media?.enabled !== "off");
      } catch {
        // A card that cannot read its own permission says nothing rather than
        // claiming there is no music. The voice path is unaffected either way, so
        // a failure here is not worth a toast.
        if (live) setLibrary(null);
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  const playAll = useCallback(async () => {
    setBusy(true);
    setNotice(null);
    try {
      const queued = await queueMusic();
      setNotice(queued.ok ? `${queued.queued} queued — ${queued.note}` : queued.error);
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "That could not be queued.");
    } finally {
      setBusy(false);
    }
  }, []);

  const ready = Boolean(library) && enabled;

  return (
    <section className="widget-card widget--music">
      <header className="widget-head flex items-center justify-between gap-2.5 min-w-0">
        <span className="widget-title text-faint text-[10.5px] font-semibold tracking-[0.09em] uppercase">Music</span>
        <span className="widget-aside inline-flex items-center gap-1.5 min-w-0 overflow-hidden text-faint text-[11px] text-ellipsis whitespace-nowrap">
          <Icon name="music" size={15} weight={ready ? "fill" : "regular"} />
        </span>
      </header>

      <div className="widget-body">
        {/*
          The folder's own name, or the one thing missing. Both are derived from
          the document rather than asked for, so this line is correct the instant
          the page paints.
        */}
        <p className="widget-empty-line m-0 text-foreground text-[14px] font-semibold tracking-[-0.02em] truncate">
          {library ? folderNameOf(library) : "No folder chosen"}
        </p>
        <p className="widget-caption faint">
          {!library
            ? "Give her one to play from."
            : !enabled
              ? "Music is switched off in Permissions."
              : "Ask her by name, or queue the lot."}
        </p>
        {/*
          The queue's own report, kept rather than cleared. Nine tracks went into
          the player's window and the user has to know it — the alternative was a
          button that appeared to do nothing while a queue quietly played.
        */}
        {notice && (
          <p className="music-notice text-[11.5px] leading-relaxed mt-1.5" role="status">
            {notice}
          </p>
        )}
      </div>

      <footer className="widget-foot flex items-center gap-2 mt-auto pt-0.5">
        <button type="button" className="widget-button" onClick={() => void playAll()} disabled={!ready || busy}>
          <Icon name="music" size={14} weight="fill" />
          {busy ? "Queueing…" : "Play all"}
        </button>
        <button
          type="button"
          className="widget-button widget-button--quiet"
          onClick={onOpenPermissions}
          title="Choose the folder Lumine may play from"
        >
          {library ? "Change folder" : "Choose folder"}
        </button>
      </footer>
    </section>
  );
}