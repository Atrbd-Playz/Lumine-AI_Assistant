import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { DEFAULT_APPEARANCE } from "./home/constants";
import { AppearanceDialog } from "./home/components/AppearanceDialog";
import { MainSpace } from "./home/components/MainSpace";
import { Sidebar } from "./home/components/Sidebar";
import { WorkspaceView } from "./home/components/WorkspaceView";
import type { LumineState } from "./home/types";
import { getReadableForeground, getReadableTextColor, fontStack } from "./home/utils";
import { usePreferences } from "./home/hooks/usePreferences";
import { useDocumentTheme } from "./home/hooks/useDocumentTheme";
import { useNotify } from "../features/toast/useNotify";
import { summarizeToolResult, toolToastText } from "../features/toast/toolToast";
import { useAgentRuntime } from "../lib/agentRuntime";
import { categorizeError } from "../lib/errors";
import { SETTINGS_SECTIONS, type SettingsSection } from "./settings/SettingsNav";
import { useConversation } from "./home/conversation/useConversation";
import { ConversationPanel } from "./home/components/ConversationPanel";
import { useLumineVoice } from "../features/voice/useLumineVoice";
import { useLocalMedia } from "../features/voice/useLocalMedia";
import { DEFAULT_INTERRUPTION_MODE } from "../features/voice/interruption";
import { SettingsDialog } from "./settings/SettingsDialog";
import { ProvidersPage } from "./settings/ai/ProvidersPage";
import { VoicePage } from "./settings/ai/VoicePage";
import { ModelsPage } from "./settings/ai/ModelsPage";
import { DiagnosticsPage } from "./settings/system/DiagnosticsPage";
import { AboutPage } from "./settings/system/AboutPage";
import { CONFIG_VERSION } from "../features/settings/aiConfigTypes";
import { useAiConfig } from "../features/settings/useAiConfig";
import { useSetupStatus } from "../features/settings/useSetupStatus";
import { SetupGate } from "./onboarding/SetupGate";
import AvatarLabPage from "./AvatarLabPage";

