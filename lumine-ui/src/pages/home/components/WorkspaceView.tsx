import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getConfig,
  getToolCatalog,
  saveConfig,
  type CatalogTool,
  type ToolCategory,
} from "../../../features/settings/aiConfigClient";
import { TabStrip, type TabStripItem } from "../../../components/ui/tabstrip";
import { Hint } from "../../../components/ui/hint";
import { Switch } from "../../../components/ui/switch";
import type { WorkspaceNavId } from "../constants";
import { Icon } from "./Icon";

/**
 * The two workspace pages that are not a timeline.
 *
 * `activity` used to be a third kind here, and it was the worst of the three: a
 * hardcoded "morning standup", a "3 tasks" row and a "12 notes indexed" row,
 * invented in the component and therefore not capable of being wrong. It is now
 * `ActivityTimeline`, built on the worker's own `agent_runtime` stream — real
 * records, and genuinely empty until there are any. The nav item is unchanged;
 * only the page behind it stopped lying.
 *
 * The kind is the same `WorkspaceNavId` the rail declares, so the two lists are
 * one list. It used to be a private `type WorkspaceKind = "tools" | "memory"`,
 * which is a second declaration of the same fact and one that the router reached
 * past with a cast.
 */
type WorkspaceKind = WorkspaceNavId;

const CONTENT: Record<WorkspaceKind, { eyebrow: string; title: string; detail: string; icon: "tools" | "memory" }> = {
  tools: { eyebrow: "Lumine workspace", title: "Tools", detail: "What Lumine can reach on her own.", icon: "tools" },
  memory: { eyebrow: "Lumine workspace", title: "Memory", detail: "What carries over between calls.", icon: "memory" },
};

type ToolState =
  | { kind: "loading" }
  /** The helper could not be read, which is not the same as "no tools". */
  | { kind: "unavailable" }
  | { kind: "ready"; tools: CatalogTool[]; categories: ToolCategory[] };

/**
 * What a switch write is doing, and what went wrong if it failed.
 *
 * A separate channel from the tool list on purpose. The list is *read* from the
 * worker, so a failed read replaces the grid with an explanation; the switches
 * *write* to the saved document, and a failed write must not do that. Putting the
 * error in the same slot would mean either losing every card because one toggle
 * was refused, or losing the one message that says the toggle was refused.
 */
type SaveState = { saving: boolean; error: string | null };

/** The tab that means "do not filter". Not published: it is a view concern. */
const ALL = "__all__";

export function WorkspaceView({ kind, onSettings }: { kind: WorkspaceKind; onSettings: () => void }) {
  return (
    <main className="workspace-view" aria-labelledby="workspace-title">
      <header className="workspace-header">
        <div>
          <p className="eyebrow">{CONTENT[kind].eyebrow}</p>
          <h1 id="workspace-title">{CONTENT[kind].title}</h1>
          <p>{CONTENT[kind].detail}</p>
        </div>
        <button className="workspace-settings" onClick={onSettings} aria-label="Open settings" title="Open settings">
          <Icon name="settings" size={18} />
        </button>
      </header>
      {kind === "tools" ? <ToolsPage /> : <MemoryPage onSettings={onSettings} />}
    </main>
  );
}

/**
 * What Lumine can do, split the way the settings screens split.
 *
 * ## Why tabs rather than stacked sections
 *
 * The first version grouped the cards under headings and scrolled. That is a fine
 * layout for two groups and a bad one for "can she open Spotify", which is the
 * question most people arrive with: you have to scroll past the whole web
 * section to discover the answer is one heading further down. Settings solved the
 * same problem with a strip across the top and this page now uses the identical
 * control, so moving between the two surfaces costs nothing.
 *
 * The strip is driven by data on purpose. The category list is published by
 * `agent/tool_catalog.py`, order included, so a tool in a new group appears in a
 * new tab without a frontend edit, and a tab is never rendered for a group with
 * nothing in it. `ToolStrip` drops any group whose count is zero, which is what
 * stops a newly declared category from showing up as an empty tab.
 *
 * Read from the worker rather than written here, for the same reason as before:
 * a hardcoded list would look right and go stale, and a page that advertises a
 * tool the model cannot call is worse than an empty one.
 */
