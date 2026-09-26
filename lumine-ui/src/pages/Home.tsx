import { useState, type CSSProperties } from "react";
import "./index.css";
import { DEFAULT_APPEARANCE } from "./home/constants";
import { AppearanceDialog } from "./home/components/AppearanceDialog";
import { MainSpace } from "./home/components/MainSpace";
import { Sidebar } from "./home/components/Sidebar";
import { WorkspaceView } from "./home/components/WorkspaceView";
import type { LumineState } from "./home/types";
import { getReadableForeground, getReadableTextColor } from "./home/utils";
import { usePreferences } from "./home/hooks/usePreferences";
import { useConversation } from "./home/conversation/useConversation";
import { ConversationPanel } from "./home/components/ConversationPanel";
import type { Appearance } from "./home/types";
import { useLumineVoice } from "../features/voice/useLumineVoice";
import { DEFAULT_INTERRUPTION_MODE } from "../features/voice/interruption";
import { SettingsDialog } from "./settings/SettingsDialog";
import { ProvidersPage } from "./settings/ai/ProvidersPage";
import { VoiceModelsPage } from "./settings/ai/VoiceModelsPage";
import { DiagnosticsPage } from "./settings/system/DiagnosticsPage";
import { CONFIG_VERSION } from "../features/settings/aiConfigTypes";
import { useAiConfig } from "../features/settings/useAiConfig";
import AvatarLabPage from "./AvatarLabPage";

