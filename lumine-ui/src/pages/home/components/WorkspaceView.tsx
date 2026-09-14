import { Icon } from "./Icon";

type WorkspaceKind = "tools" | "memory" | "activity";

const CONTENT: Record<WorkspaceKind, { eyebrow: string; title: string; detail: string; icon: "tools" | "memory" | "activity" }> = {
  tools: { eyebrow: "Lumine workspace", title: "Tools", detail: "Capabilities become available here as Lumine connects to them.", icon: "tools" },
  memory: { eyebrow: "Lumine workspace", title: "Memory", detail: "A quiet place for the things Lumine is allowed to remember.", icon: "memory" },
  activity: { eyebrow: "Lumine workspace", title: "Activity", detail: "A calm timeline of meaningful moments with Lumine.", icon: "activity" },
};

export function WorkspaceView({ kind, onSettings }: { kind: WorkspaceKind; onSettings: () => void }) {
  const content = CONTENT[kind];
  return <main className="workspace-view" aria-labelledby="workspace-title">
    <header className="workspace-header">
      <div><p className="eyebrow">{content.eyebrow}</p><h1 id="workspace-title">{content.title}</h1><p>{content.detail}</p></div>
      <button className="workspace-settings" onClick={onSettings} aria-label="Open settings" title="Open settings"><Icon name="settings" size={18} /></button>
    </header>
    <section className={`workspace-empty workspace-empty-${kind}`}>
      <span className="workspace-empty-icon"><Icon name={content.icon} size={22} /></span>
      <h2>{kind === "tools" ? "No tools connected" : kind === "memory" ? "Memory is not available yet" : "No activity to show"}</h2>
      <p>{kind === "tools" ? "When a supported tool is connected, Lumine will explain what it can do and ask for confirmation when needed." : kind === "memory" ? "Lumine does not have persistent memory in this build. Nothing is being stored here." : "Your meaningful sessions, voice moments, and completed actions will appear here when they are available."}</p>
      {kind === "tools" && <button className="workspace-action" onClick={onSettings}>Review settings</button>}
    </section>
  </main>;
}
