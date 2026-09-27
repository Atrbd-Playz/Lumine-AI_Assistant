import { useRef, useState, type CSSProperties } from "react";
import "./index.css";
import { DEFAULT_APPEARANCE } from "./home/constants";
import { AppearanceDialog } from "./home/components/AppearanceDialog";
import { MainSpace } from "./home/components/MainSpace";
import { Sidebar } from "./home/components/Sidebar";
import { WorkspaceView } from "./home/components/WorkspaceView";
import type { LumineState } from "./home/types";
import { getReadableForeground, getReadableTextColor } from "./home/utils";
import { usePreferences } from "./home/hooks/usePreferences";
import { useDocumentTheme } from "./home/hooks/useDocumentTheme";
import { useNotify } from "../features/toast/useNotify";
import { SETTINGS_SECTIONS } from "./settings/SettingsNav";
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
  const notify = useNotify();
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
    // Status only, and only a toast for the fact of running. The transcript
    // accumulates; a payload shown there would accumulate too, and a retrieved
    // page sitting inline in a conversation reads as though the model had been
    // handed it by the user -- which is the shape a prompt injection wants.
    conversation.upsertToolEvent(event);
    if (event.status === "started") return;
    const timing = event.durationMs ? ` · ${event.durationMs}ms` : "";
    notify({
      tone: event.status === "completed" ? "success" : "error",
      message: `${event.name}${event.status === "completed" ? " done" : " failed"}${timing}`,
    });
  };

  /**
   * A tool's actual output, as a toast.
   *
   * The one surface where showing the payload is free: transient, asked for, and
   * gone before it can accumulate. Capped again here so a long payload cannot
   * turn into a wall of text that covers the UI.
   */
  const handleToolResult = (result: { name: string; status: string; payload: string; durationMs?: number }) => {
    const detail = result.payload.length > 320 ? `${result.payload.slice(0, 320)}…` : result.payload;
    notify({
      tone: result.status === "completed" ? "info" : "error",
      title: result.name,
      message: detail,
      timeout: 6000,
    });
  };

  /**
   * A limit or failure from the agent's own runtime.
   *
   * Titled and toned differently on purpose. A rate limit is a wait, and a failed
   * turn is a problem; showing both as a red error would train the user to ignore
   * the one that matters. The message is Lumine's own words, already written for
   * a person -- the provider's raw error stays in the log.
   *
   * Deduplicated by kind, because a circuit that stays open would otherwise emit
   * one identical toast per failed turn.
   */
  const noticeRef = useRef<{ kind: string; at: number } | null>(null);
  const handleNotice = (notice: { kind: string; message: string; retryAfter?: number }) => {
    const now = Date.now();
    const previous = noticeRef.current;
    if (previous && previous.kind === notice.kind && now - previous.at < 15_000) return;
    noticeRef.current = { kind: notice.kind, at: now };
    if (notice.kind === "limit") {
      const wait = notice.retryAfter ? ` Try again in ${notice.retryAfter}s.` : " Try again shortly.";
      notify({ tone: "warning", title: "Request limit reached", message: `${notice.message}${wait}`, timeout: 8000 });
      return;
    }
    notify({ tone: "error", title: "Something went wrong", message: notice.message, timeout: 6000 });
  };

  const session = useLumineVoice({ onMessage: conversation.addMessage, onUpdateMessage: conversation.updateMessage, onToolEvent: handleToolEvent, onToolResult: handleToolResult, onNotice: handleNotice, interruptionMode, onError: (message) => notify({ tone: "error", message }) });

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

  // Portals render outside this subtree, so the palette has to reach <html> too.
  useDocumentTheme(mode, variables);

  return <div className={`lumine-app theme-${mode} route-${nav} ${glassMode ? "visual-glass" : "visual-classic"} ${conversationOpen && nav === "home" ? "conversation-open" : ""}`} style={variables}>
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
          // One fallback covers every AI section, so the title is read from the
          // section rather than hardcoded — otherwise Providers and Diagnostics
          // both announce themselves as "Voice & Models".
          const definition = SETTINGS_SECTIONS.find((entry) => entry.id === section);
          return (
            <div className="settings-page">
              <header className="settings-page-head">
                <div className="settings-page-head-text">
                  <p className="eyebrow">{definition?.group ?? "AI"}</p>
                  <h1>{definition?.label ?? "Settings"}</h1>
                </div>
              </header>
              <div className="settings-empty">
                <p className="validation is-error">{aiConfig.error ?? "The provider catalog is unavailable."}</p>
                <p className="field-hint">
                  Lumine needs the desktop app to read its configuration. Start it with{" "}
                  <code>npm run tauri dev</code>, then reopen this screen.
                </p>
              </div>
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
            onSave={async () => { const ok = await aiConfig.save(); notify(ok ? { tone: "success", message: "AI configuration saved." } : { tone: "error", message: "Could not save the AI configuration." }); }}
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
