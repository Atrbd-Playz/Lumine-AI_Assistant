import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { DEFAULT_APPEARANCE, NAV_ITEMS, isWorkspaceNav, type NavId } from "./home/constants";
import { AppearanceDialog } from "./home/components/AppearanceDialog";
import { MainSpace } from "./home/components/MainSpace";
import { readStoredPanel, writeStoredPanel, type PanelKind } from "./home/components/ConversationPanel";
import { Sidebar } from "./home/components/Sidebar";
import { WorkspaceView } from "./home/components/WorkspaceView";
import { FocusPage } from "./FocusPage";
import { useNotes } from "./home/notes/useNotes";
import { getReadableForeground, getReadableTextColor, fontStack } from "./home/utils";
import { usePreferences } from "./home/hooks/usePreferences";
import { useDocumentTheme } from "./home/hooks/useDocumentTheme";
import { useNotify } from "../features/toast/useNotify";
import { summarizeToolResult, toolToastText } from "../features/toast/toolToast";
import { useAgentRuntime } from "../lib/agentRuntime";
import { categorizeError } from "../lib/errors";
import { SETTINGS_SECTIONS, type SettingsSection } from "./settings/SettingsNav";
import { useConversation } from "./home/conversation/useConversation";
import { CONVERSATION_STATUS_BY_PRESENCE, PRESENCE_STATE_BY_STATUS } from "./home/presenceState";
import { CommandPalette } from "../components/command/CommandPalette";
import { useCommandHotkeys, describeBinding } from "../components/command/useCommandHotkeys";
import { buildCommands, type Command } from "../components/command/commands";
import { useActivityFeed } from "../features/activity/activityFeed";
import { ActivityTimeline } from "./home/components/ActivityTimeline";
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

/**
 * The path a destination is served at, and the destination a path names.
 *
 * Both live here rather than as literals in `Home.tsx` because there are now
 * three places that have to agree about them: the push on navigation, the
 * `popstate` that follows a Back, and the first render, which reads whatever
 * the address bar already says. The old code pushed `/tools`, `/memory`,
 * `/focus`, `/activity` and `/avatar` but only ever *read* `/avatar`, so
 * reloading any other one landed on Home while the URL claimed otherwise —
 * and no `popstate` listener existed at all, so Back changed the address and
 * left the screen alone.
 *
 * `navFromPath` returns `home` for anything it does not recognise rather than
 * `null`, because every caller needs a page to render and an unknown path is a
 * reason to show Home, not a reason to show nothing.
 */
export function pathForNav(nav: NavId): string {
  return nav === "home" ? "/" : `/${nav}`;
}

export function navFromPath(pathname: string): NavId {
  const segment = pathname.split("/").filter(Boolean)[0] ?? "";
  const match = NAV_ITEMS.find(([id]) => id === segment);
  return match ? match[0] : "home";
}

/**
 * `localStorage` in a `useState` initializer, and nothing above it catching.
 *
 * Storage throws — a full disk, a private window, a disabled profile — and an
 * exception here is the first thing that runs, so one throw is a blank app with
 * no error boundary to render. The project already wraps every other access;
 * this was the one that did not.
 */
function readStoredGlassMode(): boolean {
  try {
    return window.localStorage.getItem("lumine.presentation-mode") === "glass";
  } catch {
    return false;
  }
}

function writeStoredGlassMode(glass: boolean) {
  try {
    window.localStorage.setItem("lumine.presentation-mode", glass ? "glass" : "classic");
  } catch {
    // Losing the preference for this session is the cheap version of this failure.
  }
}

