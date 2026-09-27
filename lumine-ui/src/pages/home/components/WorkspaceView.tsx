import { useEffect, useMemo, useState } from "react";
import { getToolCatalog, type CatalogTool, type ToolCategory } from "../../../features/settings/aiConfigClient";
import { TabStrip, type TabStripItem } from "../../../components/ui/tabstrip";
import { Hint } from "../../../components/ui/hint";
import { Icon } from "./Icon";

type WorkspaceKind = "tools" | "memory" | "activity";

const CONTENT: Record<WorkspaceKind, { eyebrow: string; title: string; detail: string; icon: "tools" | "memory" | "activity" }> = {
  tools: { eyebrow: "Lumine workspace", title: "Tools", detail: "Everything Lumine can reach on her own. She decides when to use one.", icon: "tools" },
  memory: { eyebrow: "Lumine workspace", title: "Memory", detail: "A quiet place for the things Lumine is allowed to remember.", icon: "memory" },
  activity: { eyebrow: "Lumine workspace", title: "Activity", detail: "A calm timeline of meaningful moments with Lumine.", icon: "activity" },
};

type ToolState =
  | { kind: "loading" }
  /** The helper could not be read, which is not the same as "no tools". */
  | { kind: "unavailable" }
  | { kind: "ready"; tools: CatalogTool[]; categories: ToolCategory[] };

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
      {kind === "tools" ? <ToolsPage /> : <EmptyWorkspace kind={kind} onSettings={onSettings} />}
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
 *
 * Memory and Activity stay empty states. They are placeholders for capabilities
 * that do not exist yet, and inventing content for them would be the same lie in
 * a more convincing costume.
 */
function ToolsPage() {
  const [state, setState] = useState<ToolState>({ kind: "loading" });
  const [tab, setTab] = useState(ALL);

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
  const off = state.tools.filter((tool) => !tool.enabled).length;
  const active = tabs.find((entry) => entry.id === tab);

  return (
    <div className="tools-panel">
      <TabStrip items={tabs} value={tab} onChange={setTab} label="Tool categories" hintSide="bottom" />
      {off > 0 && (
        <p className="tools-note">
          {off} {off === 1 ? "tool is" : "tools are"} switched off in this environment.
        </p>
      )}
      {shown.length === 0 ? (
        <p className="tools-note">No tools in this group.</p>
      ) : (
        <>
          {active && active.help && <p className="tools-group-note">{active.help}</p>}
          <ul className="tool-grid">
            {shown.map((tool) => (
              <ToolCard key={tool.id} tool={tool} />
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
        The list is generated from the agent rather than stored here, so it is empty
        whenever the worker cannot be asked. That is a worker that has not started
        yet, or a Python environment that is missing.
      </p>
    </div>
  );
}

function ToolCard({ tool }: { tool: CatalogTool }) {
  // Named rather than a bare boolean, so a card cannot quietly lose the
  // distinction between these. Four of the five only answer a question; the one
  // that starts a program is the reason this view is worth having at all.
  const effect = tool.effect === "opens" ? "Acts on this device" : "Answers only";

  return (
    <li className={"tool-card" + (tool.enabled ? "" : " is-off")}>
      <header>
        <h3>{tool.label}</h3>
        {!tool.enabled && (
          <span className="tool-tag is-off" title={tool.disabledReason}>
            Off
          </span>
        )}
      </header>
      <p className="tool-summary">{tool.summary}</p>
      <ul className="tool-facts">
        <li className={"tool-fact tool-fact-effect is-" + tool.effect}>{effect}</li>
        {tool.network && <li className="tool-fact">Uses the internet</li>}
      </ul>
      {tool.parameters.length > 0 && (
        <dl className="tool-params">
          {tool.parameters.map((parameter) => (
            <div key={parameter.name}>
              <dt>
                {parameter.name}
                {parameter.required ? "" : " (optional)"}
              </dt>
              <dd>{parameter.help}</dd>
            </div>
          ))}
        </dl>
      )}
      {/* The id the model actually receives. Not decoration: it is how a report
          of "she used get_weather" is matched to the card above it. */}
      <code className="tool-id">{tool.id}</code>
    </li>
  );
}

function EmptyWorkspace({ kind, onSettings }: { kind: WorkspaceKind; onSettings: () => void }) {
  const content = CONTENT[kind];
  return (
    <section className={`workspace-empty workspace-empty-${kind}`}>
      <span className="workspace-empty-icon">
        <Icon name={content.icon} size={22} />
      </span>
      <h2>{kind === "memory" ? "Memory is not available yet" : "No activity to show"}</h2>
      <p>
        {kind === "memory"
          ? "Lumine does not have persistent memory in this build. Nothing is being stored here."
          : "Your meaningful sessions, voice moments, and completed actions will appear here when they are available."}
      </p>
      {kind === "activity" && (
        <button className="workspace-action" onClick={onSettings}>
          Review settings
        </button>
      )}
    </section>
  );
}