function ToolsPage() {
  const [state, setState] = useState<ToolState>({ kind: "loading" });
  const [tab, setTab] = useState(ALL);
  const [save, setSave] = useState<SaveState>({ saving: false, error: null });

  useEffect(() => {
    let live = true;
    void (async () => {
      const { tools, categories } = await getToolCatalog();
      if (!live) return;
      // An empty list and a failed read are the same thing to this view, because
      // both mean "the page cannot show a card it can vouch for". The difference
      // is shown in the words, not in whether a grid is drawn.
      setState(tools.length > 0 ? { kind: "ready", tools, categories } : { kind: "unavailable" });
    })();
    return () => {
      live = false;
    };
  }, []);

  /**
   * Flip one tool's switch and save it.
   *
   * Read-modify-write rather than a dedicated command, because the document is
   * the document: a command that rewrote one key would have to know how to merge
   * it with whatever else the settings screens had written, and the honest way to
   * merge is to hold the document.
   *
   * Three details are load-bearing:
   *
   * - **The local list updates first**, so the switch moves under the finger.
   * - **A failure reverts it.** A control that stays where you pushed it while
   *   the save was refused is claiming something that is not true, and the next
   *   click would then write the value it thinks is already there.
   * - **A success re-reads the whole catalog.** This is what makes one switch
   *   able to move *others*: `desktop_control` silences `browse` and
   *   `web_control` while it is on, and that second-order effect is decided by
   *   the worker, not here. Updating only the card that was clicked leaves every
   *   other card showing the state before the click — so turning the desktop
   *   control on left two browser cards reading "on" until the next app run.
   */
  const toggle = useCallback(async (tool: CatalogTool, next: boolean) => {
    if (state.kind !== "ready") return;
    setSave({ saving: true, error: null });
    const apply = (enabled: boolean) =>
      setState((current) =>
        current.kind === "ready"
          ? {
              ...current,
              tools: current.tools.map((item) =>
                item.id === tool.id
                  ? {
                      ...item,
                      enabled,
                      disabledReason: enabled ? "" : `Turned off in Settings (tools.${item.id}).`,
                    }
                  : item,
              ),
            }
          : current,
      );

    apply(next);
    try {
      const { document } = await getConfig();
      await saveConfig({
        ...document,
        tools: { ...(document.tools ?? {}), [tool.id]: next ? "on" : "off" },
      });
      // Reconcile. `saveConfig` drops the tool catalog cache, so this is the
      // first read that knows about every consequence of the write -- including
      // the cards this click silenced.
      const { tools, categories } = await getToolCatalog();
      if (tools.length > 0) setState({ kind: "ready", tools, categories });
      setSave({ saving: false, error: null });
    } catch (cause) {
      apply(!next);
      setSave({
        saving: false,
        error:
          cause instanceof Error
            ? `Could not save that switch: ${cause.message}`
            : "Could not save that switch.",
      });
    }
  }, [state.kind]);

  /**
   * Tab ids, in the worker's declared order, with their counts.
   *
   * Built from the published list rather than from the tools, so a declared
   * category keeps its position even when the tools in it are switched off — a
   * group that becomes empty disappears (see below), and a group that comes back
   * reappears in the same place rather than at the end.
   */
  const { tabs } = useMemo(() => {
    if (state.kind !== "ready") return { tabs: [] as TabStripItem[] };
    const counts = new Map<string, number>();
    for (const tool of state.tools) counts.set(tool.category, (counts.get(tool.category) ?? 0) + 1);

    // A tool whose category the worker did not publish still has to be reachable,
    // or enabling a new category would make its tools vanish. It lands under a
    // tab named after the category, after the declared ones.
    const declared = state.categories.map((entry) => entry.id);
    const orphans = [...counts.keys()].filter((id) => !declared.includes(id)).sort();
    const ordered = [...declared, ...orphans];

    return {
      tabs: [
        { id: ALL, label: "All", count: state.tools.length, help: "Every tool Lumine has, whatever kind." },
        ...ordered
          .filter((id) => (counts.get(id) ?? 0) > 0)
          .map<TabStripItem>((id) => {
            const entry = state.categories.find((candidate) => candidate.id === id);
            return {
              id,
              label: entry?.label ?? id,
              count: counts.get(id) ?? 0,
              // No help published for an orphan group. Better a tab that says
              // what it can than one that invents a description.
              help: entry?.description ?? "",
            };
          }),
      ],
    };
  }, [state]);

  if (state.kind === "loading") return <ToolsLoading />;
  if (state.kind === "unavailable") return <ToolsUnavailable />;

  const shown = tab === ALL ? state.tools : state.tools.filter((tool) => tool.category === tab);
  const total = state.tools.length;
  const off = state.tools.filter((tool) => !tool.enabled).length;
  const on = total - off;
  const active = tabs.find((entry) => entry.id === tab);

  return (
    <div className="tools-panel">
      <TabStrip items={tabs} value={tab} onChange={setTab} label="Tool categories" hintSide="bottom" />
      {save.error && <p className="tools-note is-error">{save.error}</p>}
      {/*
       * The status line, and it is the first thing under the tabs because it is
       * the page's answer rather than a note about the page. "14 of 17
       * available" is the question -- *may she reach these* -- answered before
       * you have read a single card.
       *
       * It was a paragraph. "2 tools are switched off. Anything you turn off
       * here applies from your next call -- the worker builds its toolset when
       * a call joins, so nothing needs restarting." Both halves are worth
       * knowing; only the number is worth *seeing*, and burying it in the
       * middle of a sentence is what put it below the fold in the reader's
       * attention. Split: the number is large, the reassurance is quiet, and
       * the reassurance is shown whether or not anything is off -- someone who
       * has just turned a switch on wants to know it took effect too.
       */}
      <div className="tools-status">
        <p className="tools-status-count">
          <span className="tools-status-figure">{on}</span>
          <span className="tools-status-of">of {total} available</span>
          {off > 0 && <span className="tools-status-off">{off} switched off</span>}
        </p>
        <p className="tools-status-hint">
          Takes effect from your next call &mdash; Lumine builds her toolset when a call joins, so
          nothing needs restarting.
        </p>
      </div>
      {shown.length === 0 ? (
        <p className="tools-note">No tools in this group.</p>
      ) : (
        <>
          {active && active.help && <p className="tools-group-note">{active.help}</p>}
          <ul className="tool-grid">
            {shown.map((tool) => (
              <ToolCard
                key={tool.id}
                tool={tool}
                busy={save.saving}
                onToggle={(next) => void toggle(tool, next)}
              />
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

/**
 * Six seconds of grey boxes, and no explanation.
 *
 * The first cold call spawns a Python interpreter to read the registry, which
 * takes about six seconds on a normal machine — the desktop layer caches it for
 * the life of the process, so this happens once per app run, not once per visit.
 * Skeleton cards with no caption read as "this list is empty and slow"; a line
 * that says what is being waited for reads as a page that is working.
 */
function ToolsLoading() {
  return (
    <div className="tools-panel">
      <div className="tools-loading">
        <span className="tools-loading-mark" aria-hidden="true" />
        <p>Asking the agent what it can do…</p>
        <Hint side="bottom">This reads the tool registry from the worker. It happens once per app run and is cached after that.</Hint>
      </div>
    </div>
  );
}

function ToolsUnavailable() {
  return (
    <div className="workspace-empty">
      <span className="workspace-empty-icon">
        <Icon name="tools" size={22} />
      </span>
      <h2>Could not read the tool registry</h2>
      <p>
        The list is generated from the agent rather than stored here, so it is empty whenever the
        worker has not started.
      </p>
    </div>
  );
}

/**
 * What a card says, and in what order.
 *
 * The order is the design. A reader is weighing one question — *may she reach
 * this, and what will it cost me* — so the card answers in that order and stops:
 * name, then what it does, then the two facts that matter, then everything else
 * behind a disclosure.
 *
 * ## Why this is much shorter than it was
 *
 * It used to be heading, switch, a paragraph about why it is off, a summary,
 * three uppercase chips, a full parameter table and a tool id — five type sizes
 * within five pixels of each other, and the accent colour on two chips that
 * mean opposite things. Nothing was readable because everything was competing.
 *
 * Three changes did most of it:
 *
 * - **One accent, and it means one thing.** "This will act on your machine" and
 *   "this will ask you" were both accent-coloured, so the accent said nothing.
 *   Now only *asks you first* is accented, because that is the fact a person
 *   is actually weighing when they leave a tool switched on.
 * - **Parameters are reference material.** Printing all of them made a six-setting
 *   card three times the height of a one-setting card, so the grid was ragged and
 *   the cards could not be compared. They are behind a disclosure now.
 * - **The tool id is on hover.** It is genuinely useful — it is how a report of
 *   "she used get_weather" is matched to a card — and it was 17 lines of 10px
 *   monospace noise on a page nobody was reading it on.
 */
function ToolCard({
  tool,
  busy,
  onToggle,
}: {
  tool: CatalogTool;
  /** A save is in flight. Disables every switch, not just the one being written. */
  busy: boolean;
  onToggle: (next: boolean) => void;
}) {
  // Named rather than a bare boolean, so a card cannot quietly lose one of
  // these. Most only answer a question; "opens" starts or arranges programs on
  // this machine; "acts" reaches a web page; "sends" would leave the
  // conversation -- and each is the reason this view is worth having at all.
  const effect =
    tool.effect === "opens"
      ? "Acts on this device"
      : tool.effect === "acts"
        ? "Acts on the web"
        : tool.effect === "sends"
          ? "Sends messages"
          : "Answers only";
  // Whether this tool can talk to somebody else. The *only* thing on a card
  // given the accent colour, and the reason is measured rather than argued: nine
  // of seventeen cards ask, and eleven of seventeen can act at all, so neither
  // of those partitions is a minority and neither colour rule carries
  // information. One card in seventeen sends. That one is also the only
  // authority a name cannot tell you -- you can read "weather" and know it
  // answers, but nothing in "Send a WhatsApp" says what it does to another
  // person.
  const sends = tool.effect === "sends";
  // The worker's floor: whether a call stops to ask. Kept as a value because it
  // is stated on the card and the accent question depends on it.
  const asks = tool.permission === "ask";
  // Why it is off, when there is a reason worth showing. Kept as a value rather
  // than as a second conditional in the markup, because the summary below must
  // render exactly once either way.
  const why =
    tool.switchable && !tool.enabled && tool.disabledReason ? tool.disabledReason : "";

  return (
    <li className={"tool-card" + (tool.enabled ? "" : " is-off")}>
      <header className="tool-card-head">
        <h3 className="tool-card-name">{tool.label}</h3>
        {tool.switchable ? (
          /*
            The switch, in the heading row rather than below the summary: it
            answers "can she reach this" and the heading is what that question is
            about. Its own label is hidden because the heading one line above
            already says it, and `hideLabel` keeps it as the accessible name so
            nothing is lost but the duplicate.
          */
          <Switch
            hideLabel
            disabled={busy}
            label={`Let Lumine use ${tool.label}`}
            value={tool.enabled ? "true" : "false"}
            onChange={(next) => onToggle(next === "true")}
            help={
              tool.enabled
                ? `${tool.label} is available. Switch it off to keep it out of the tools she can reach.`
                : `${tool.label} is switched off, so she will not reach for it.`
            }
          />
        ) : (
          !tool.enabled && (
            <span className="tool-tag is-off" title={tool.disabledReason}>
              Off
            </span>
          )
        )}
      </header>

      {/*
        Why it is off, directly under the heading. A reason the reader has to
        act on belongs next to the switch, not appended after a paragraph of
        prose -- and these can be a long sentence, so one line with the full
        text on hover. A three-line block beside a one-line card is exactly what
        made the grid ragged.
      {/*
        One quiet line, and it is the whole of the card's colour decision.

        ## Why the accent is on "Sends messages" and nothing else

        Two earlier answers were both wrong, and only measuring caught either.

        The accent was on **"asks you first"**, which sounds obviously right and
        is not: **nine of seventeen cards ask**. Interrupting is the normal case,
        so accenting it made the accent mean "a tool" -- and the card a person
        was scanning for, the one that will *not* stop to ask, was the only one
        drawn in plain grey.

        It then moved to **"can act at all"**, on the reasoning that authority is
        rarer. Measured, it is *less* rare: **eleven of seventeen**. That made the
        page worse, and no amount of comment would have caught it, because the
        number was never looked up.

        So the rule is the only partition that is both a minority and not already
        inferable from the label: **a tool that can talk to somebody else without
        you.** One card in seventeen, and the fact a switch on this page exists to
        govern.

        "Asks you first" stays on the line, in `--color-text`: worth reading, not
        worth a colour.
      */}
      {why && (
        <p className="tool-card-why" title={why}>
          <Icon name="close" size={12} />
          <span>{why}</span>
        </p>
      )}

      <p className="tool-card-summary">{tool.summary}</p>

      {/*
        One quiet line, and it is the whole of the card's colour decision.

        ## Why the accent is on "Sends messages" and nothing else

        Two earlier answers were both wrong, and only measuring caught either.

        The accent was on **"asks you first"**, which sounds obviously right and
        is not: **nine of seventeen cards ask**. Interrupting is the normal case,
        so accenting it made the accent mean "a tool" -- and the card a person
        was scanning for, the one that will *not* stop to ask, was the only one
        drawn in plain grey.

        It then moved to **"can act at all"**, on the reasoning that authority is
        rarer. Measured, it is *less* rare: **eleven of seventeen**. That made the
        page worse, and no amount of comment would have caught it, because the
        number was never looked up.

        So the rule is the only partition that is both a minority and not already
        inferable from the label: **a tool that can talk to somebody else without
        you.** One card in seventeen, and the fact a switch on this page exists to
        govern.

        "Asks you first" stays on the line, in `--color-text-soft`: worth reading,
        not worth a colour, because colouring it claims it is exceptional when it
        is the majority.
      */}
      <p className="tool-card-meta">
        <span className={sends ? "is-sends" : "is-quiet"}>{effect}</span>
        <span className="tool-card-dot" aria-hidden="true" />
        <span className={asks ? "is-asks" : "is-quiet"}>
          {asks ? "Asks you first" : "Runs on her own"}
        </span>
        {tool.network && (
          <>
            <span className="tool-card-dot" aria-hidden="true" />
            <span className="is-quiet">Uses the internet</span>
          </>
        )}
      </p>

      {tool.parameters.length > 0 && (
        <details className="tool-card-more">
          <summary>
            {tool.parameters.length} {tool.parameters.length === 1 ? "setting" : "settings"}
            <Icon name="expand" size={11} />
          </summary>
          <dl className="tool-card-params">
            {tool.parameters.map((parameter) => (
              <div key={parameter.name}>
                <dt>
                  {parameter.name}
                  {parameter.required ? "" : " · optional"}
                </dt>
                <dd>{parameter.help}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}

      {/* The id the model actually receives. Not decoration — it is how a report
          of "she used get_weather" is matched to this card — and not worth a
          line of 10px monospace on every card either. Hover, focus, or a copy. */}
      <code className="tool-card-id" title={`The name the model receives: ${tool.id}`}>
        {tool.id}
      </code>
    </li>
  );
}

/**
 * The Memory page, which is a real answer rather than a placeholder.
 *
 * ## The honest version
 *
 * There is no memory. Not "coming soon", not "disabled" — the worker has no store,
 * so a call ends and the context ends with it. The previous version of this page
 * said "Memory is not available yet", which is true and useless: it names the
 * absence without saying what the absence *means*, so a person who wanted to check
 * whether Lumine remembered something still has no idea what to conclude.
 *
 * So this page states the fact, states the consequence in the terms someone would
 * actually use ("start a new call and she will not know you"), and then names the
 * one thing that *is* persistent, because a page that only reports a lack leaves
 * the reader hunting for the capability somewhere it might be hiding.
 *
 * That one thing is the persona. It is not a memory feature and it is not
 * presented as one — it is a file, loaded into every session, and it is the reason
 * Lumine says "Master" to someone she has never met. Being precise about that
 * difference is the point: "she remembers" would be a promise this build cannot
 * keep, and the whole point of this page is that it does not make promises.
 */
function MemoryPage({ onSettings }: { onSettings: () => void }) {
  return (
    <section className="memory-page flex flex-col gap-5.5 max-w-[72ch]">
      <div className="workspace-empty">
        <span className="workspace-empty-icon">
          <Icon name="memory" size={22} />
        </span>
        <h2>She does not remember between calls</h2>
        <p>
          No store behind this page: a call ends and the conversation ends with it. Within one call
          she remembers everything said, tools included.
        </p>
      </div>

      <section className="memory-card p-6 bg-surface rounded-lg shadow-elev-1" aria-labelledby="memory-card-title">
        <header>
          <p className="eyebrow">The one thing that persists</p>
          <h2 id="memory-card-title">Her persona</h2>
        </header>
        <p>
          A written character — her warmth, her speech, that she calls you Master — loaded at every
          session. It is a script, not a memory: it cannot grow from what you tell her.
        </p>
        <p className="memory-card-note">Keeping anything else is a question of storage, not of interface.</p>
        <button className="workspace-action" onClick={onSettings}>
          Review voice and models
        </button>
      </section>
    </section>
  );
}