export default function Home() {
  const [nav, setNav] = useState<NavId>(() => navFromPath(window.location.pathname));
  const [glassMode, setGlassMode] = useState(readStoredGlassMode);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("voice");
  const [paletteOpen, setPaletteOpen] = useState(false);
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

  /**
   * The worker's own event stream, kept for the Activity page.
   *
   * `useAgentRuntime` above subscribes for the *status*; this one subscribes for
   * the *records*, and the two cannot be merged because a status is one field and
   * the timeline needs every record. `useActivityFeed` mounts here rather than in
   * the Activity page so navigating to Tools and back does not lose the record of
   * what happened.
   */
  const activity = useActivityFeed();

  /**
   * Which right-hand aside occupies the slot. Held here, not in either panel.
   *
   * Four things open one of them and none of them is a panel: the two switches in
   * the stage's topbar, the command palette's "Show the transcript", and
   * `mod+shift+enter`. It is one value rather than two booleans because there is
   * one slot — the tracks are exclusive — and a nav item that scrolls you to a
   * surface and then needs a second click to reveal it is the dead end the second
   * conversation panel used to be.
   *
   * The stored value is read through the panel's own reader, so the key
   * `localStorage` is written to stays in one file.
   */
  const [panel, setPanel] = useState<PanelKind>(readStoredPanel);
  const [activeNoteId, setActiveNoteId] = useState<string | null>(null);
  const notes = useNotes();

  useEffect(() => {
    writeStoredPanel(panel);
  }, [panel]);

  // Collapsed navigation is a view preference like the theme, so it lives in the
  // shell rather than in the rail: folding the rail and then changing page would
  // otherwise unfold it, which reads as the control not having worked.
  const [navCollapsed, setNavCollapsed] = useState(() => {
    try {
      return window.localStorage.getItem("lumine.nav.collapsed") === "1";
    } catch {
      return false;
    }
  });
  const collapseNav = useCallback((collapsed: boolean) => {
    setNavCollapsed(collapsed);
    try {
      window.localStorage.setItem("lumine.nav.collapsed", collapsed ? "1" : "0");
    } catch {
      // Losing the preference for this session is the cheap version of this failure.
    }
  }, []);

  const handleNavigation = (next: NavId) => {
    const path = pathForNav(next);
    // Re-clicking the row already on screen must not stack a history entry:
    // four dead entries between two clicks is what makes Back look broken, and
    // it did — each one changed the URL and left the page exactly where it was.
    if (window.location.pathname !== path) {
      window.history.pushState({}, "", path);
    }
    setNav(next);
  };

  // Back and Forward change the address bar with nothing listening, so they
  // moved the URL and not the page. This is the other half of the push above.
  useEffect(() => {
    const syncFromHistory = () => setNav(navFromPath(window.location.pathname));
    window.addEventListener("popstate", syncFromHistory);
    return () => window.removeEventListener("popstate", syncFromHistory);
  }, []);

  const sessionState = PRESENCE_STATE_BY_STATUS[session.status];
  const conversationStatus = CONVERSATION_STATUS_BY_PRESENCE[sessionState];

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
    /* The wash is mixed against the *surface*, not against white.
       White is the right base in light mode, where the surface is #fbfaf8 and
       the two are within a point of each other — but in dark mode a
       white-based mix is a pale tint, so the first custom accent a dark-mode
       user picked flipped every `bg-accent/12` chip from a warm shadow to a
       highlight *within the same theme*. Mixing toward the surface keeps the
       wash a wash in both. */
    ...(mode === "light" || appearance.accent !== DEFAULT_APPEARANCE.accent
      ? {
          "--color-accent-soft": `color-mix(in srgb, ${appearance.accent} 18%, ${
            mode === "light" ? "#fbfaf8" : "#211f1d"
          })`,
        }
      : {}),
    "--font-ui": fontStack(appearance.font),
    "--conversation-font": fontStack(appearance.chatFont),
    /* 4.5, not the 3 that `--voice-button-icon` asks for. This pair is behind
       button *labels* — SetupGate's CTA and the voice settings' save — so it
       is text and takes the stricter bar; the call glyph beside it is a
       graphic and deliberately keeps white-on-orange. */
    "--accent-foreground": getReadableTextColor(appearance.accent, 4.5),
    "--voice-button-icon": getReadableTextColor(appearance.accent),
    /* The `default` chat bubble puts its label *on* the accent rather than beside
       it, so the label needs that accent's readable half — and this accent is
       `chatAccent`, a value the appearance picker lets the reader move. The label
       was pinned to `#fffaf5`, which is 4.17:1 on the light default `#c85c31` and
       2.9:1 on a mid-tone choice, with no path by which a bad pick stopped being
       applied. Read back out of `conversationColors` instead of recomputed,
       because that map is already the thing deciding what `--conversation-accent`
       is about to be — deriving it anywhere else is a second opinion about the
       same value. */
    "--conversation-accent-text": getReadableTextColor(
      conversationColors["--conversation-accent"] ?? "#c85c31",
      4.5,
    ),
    "--color-stage-text": getReadableTextColor(
      mode === "light" || appearance.stage !== DEFAULT_APPEARANCE.stage ? appearance.stage : "#11100f",
      // Text, so 4.5 rather than the 3 a glyph is held to.
      4.5,
    ),
  } as CSSProperties;

  // Writing inside the updater is a side effect in a function React may call
  // twice under StrictMode, and a throw there escapes with no boundary above
  // it — so the click that changes the presentation mode could take the app
  // down. The state change is the product; the record of it is best-effort.
  const toggleGlassMode = () =>
    setGlassMode((enabled) => {
      const next = !enabled;
      writeStoredGlassMode(next);
      return next;
    });

  // Portals render outside this subtree, so the palette has to reach <html> too.
  useDocumentTheme(mode, variables);

  // The camera and the screen, released the moment the call ends. Owned here
  // because the capture and the publication have different owners: this hook
  // holds the device, and the voice session holds the track in the room.
  const media = useLocalMedia({ enabled: session.isActive });

  /**
   * Camera and screen failures are toasts now, not inline rows.
   *
   * They used to be a `<p>` between the stage and the call strip, which meant a
   * permission refusal *grew the strip by a line* — the controls the user was
   * about to click moved down as they reached for them, in the one state where
   * they were already having trouble with the platform. A toast says the same
   * thing, is dismissible, and moves nothing.
   *
   * The tone is `warning` rather than `error`: this is not a failure of the app,
   * it is the platform declining a device. Nothing is on fire, and a permission
   * prompt that shouts is one people dismiss.
   *
   * `notify` is held in a ref so the two error strings are the only dependencies.
   * Depending on the notifier would re-fire whenever it re-creates itself, and a
   * toast that reappears on every render is a toast the user learns to ignore.
   */
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  useEffect(() => {
    if (media.cameraError) {
      notifyRef.current({ tone: "warning", title: "Camera unavailable", message: media.cameraError, timeout: 8000 });
    } else if (media.screenError) {
      notifyRef.current({ tone: "warning", title: "Screen share unavailable", message: media.screenError, timeout: 8000 });
    }
  }, [media.cameraError, media.screenError]);

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

  /**
   * Typed text, as a real turn.
   *
   * This is what the composer's `onAddMessage` used to do: append to local state
   * and stop. The user typed a message, it appeared in the transcript, and Lumine
   * was never told. `sendText` publishes it on the room's `lk.chat` topic, which
   * the agent's `AgentSession` consumes as a chat message and answers out loud.
   *
   * Returns a boolean rather than throwing. The caller is a composer's submit
   * handler, and a rejected promise there is an unhandled rejection plus a field
   * that stays full of text the user has no way to tell failed.
   */
  const sendText = useCallback(
    (text: string): boolean => {
      if (!session.canSendText) {
        notify({
          tone: "warning",
          title: "No call to send to",
          message: "Start a call first — Lumine answers text over a live room, not on her own.",
          timeout: 5000,
        });
        return false;
      }
      // The user's own line is appended here rather than arriving by transcription.
      // The agent does not republish a participant's speech, so a `role: "user"`
      // row never arrives on its own — and a composer that echoed nothing would
      // look like it had dropped the message.
      conversation.addMessage({ role: "user", content: text, timestamp: new Date(), type: "text", status: "complete" });
      void session.sendText(text).then((delivered) => {
        if (delivered) return;
        notify({
          tone: "error",
          title: "Message not sent",
          message: "The room refused the text. The call may have just ended.",
          timeout: 6000,
        });
      });
      return true;
    },
    [conversation, notify, session],
  );

  /**
   * The command registry.
   *
   * Rebuilt on every render, and deliberately not memoised: each entry closes over
   * current state (`muted`, `canSendText`, `isPublished`), so a memo would hold a
   * command that acts on a call which no longer exists. The palette memoises the
   * *filtered* result against this array's identity, and it is the ranking that
   * costs — an array of ~18 plain objects per render is not a thing to optimise.
   */
  const commands = buildCommands([
    {
      id: "palette.open",
      label: "Search all commands",
      hint: "Everything the app can do, in one list",
      group: "App",
      icon: "spark",
      /**
       * Four chords, and only one of them is advertised.
       *
       * - `mod+k` is the one the topbar button shows: Ctrl+K off Apple, ⌘K on it.
       * - `ctrl+k` is the *literal* Control chord on macOS, where `mod+k` is
       *   something else. Both are needed for the chord to be right on both
       *   platforms; see `eventBinding`.
       * - `meta+k` is Win+K, which is what a Windows user reaches for first
       *   because that is what every Windows app with a command bar uses. The OS
       *   reserves Win+K and normally swallows it before any window sees the
       *   keystroke, so this usually does nothing — but when the shell *does*
       *   pass it through, the app answers instead of ignoring it. Binding a
       *   chord the OS may take costs one line and is the difference between
       *   "sometimes works" and "never works".
       * - `f1` is the fallback with no OS opinion at all. Not advertised, because
       *   a second shortcut on the same row makes a reader pick between them
       *   rather than remember one.
       */
      keys: ["mod+k", "ctrl+k", "meta+k", "f1"],
      run: () => setPaletteOpen((open) => !open),
    },
    {
      id: "call.toggle",
      label: session.isActive ? "End the call" : "Call Lumine",
      hint: session.isActive ? "Hang up" : "Open a room and say hello",
      group: "Call",
      icon: session.isActive ? "phone-down" : "phone",
      keys: ["mod+shift+c"],
      available: !session.isActive ? Boolean(voiceBlockedReason) === false : true,
      unavailableReason: voiceBlockedReason ?? undefined,
      run: () => { if (session.isActive) void session.disconnect(); else { conversation.beginSession(); void session.connect(); } },
    },
    {
      id: "call.mute",
      label: session.muted ? "Unmute the microphone" : "Mute the microphone",
      group: "Call",
      icon: session.muted ? "mic-off" : "mic",
      keys: ["mod+shift+m"],
      available: session.isActive,
      unavailableReason: "There is no call to mute.",
      run: () => { void session.toggleMute(); },
    },
    {
      id: "media.camera",
      label: media.active?.source === "camera" ? "Turn the camera off" : "Turn the camera on",
      group: "Call",
      icon: "video",
      available: session.isActive,
      unavailableReason: "There is no call to put a camera into.",
      run: () => { void toggleSource("camera"); },
    },
    {
      id: "media.screen",
      label: media.active?.source === "screen" ? "Stop sharing the screen" : "Share the screen",
      group: "Call",
      icon: "monitor",
      available: session.isActive && (canReceiveVideo || media.active?.source === "screen"),
      unavailableReason: canReceiveVideo ? "There is no call to share into." : "The selected model cannot read frames.",
      run: () => { void toggleSource("screen"); },
    },
    {
      id: "nav.home",
      label: "Go to the command space",
      group: "Navigate",
      icon: "home",
      keys: ["mod+1"],
      run: () => handleNavigation("home"),
    },
    {
      id: "nav.tools",
      label: "Go to tools",
      hint: "What Lumine can reach on her own",
      group: "Navigate",
      icon: "tools",
      keys: ["mod+2"],
      run: () => handleNavigation("tools"),
    },
    {
      id: "nav.memory",
      label: "Go to memory",
      group: "Navigate",
      icon: "memory",
      keys: ["mod+3"],
      run: () => handleNavigation("memory"),
    },
    {
      id: "nav.activity",
      label: "Go to activity",
      hint: "What the worker reported, as it reported it",
      group: "Navigate",
      icon: "activity",
      keys: ["mod+4"],
      run: () => handleNavigation("activity"),
    },
    {
      id: "nav.avatar",
      label: "Go to the Avatar Lab",
      hint: "Tune the expressions, gestures and montage",
      group: "Navigate",
      icon: "avatar",
      keys: ["mod+5"],
      run: () => handleNavigation("avatar"),
    },
    {
      id: "nav.focus",
      label: "Go to Focus",
      hint: "The timer and the notes",
      group: "Navigate",
      icon: "timer",
      keys: ["mod+6"],
      run: () => handleNavigation("focus"),
    },
    {
      id: "appearance.toggle",
      label: mode === "light" ? "Switch to the dark theme" : "Switch to the light theme",
      group: "Appearance",
      icon: "presentation",
      keys: ["mod+shift+l"],
      run: () => setMode(mode === "light" ? "dark" : "light"),
    },
    {
      id: "appearance.avatarColour",
      label: appearance.showAvatarColor ? "Hide the avatar's colour" : "Show the avatar's colour",
      hint: "The avatar is drawn from your palette either way; this controls how much of it shows.",
      group: "Appearance",
      icon: "spark",
      run: () => setAppearance({ ...appearance, showAvatarColor: !appearance.showAvatarColor }),
    },
    {
      id: "appearance.gaze",
      label: cursorGaze ? "Stop following the cursor" : "Let her follow the cursor",
      group: "Appearance",
      icon: "avatar",
      run: () => setCursorGaze(!cursorGaze),
    },
    {
      id: "dock.toggle",
      label: panel === "conversation" ? "Hide the transcript" : "Show the transcript",
      hint: "The aside holds the conversation and the composer",
      group: "Session",
      icon: "chat",
      keys: ["mod+shift+enter"],
      run: () => {
        // From another page this navigates rather than toggles: "show the
        // transcript" from the Tools page has to get there first, and arriving
        // somewhere else with a toggle pressed is how a shortcut ends up
        // appearing to do nothing.
        if (nav !== "home") {
          handleNavigation("home");
          setPanel("conversation");
          return;
        }
        setPanel(panel === "conversation" ? null : "conversation");
      },
    },
    {
      id: "notes.toggle",
      label: panel === "notes" ? "Hide the notes" : "Show the notes",
      group: "Session",
      icon: "note",
      run: () => {
        if (nav !== "home") {
          handleNavigation("home");
          setPanel("notes");
          return;
        }
        setPanel(panel === "notes" ? null : "notes");
      },
    },
    {
      id: "session.clear",
      label: "Clear this conversation",
      hint: "Removes the transcript. It does not end the call.",
      group: "Session",
      icon: "trash",
      run: () => {
        conversation.clearMessages();
        notify({ tone: "success", message: "Conversation cleared." });
      },
    },
    {
      id: "settings.open",
      label: "Open settings",
      group: "App",
      icon: "settings",
      keys: ["mod+,"],
      run: () => { setSettingsSection("voice"); setSettingsOpen(true); },
    },
    {
      id: "settings.diagnostics",
      label: "Open diagnostics",
      hint: "Keys, models, and whether the worker is registered",
      group: "App",
      icon: "info",
      run: () => { setSettingsSection("diagnostics"); setSettingsOpen(true); },
    },
  ]);

  /**
   * One answer for "that command cannot run", whatever asked.
   *
   * The chord path already had this and the palette's Enter did not, so the
   * same unavailable command toasted over a keystroke and did nothing over a
   * click — two routes to one command giving two different replies. Both reach
   * here now, and the reason shown is the command's own.
   */
  const reportBlocked = (command: Command) => {
    notify({ tone: "warning", title: command.label, message: command.unavailableReason ?? "Not available right now.", timeout: 4000 });
  };

  /**
   * The command list, wired to the keyboard.
   *
   * The palette's own toggle is the `mod+k` entry, so there is one declaration of
   * that chord rather than a hotkey here and a command there — two of which would
   * disagree the first time somebody changed the binding.
   */
  useCommandHotkeys({
    commands,
    // Off while the settings overlay is open. It has its own focus trap and its
    // own key handling, and a global `mod+1` firing underneath it would navigate
    // the page out from under a dialog.
    enabled: !settingsOpen,
    onBlocked: reportBlocked,
  });

  /**
   * Which page the rail's `nav` actually means.
   *
   * This was a ternary chain ending in `<WorkspaceView kind={nav as "tools" | "memory"} />`,
   * and the cast was the defect: `nav` is a string, so `"conversation"` and
   * `"settings"` both satisfied the type and reached `CONTENT[kind]` as
   * `undefined`. A crash, not a wrong page — which is the lucky version of that
   * bug. `isWorkspaceNav` narrows instead of asserting, and a destination with no
   * page behind it now falls through to the stage rather than to whatever the
   * last branch happened to be.
   */
  const page = isWorkspaceNav(nav)
    ? nav
    : nav === "avatar"
      ? "avatar"
      : nav === "activity"
        ? "activity"
        : nav === "focus"
          ? "focus"
          : "home";

  return <div className={`lumine-app theme-${mode} route-${nav} ${glassMode ? "visual-glass" : "visual-classic"}${navCollapsed ? " is-nav-collapsed" : ""}`} style={variables}>
    <Sidebar
      active={nav}
      onChange={handleNavigation}
      onSettings={() => setSettingsOpen(true)}
      collapsed={navCollapsed}
      onCollapsedChange={collapseNav}
    />
    {page === "home" ? <MainSpace
      state={sessionState}
      cursorGaze={cursorGaze}
      showAvatarColor={appearance.showAvatarColor}
      sessionStatus={session.status}
      // A new call no longer erases the last one.
      //
      // It used to: `onConnect` called `clearMessages()` before joining, so every
      // call to Lumine began by destroying the record of the previous call. On a
      // voice app that is the whole history, and it is the one thing a person
      // cannot reconstruct afterwards. Sessions are separated by a divider now
      // instead — see `beginSession` in the voice layer — so the transcript reads
      // as a day rather than as a buffer.
      onConnect={() => { conversation.beginSession(); void session.connect(); }}
      onDisconnect={() => { void session.disconnect(); }}
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
      conversationItems={conversation.items}
      conversationStatus={conversationStatus}
      bubbleVariant={appearance.chatBubbleVariant}
      canSendText={session.canSendText}
      sendTextHint="Start a call to send Lumine a message."
      onSendText={sendText}
      onClearConversation={conversation.clearMessages}
      onResetConversation={conversation.resetMessages}
      glassMode={glassMode}
      onGlassModeToggle={toggleGlassMode}
      onOpenPalette={() => setPaletteOpen(true)}
      paletteHint={describeBinding("mod+k")}
      subscribeEnergy={session.subscribeAudioLevel}
      panel={panel}
      onPanelChange={setPanel}
      notes={notes}
      activeNoteId={activeNoteId}
      onSelectNote={setActiveNoteId}
      onCreateNote={() => setActiveNoteId(notes.create())}
      onEditNote={notes.save}
      onDeleteNote={(id) => {
        notes.remove(id);
        setActiveNoteId((current) => (current === id ? null : current));
      }}
      onFocusPage={() => handleNavigation("focus")}
    /> : page === "focus" ? <FocusPage notes={notes} /> : page === "avatar" ? <AvatarLabPage /> : page === "activity" ? <ActivityTimeline entries={activity.entries} onClear={activity.clear} expanded /> : <WorkspaceView kind={page} onSettings={() => { setSettingsSection("voice"); setSettingsOpen(true); }} />}

    <CommandPalette
      open={paletteOpen}
      onOpenChange={setPaletteOpen}
      commands={commands}
      onBlocked={reportBlocked}
    />

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
                <div className="settings-page-head-text min-w-0">
                  <p className="eyebrow">{definition?.group ?? "AI"}</p>
                  <h1>{definition?.label ?? "Settings"}</h1>
                </div>
              </header>
              <div className="settings-empty">
                <p className="validation is-error">{aiConfig.error ?? "The provider catalog is unavailable."}</p>
                <p className="field-hint text-faint text-[11.5px] leading-[1.5]">
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
        return (
          <ModelsPage
            {...shared}
            tab={route.tab ?? "llm"}
            // Voices the user kept live in the same document as the profile
            // rather than in local storage, because they belong to the settings —
            // clearing site data should not empty a list of voices somebody
            // chose, and the worker reads this file already.
            savedVoices={aiConfig.draft?.voices ?? []}
            onSavedVoicesChange={(voices) => aiConfig.mutateDocument((doc) => ({ ...doc, voices }))}
          />
        );
      }}
    </SettingsDialog>}
  </div>;
}