export default function Home() {
  const [nav, setNav] = useState(() => window.location.pathname === "/avatar" ? "avatar" : "home");
  const [glassMode, setGlassMode] = useState(() => localStorage.getItem("lumine.presentation-mode") === "glass");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("voice");
  const [conversationOpen, setConversationOpen] = useState(false);
  const notify = useNotify();
  const conversation = useConversation();
  const { mode, setMode, cursorGaze, setCursorGaze, appearance, setAppearance, resetAppearance, resetAllAppearance, presets, savePreset, importPresets, deletePreset } = usePreferences();
  const aiConfig = useAiConfig();
  // Read once here and refreshed by the gate, so a key pasted in the wizard is
  // reflected in the voice button without a second round-trip on every render.
  const setup = useSetupStatus();
  const voiceBlockedReason = setup.status && !setup.status.ready
    ? setup.status.blocking.length > 0
      ? "Lumine cannot start until the configuration is fixed. Open Voice & Models."
      : "Lumine cannot start until its keys are set. Add them in the setup panel."
    : null;
  // The active voice profile is authoritative once one is saved; until then the
  // environment default is what the worker will use, so the UI mirrors that.
  const interruptionMode =
    aiConfig.activeProfile?.kind === "realtime"
      ? (aiConfig.activeProfile.realtime?.turnHandling?.interruptionMode ?? DEFAULT_INTERRUPTION_MODE)
      : (aiConfig.activeProfile?.pipeline?.turnHandling?.interruptionMode ?? DEFAULT_INTERRUPTION_MODE);
  /**
   * A tool call in the transcript. No toast.
   *
   * The transcript accumulates and the notification does not, so a call's
   * lifecycle belongs in one of them and not both. This used to raise a
   * "get_weather done" toast here as well, which is why every tool produced two
   * stacked notifications for a single action.
   *
   * The payload is deliberately not rendered here. A retrieved page sitting inline
   * in a conversation reads as though the model had been handed it by the user --
   * which is the shape a prompt injection wants. It goes to a toast instead.
   */
  const handleToolEvent = (event: Parameters<typeof conversation.upsertToolEvent>[0]) => {
    conversation.upsertToolEvent(event);
  };

  /**
   * A finished tool call, as exactly one toast.
   *
   * The single notification for a tool call: what it was, one line of what came
   * back, and how long it took. Short on purpose. A notification that has to be
   * read before it can be dismissed is a document, and a tool call is neither
   * rare enough to justify one nor important enough to want a paragraph.
   */
  const handleToolResult = (result: { name: string; status: string; payload: string; durationMs?: number }) => {
    const line = summarizeToolResult(result);
    const text = toolToastText(line, result.durationMs);
    // A failure is worth interrupting for; a routine lookup is not. A success
    // with nothing to say is dropped entirely rather than shown as a bare "Done",
    // which is a notification carrying no information.
    if (line.tone === "success" && text === null) return;
    notify({
      tone: line.tone,
      title: line.label,
      ...(text ? { message: text } : { message: "No detail returned." }),
      timeout: line.tone === "error" ? 8000 : 4000,
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

  /**
   * A failure, as a cause and a next step rather than a provider string.
   *
   * Every failure in the app -- a rejected key, a dropped room, a dead worker --
   * arrives here as text, and text is the one thing a person cannot act on. A
   * bare `401 Unauthorized` tells them nothing they did not already suspect, and
   * the most common cause by a wide margin is a key that was never right.
   * `categorizeError` turns the string into a title that names the problem and
   * one sentence that says what to do about it.
   *
   * A rate limit is toned as a warning rather than an error, for the same reason
   * the agent's own notices are: showing both red trains people to ignore the
   * one that actually needs them.
   */
  const reportFailure = useCallback((raw: string, title?: string) => {
    const { category, title: heading, message, action } = categorizeError(raw);
    notify({
      tone: category === "rate_limit" ? "warning" : "error",
      title: title ?? heading,
      message: action ? `${message} ${action}` : message,
      timeout: 10_000,
    });
  }, [notify]);

  const session = useLumineVoice({ onMessage: conversation.addMessage, onUpdateMessage: conversation.updateMessage, onToolEvent: handleToolEvent, onToolResult: handleToolResult, onNotice: handleNotice, interruptionMode, onError: (message) => reportFailure(message) });

  /**
   * The voice worker, followed rather than asked.
   *
   * A worker that dies between two status reads used to be invisible, because
   * the only thing watching it was a poll. It is the difference between "Lumine
   * did not answer" and "Lumine's process exited", and the second one is
   * something that can be acted on.
   */
  useAgentRuntime({
    onError: (status) => {
      if (status.error) reportFailure(status.error, "The voice worker reported a problem");
    },
  });

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

  // The camera and the screen, released the moment the call ends. Owned here
  // because the capture and the publication have different owners: this hook
  // holds the device, and the voice session holds the track in the room.
  const media = useLocalMedia({ enabled: session.isActive });
  // Whether the selected model could consume frames, read from the same
  // `inputModalities` the Diagnostics matrix draws.
  const canReceiveVideo = useMemo(() => {
    const profile = aiConfig.activeProfile;
    if (!profile || !aiConfig.catalog) return false;
    const stage = profile.kind === "realtime" ? profile.realtime : profile.pipeline?.llm;
    if (!stage?.provider || !stage.model) return false;
    const model = aiConfig.catalog.providers
      .find((provider) => provider.id === stage.provider)
      ?.models.find((entry) => entry.id === stage.model);
    return Boolean(model?.inputModalities.includes("image"));
  }, [aiConfig.activeProfile, aiConfig.catalog]);

  // Capture is local; publication is what Lumine sees. The model is the gate,
  // because LiveKit silently discards frames from a model that cannot read them
  // -- no error, and a recording indicator that is lit for nothing.
  const [isPublished, setIsPublished] = useState(false);
  const publishedRef = useRef(false);
  const { publishVideo, unpublishVideo } = session;

  const dropPublished = useCallback(() => {
    if (!publishedRef.current) return;
    publishedRef.current = false;
    setIsPublished(false);
    void unpublishVideo();
  }, [unpublishVideo]);

  /**
   * Unpublish whenever anything the publication depended on stops being true.
   *
   * Three ways that happens, and none of them involve the user pressing the
   * control -- which is the whole reason this is an effect rather than a branch
   * inside the toggle:
   *
   * * the model changes to one that declares no image input;
   * * the call ends, so there is no session left to publish into;
   * * the capture ends on its own, because the user pressed the browser's own
   *   "Stop sharing" bar or revoked the device from the system tray. The
   *   preview card simply disappears in that case, so without this the track
   *   stays in the room and `isPublished` keeps claiming a share that ended.
   *
   * A track left in the room after the thing that could read it is gone is
   * exactly the silent failure this feature is built to avoid.
   */
  useEffect(() => {
    if (canReceiveVideo && session.isActive && media.active) return;
    dropPublished();
  }, [canReceiveVideo, session.isActive, media.active, dropPublished]);

  const toggleSource = useCallback(
    async (source: "camera" | "screen") => {
      const result = source === "camera" ? await media.toggleCamera() : await media.toggleScreen();
      if (result.kind === "failed") {
        // The capture never started, so whatever was already live is still live.
        // Unpublishing here would stop a camera the user never asked to stop --
        // the error field is what reports this, and it is already set.
        return;
      }
      if (result.kind === "stopped") {
        dropPublished();
        return;
      }
      if (!canReceiveVideo) {
        // Captured, previewed, and deliberately not published: the model cannot
        // read frames and would discard them without a word. The self-view still
        // shows the user their own picture, and its badge says so.
        return;
      }
      const published = await publishVideo(result.media.source, result.media.stream);
      publishedRef.current = published;
      setIsPublished(published);
    },
    [media, canReceiveVideo, publishVideo, dropPublished],
  );

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
      media={media}
      isVideoPublished={isPublished}
      onMediaToggle={(source) => { void toggleSource(source); }}
      startedAt={session.startedAt}
      emotion={session.emotion}
      showEmotionDebug={import.meta.env.VITE_LUMINE_DEBUG_EMOTION === "true"}
      gate={setup.status && !setup.status.ready ? <SetupGate setup={setup} onOpenProviders={() => { setSettingsSection("providers"); setSettingsOpen(true); }} onOpenDiagnostics={() => { setSettingsSection("diagnostics"); setSettingsOpen(true); }} /> : undefined}
      voiceDisabledReason={voiceBlockedReason ?? undefined}
      canReceiveVideo={canReceiveVideo}
    /> : nav === "avatar" ? <AvatarLabPage /> : <WorkspaceView kind={nav as "tools" | "memory" | "activity"} onSettings={() => { setSettingsSection("voice"); setSettingsOpen(true); }} />}
    {conversationOpen && nav === "home" && <ConversationPanel messages={conversation.messages} items={conversation.items} agentStatus={conversationStatus} bubbleVariant={appearance.chatBubbleVariant} onClose={() => setConversationOpen(false)} onClear={conversation.clearMessages} onReset={conversation.resetMessages} onAddMessage={conversation.addMessage} />}
    {settingsOpen && <SettingsDialog initialSection={settingsSection} onClose={() => setSettingsOpen(false)}>
      {(route) => {
        const section = route.section;
        if (section === "appearance") {
          return <AppearanceDialog mode={mode} setMode={setMode} cursorGaze={cursorGaze} setCursorGaze={setCursorGaze} appearance={appearance} setAppearance={setAppearance} presets={presets} savePreset={savePreset} importPresets={importPresets} deletePreset={deletePreset} onResetPalette={resetAppearance} onReset={resetAllAppearance} onClose={() => setSettingsOpen(false)} />;
        }
        if (aiConfig.state === "loading") {
          return <div className="settings-page"><p className="validation is-pending">Loading the AI configuration…</p></div>;
        }
        if (aiConfig.state === "error" || !aiConfig.catalog) {
          // One fallback covers every AI section, so the title is read from the
          // section rather than hardcoded — otherwise Providers and Diagnostics
          // both announce themselves as "Models".
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
        // Ahead of the catalog loading gate on purpose: who made the app and what
        // it is does not depend on the provider catalog, and an About screen that
        // says "the catalog is unavailable" would be a strange first impression.
        if (section === "about") {
          return <AboutPage />;
        }
        if (!aiConfig.activeProfile) {
          return <div className="settings-page"><p className="validation is-error">No active profile. Check Diagnostics.</p></div>;
        }
        // Voice and Models edit the same document, so they share the save state.
        // Destructured into one object because passing seven identical props to
        // two pages is how one of them ends up with a stale Save.
        const shared = {
          catalog: aiConfig.catalog,
          profile: aiConfig.activeProfile,
          isEnvironmentBacked: aiConfig.isEnvironmentBacked,
          onChange: (next: typeof aiConfig.activeProfile) => aiConfig.updateActiveProfile(() => next),
          onSave: async () => { const ok = await aiConfig.save(); notify(ok ? { tone: "success", message: "AI configuration saved." } : { tone: "error", message: "Could not save the AI configuration." }); },
          onDiscard: aiConfig.discard,
          canSave: aiConfig.canSave,
          saving: aiConfig.saving,
          hasUnsavedChanges: aiConfig.hasUnsavedChanges,
          saveError: aiConfig.saveError,
        };
        if (section === "voice") {
          return (
            <VoicePage
              {...shared}
              document={aiConfig.draft ?? { version: CONFIG_VERSION, activeProfileId: "", providers: {}, profiles: [] }}
              onMutateDocument={aiConfig.mutateDocument}
            />
          );
        }
        return <ModelsPage {...shared} tab={route.tab ?? "llm"} />;
      }}
    </SettingsDialog>}
  </div>;
}

