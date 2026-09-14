import { STATE_COPY } from "../constants";
import type { LumineState } from "../types";
import { Icon } from "./Icon";
import { Presence } from "./Presence";
import { ConversationToggle } from "./ConversationToggle";
import type { LumineConnectionState } from "../../../hooks/useLumineSession";

/** Main command surface: status, presence, quick intents, and voice controls. */
export function MainSpace({ state, cursorGaze, showAvatarColor, conversationOpen, onConversationToggle, sessionStatus, onConnect, onDisconnect, glassMode, onGlassModeToggle, muted, onMuteToggle }: { state: LumineState; cursorGaze: boolean; showAvatarColor: boolean; conversationOpen: boolean; onConversationToggle: () => void; sessionStatus: LumineConnectionState; onConnect: () => void; onDisconnect: () => void; glassMode: boolean; onGlassModeToggle: () => void; muted: boolean; onMuteToggle: () => void; }) {
  const copy = STATE_COPY[state];
  const today = new Intl.DateTimeFormat(undefined, { weekday: "long", day: "2-digit", month: "long" }).format(new Date());
  const active = sessionStatus === "online" || sessionStatus === "listening" || sessionStatus === "speaking" || sessionStatus === "reconnecting";
  const pending = sessionStatus === "connecting" || sessionStatus === "initializing" || sessionStatus === "waiting" || sessionStatus === "disconnecting";
  const agentLabel = sessionStatus === "idle" ? "Lumine · offline" : sessionStatus === "waiting" ? "Lumine · waiting for agent" : sessionStatus === "reconnecting" ? "Lumine · reconnecting" : sessionStatus === "error" ? "Lumine · connection error" : `Lumine · ${sessionStatus}`;

  return <main className="main-space">
    <header className="topbar"><div><p className="eyebrow">{today}</p><h1>Command space</h1></div><div className="topbar-actions"><button className={`visual-mode-toggle ${glassMode ? "is-active" : ""}`} onClick={onGlassModeToggle} aria-pressed={glassMode} aria-label={glassMode ? "Switch to classic presentation" : "Switch to glass presentation"} title={glassMode ? "Classic presentation" : "Glass presentation"}><Icon name="presentation" size={17} /></button><ConversationToggle open={conversationOpen} onClick={onConversationToggle} /></div></header>
    <section className="command-stage"><div className="stage-kicker"><span className="signal-dot" />{copy.eyebrow}</div><Presence state={state} cursorGaze={cursorGaze} showAvatarColor={showAvatarColor} /><div className="stage-copy" key={state}><h2>{copy.title}</h2><p>{copy.detail}</p></div><div className="stage-context context-left"><span>Today</span><strong>03</strong><small>tasks in focus</small></div><div className="stage-context context-right"><span>Next reminder</span><strong>09:00</strong><small>Physics notes</small></div><div className="stage-line" /></section>
    <section className="command-bottom"><div className="feature-surface"><div><p className="eyebrow">Now playing</p><h3>Ambient focus</h3><p className="subtle">Soft light for the next hour</p></div><button className="text-button"><Icon name="music" size={15} />Adjust</button></div><div className="quick-actions"><p className="eyebrow">Quick intent</p><div><span className="subtle">Voice session controls are ready below.</span></div></div></section>
    <footer className="voice-dock"><button className="circle-button end-session-button" disabled={!active && !pending} onClick={onDisconnect} aria-label="End Lumine session" title="End session"><Icon name="stop" size={14} /></button><button className={`voice-button ${state === "listening" ? "is-listening" : ""} ${active ? "is-active" : ""} ${pending ? "is-pending" : ""}`} onClick={active ? onDisconnect : pending ? undefined : onConnect} aria-label={active ? "End Lumine session" : pending ? "Lumine session is starting" : "Start Lumine session"} disabled={pending}><Icon name="mic" size={24} /></button><button className={`circle-button mute-button ${muted ? "is-muted" : ""}`} disabled={!active} onClick={onMuteToggle} aria-pressed={muted} aria-label={muted ? "Unmute microphone" : "Mute microphone"} title={muted ? "Unmute microphone" : "Mute microphone"}><Icon name={muted ? "mic-off" : "mic"} size={16} /></button><span>{muted ? `${agentLabel} · muted` : agentLabel}</span></footer>
  </main>;
}
