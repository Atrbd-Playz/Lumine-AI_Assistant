import { STATE_COPY } from "../constants";
import type { LumineState } from "../types";
import { getNextState } from "../utils";
import { Icon } from "./Icon";
import { Presence } from "./Presence";
import { ConversationToggle } from "./ConversationToggle";

/** Main command surface: status, presence, quick intents, and voice controls. */
export function MainSpace({ state, setState, cursorGaze, showAvatarColor, conversationOpen, onConversationToggle }: { state: LumineState; setState: (state: LumineState) => void; cursorGaze: boolean; showAvatarColor: boolean; conversationOpen: boolean; onConversationToggle: () => void }) {
  const copy = STATE_COPY[state];
  return <main className="main-space">
    <header className="topbar"><div><p className="eyebrow">Monday · 07 September</p><h1>Command space</h1></div><ConversationToggle open={conversationOpen} onClick={onConversationToggle} /></header>
    <section className="command-stage"><div className="stage-kicker"><span className="signal-dot" />{copy.eyebrow}</div><Presence state={state} cursorGaze={cursorGaze} showAvatarColor={showAvatarColor} /><div className="stage-copy" key={state}><h2>{copy.title}</h2><p>{copy.detail}</p></div><div className="stage-context context-left"><span>Today</span><strong>03</strong><small>tasks in focus</small></div><div className="stage-context context-right"><span>Next reminder</span><strong>09:00</strong><small>Physics notes</small></div><div className="stage-line" /></section>
    <section className="command-bottom"><div className="feature-surface"><div><p className="eyebrow">Now playing</p><h3>Ambient focus</h3><p className="subtle">Soft light for the next hour</p></div><button className="text-button"><Icon name="music" size={15} />Adjust</button></div><div className="quick-actions"><p className="eyebrow">Quick intent</p><div><button onClick={() => setState("thinking")}>Plan my evening <span>↗</span></button><button onClick={() => setState("listening")}>Capture a thought <span>↗</span></button></div></div></section>
    <footer className="voice-dock"><button className="circle-button" disabled={state === "idle"} onClick={() => setState("idle")} aria-label="Stop"><Icon name="stop" size={14} /></button><button className={`voice-button ${state === "listening" ? "is-listening" : ""}`} onClick={() => setState(getNextState(state))} aria-label="Change Lumine state"><Icon name="mic" size={24} /></button><button className="circle-button" onClick={() => setState("thinking")} aria-label="More actions"><Icon name="more" /></button><span>Tap to speak · Hold for a note</span></footer>
  </main>;
}