export default function Home() {
  const [nav, setNav] = useState(() => window.location.pathname === "/avatar" ? "avatar" : "home");
  const [glassMode, setGlassMode] = useState(() => localStorage.getItem("lumine.presentation-mode") === "glass");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<"appearance" | "voice" | "providers" | "diagnostics">("voice");
  const [conversationOpen, setConversationOpen] = useState(false);
  const [toast, setToast] = useState<{ tone: "success" | "error" | "info"; message: string } | null>(null);
  const conversation = useConversation();
  const { mode, setMode, cursorGaze, setCursorGaze, appearance, setAppearance, resetAppearance, presets, savePreset, importPresets, deletePreset } = usePreferences();
  const aiConfig = useAiConfig();
  // The active voice profile is authoritative once one is saved; until then the
  // environment default is what the worker will use, so the UI mirrors that.
  const interruptionMode =
    aiConfig.activeProfile?.kind === "realtime"
      ? (aiConfig.activeProfile.realtime?.turnHandling?.interruptionMode ?? DEFAULT_INTERRUPTION_MODE)
      : (aiConfig.activeProfile?.pipeline?.turnHandling?.interruptionMode ?? DEFAULT_INTERRUPTION_MODE);
  const handleToolEvent = (event: Parameters<typeof conversation.upsertToolEvent>[0]) => {
    conversation.upsertToolEvent(event);
    const tone = event.status === "completed" ? "success" : event.status === "failed" ? "error" : "info";
    const message = event.summary || `${event.name} ${event.status}`;
    setToast({ tone, message });
  };
  const session = useLumineVoice({ onMessage: conversation.addMessage, onUpdateMessage: conversation.updateMessage, onToolEvent: handleToolEvent, interruptionMode, onError: (message) => setToast({ tone: "error", message }) });

  const handleNavigation = (next: string) => {
    if (next === "conversation") {
      setNav("home");
      setConversationOpen(true);
      return;
    }
    setConversationOpen(false);
    setNav(next);
    window.history.pushState({}, "", next === "home" ? "/" : `/${next}`);
  };

  const sessionState: LumineState = session.status === "speaking" ? "speaking" : session.status === "listening" ? "listening" : session.status === "idle" || session.status === "error" ? "idle" : "thinking";
  const conversationStatus = session.status === "speaking" ? "speaking" : session.status === "listening" ? "listening" : session.status === "error" ? "error" : session.status === "idle" ? "idle" : "thinking";

  const customColors = Object.fromEntries(
    (["accent", "icon", "canvas", "surface", "stage", "text", "avatar"] as const)
      .filter((key) => mode === "light" || appearance[key] !== DEFAULT_APPEARANCE[key])
      .map((key) => {
        if (key === "icon") {
          const fallback = mode === "light" ? "#5f5c57" : "#aaa39b";
          const backgrounds = mode === "light" ? ["#fbfaf8", "#f4f3f0"] : ["#211f1d", "#181715"];
          return [`--color-${key}`, getReadableForeground(appearance[key], backgrounds, fallback, 3)];
        }
        if (key === "text") {
          const fallback = mode === "light" ? "#252525" : "#f1ece5";
          const backgrounds = mode === "light" ? ["#fbfaf8", "#f4f3f0"] : ["#211f1d", "#181715"];
          return [`--color-${key}`, getReadableForeground(appearance[key], backgrounds, fallback, 4.5)];
        }
        return [`--color-${key}`, appearance[key]];
      }),
  );
  const conversationColors = Object.fromEntries(
    (["chatSurface", "chatUser", "chatAssistant", "chatText", "chatAccent"] as const)
      .filter((key) => mode === "dark" || appearance[key] !== DEFAULT_APPEARANCE[key])
      .map((key) => [`--conversation-${key.replace("chat", "").toLowerCase()}`, appearance[key]]),
  );

  // Default dark tokens come from CSS; changed values continue to work in either mode.
  const variables = {
    ...customColors,
    ...conversationColors,
    ...(mode === "light" || appearance.accent !== DEFAULT_APPEARANCE.accent
      ? { "--color-accent-soft": `color-mix(in srgb, ${appearance.accent} 18%, white)` }
      : {}),
    "--font-ui": fontStack(appearance.font),
    "--conversation-font": fontStack(appearance.chatFont),
    "--voice-button-icon": getReadableTextColor(appearance.accent),
    "--color-stage-text": getReadableTextColor(
      mode === "light" || appearance.stage !== DEFAULT_APPEARANCE.stage ? appearance.stage : "#11100f",
    ),
  } as CSSProperties;

  const toggleGlassMode = () => setGlassMode((enabled) => { const next = !enabled; localStorage.setItem("lumine.presentation-mode", next ? "glass" : "classic"); return next; });

  return <div className={`lumine-app theme-${mode} route-${nav} ${glassMode ? "visual-glass" : "visual-classic"} ${conversationOpen && nav === "home" ? "conversation-open" : ""}`} style={variables}>
    {toast && <div className={`runtime-toast is-${toast.tone}`} role="status"><span className="runtime-toast-dot" /><span>{toast.message}</span><button onClick={() => setToast(null)} aria-label="Dismiss notification">×</button></div>}
    <Sidebar active={conversationOpen ? "conversation" : nav} onChange={handleNavigation} onSettings={() => setSettingsOpen(true)} />
    {nav === "home" ? <MainSpace
      state={sessionState}
      cursorGaze={cursorGaze}
      showAvatarColor={appearance.showAvatarColor}
      conversationOpen={conversationOpen}
      onConversationToggle={() => setConversationOpen((open) => !open)}
      sessionStatus={session.status}
      onConnect={() => { conversation.clearMessages(); void session.connect(); }}
      onDisconnect={() => { void session.disconnect(); }}
      glassMode={glassMode}
      onGlassModeToggle={toggleGlassMode}
      muted={session.muted}
      onMuteToggle={() => { void session.toggleMute(); }}
      emotion={session.emotion}
      showEmotionDebug={import.meta.env.VITE_LUMINE_DEBUG_EMOTION === "true"}
    /> : nav === "avatar" ? <AvatarLabPage /> : <WorkspaceView kind={nav as "tools" | "memory" | "activity"} onSettings={() => { setSettingsSection("voice"); setSettingsOpen(true); }} />}
    {conversationOpen && nav === "home" && <ConversationPanel messages={conversation.messages} items={conversation.items} agentStatus={conversationStatus} bubbleVariant={appearance.chatBubbleVariant} onClose={() => setConversationOpen(false)} onClear={conversation.clearMessages} onReset={conversation.resetMessages} onAddMessage={conversation.addMessage} />}
    {settingsOpen && <SettingsDialog initialSection={settingsSection} onClose={() => setSettingsOpen(false)}>
      {(section) => {
        if (section === "appearance") {
          return <AppearanceDialog mode={mode} setMode={setMode} cursorGaze={cursorGaze} setCursorGaze={setCursorGaze} appearance={appearance} setAppearance={setAppearance} presets={presets} savePreset={savePreset} importPresets={importPresets} deletePreset={deletePreset} onResetPalette={resetAppearance} onReset={() => { setMode("dark"); setCursorGaze(true); resetAppearance(); }} onClose={() => setSettingsOpen(false)} />;
        }
        if (aiConfig.state === "loading") {
          return <div className="settings-page"><p className="validation is-pending">Loading the AI configuration…</p></div>;
        }
        if (aiConfig.state === "error" || !aiConfig.catalog) {
          return (
            <div className="settings-page">
              <header className="settings-page-head"><div><p className="eyebrow">AI</p><h1>Voice &amp; Models</h1></div></header>
              <p className="validation is-error">{aiConfig.error ?? "The provider catalog is unavailable."}</p>
            </div>
          );
        }
        if (section === "providers") {
          return <ProvidersPage catalog={aiConfig.catalog} isEnvironmentBacked={aiConfig.isEnvironmentBacked} />;
        }
        if (section === "diagnostics") {
          return <DiagnosticsPage catalog={aiConfig.catalog} isEnvironmentBacked={aiConfig.isEnvironmentBacked} validating={aiConfig.validating} diagnostics={aiConfig.diagnostics} />;
        }
        if (!aiConfig.activeProfile) {
          return <div className="settings-page"><p className="validation is-error">No active profile. Check Diagnostics.</p></div>;
        }
        return (
          <VoiceModelsPage
            catalog={aiConfig.catalog}
            profile={aiConfig.activeProfile}
            document={aiConfig.draft ?? { version: CONFIG_VERSION, activeProfileId: "", providers: {}, profiles: [] }}
            isEnvironmentBacked={aiConfig.isEnvironmentBacked}
            diagnostics={aiConfig.diagnostics}
            validating={aiConfig.validating}
            onChange={(next) => aiConfig.updateActiveProfile(() => next)}
            onMutateDocument={aiConfig.mutateDocument}
            onSave={async () => { const ok = await aiConfig.save(); setToast(ok ? { tone: "success", message: "AI configuration saved." } : { tone: "error", message: "Could not save the AI configuration." }); }}
            onDiscard={aiConfig.discard}
            canSave={aiConfig.canSave}
            saving={aiConfig.saving}
            hasUnsavedChanges={aiConfig.hasUnsavedChanges}
            saveError={aiConfig.saveError}
          />
        );
      }}
    </SettingsDialog>}
  </div>;
}

function fontStack(font: Appearance["font"]) {
  return font === "Newsreader" ? "Newsreader, serif" : font === "Space Grotesk" ? "Space Grotesk, sans-serif" : font === "DM Mono" ? "DM Mono, monospace" : font === "Roboto" ? "Roboto, sans-serif" : font === "Ubuntu" ? "Ubuntu, sans-serif" : "Manrope, sans-serif";
}
