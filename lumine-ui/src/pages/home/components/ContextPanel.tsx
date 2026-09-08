import { useRef, useState, type KeyboardEvent } from "react";
import { INITIAL_ENTRIES } from "../constants";
import type { Entry, LumineState } from "../types";
import { Icon, Mark } from "./Icon";

/** Context rail with today's tasks and a lightweight message composer. */
export function ContextPanel({ state, onSettings }: { state: LumineState; onSettings: () => void }) {
  const [entries, setEntries] = useState<Entry[]>(INITIAL_ENTRIES);
  const [input, setInput] = useState("");
  const nextId = useRef(4);
  const submit = () => { const content = input.trim(); if (!content) return; setEntries((current) => [...current, { id: nextId.current++, kind: "message", content, time: "Now" }]); setInput(""); };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); } };
  return <aside className="context-panel"><header className="panel-head"><div><div className="panel-brand"><Mark /><span>Lumine</span><i /></div><p>Personal assistant · {state === "idle" ? "Online" : state}</p></div><button className="panel-icon" onClick={onSettings} aria-label="Open appearance settings"><Icon name="settings" size={17} /></button></header>
    <div className="context-scroll scroll-hidden"><section className="current-activity"><p className="section-label">Current activity</p><div className="activity-live"><span className="activity-icon"><Icon name="music" size={15} /></span><div><strong>Ambient focus is playing</strong><p>Low-volume study mix · 47 min left</p></div><button aria-label="More activity options"><Icon name="more" size={17} /></button></div></section>
      <section className="today-list"><p className="section-label">Today</p>{[["01", "Physics notes", "Due tonight · 9:00 PM"], ["02", "Project work", "Sketch the Lumine avatar direction"], ["03", "Study session", "Open focus block · 60 minutes"]].map(([number, title, detail]) => <div key={number}><span>{number}</span><p><strong>{title}</strong><small>{detail}</small></p><button aria-label={`Complete ${title}`}><Icon name="check" size={16} /></button></div>)}</section>
      <section className="recent"><p className="section-label">Recent context</p>{entries.map((entry) => <div className={`recent-entry ${entry.kind}`} key={entry.id}><span className="entry-icon"><Icon name={entry.kind === "action" ? "bell" : entry.kind === "note" ? "music" : "spark"} size={14} /></span><div><p>{entry.content}</p><small>{entry.time}</small></div></div>)}</section>
    </div><div className="composer"><div><textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={onKeyDown} placeholder="Add a thought for Lumine…" rows={1} /><div className="composer-actions"><button aria-label="Voice note"><Icon name="mic" size={16} /></button><button className={input.trim() ? "ready" : ""} onClick={submit} aria-label="Send"><Icon name="send" size={16} /></button></div></div><p>Enter to send · Shift + Enter for a new line</p></div>
  </aside>;
}
