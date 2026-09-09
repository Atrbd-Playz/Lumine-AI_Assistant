import { useState, type CSSProperties } from "react";
import "./index.css";
import { DEFAULT_APPEARANCE } from "./home/constants";
import { AppearanceDialog } from "./home/components/AppearanceDialog";
import Context from "./Context";
import { MainSpace } from "./home/components/MainSpace";
import { Sidebar } from "./home/components/Sidebar";
import type { LumineState } from "./home/types";
import { getReadableForeground, getReadableTextColor } from "./home/utils";
import { usePreferences } from "./home/hooks/usePreferences";
import { useConversation } from "./home/conversation/useConversation";
import { ConversationPanel } from "./home/components/ConversationPanel";
import type { Appearance } from "./home/types";

/** Page-level state owner. Child components receive data and callbacks only. */
export default function Home() {
  const [nav, setNav] = useState("home");
  const [state, setState] = useState<LumineState>("idle");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [conversationOpen, setConversationOpen] = useState(false);
  const conversation = useConversation();
  const { mode, setMode, cursorGaze, setCursorGaze, appearance, setAppearance, resetAppearance, presets, savePreset, importPresets, deletePreset } = usePreferences();

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

  return <div className={`lumine-app theme-${mode} route-${nav} ${conversationOpen && nav === "home" ? "conversation-open" : ""}`} style={variables}>
    <Sidebar active={nav} onChange={setNav} onSettings={() => setSettingsOpen(true)} />
    <MainSpace state={state} setState={setState} cursorGaze={cursorGaze} showAvatarColor={appearance.showAvatarColor} conversationOpen={conversationOpen} onConversationToggle={() => setConversationOpen((open) => !open)} />
    {conversationOpen && nav === "home" && <ConversationPanel messages={conversation.messages} agentStatus={conversation.agentStatus} bubbleVariant={appearance.chatBubbleVariant} onClose={() => setConversationOpen(false)} onClear={conversation.clearMessages} onReset={conversation.resetMessages} onAddMessage={conversation.addMessage} />}
    {nav !== "home" && <Context state={state} onSettings={() => setSettingsOpen(true)} />}
    {settingsOpen && <AppearanceDialog mode={mode} setMode={setMode} cursorGaze={cursorGaze} setCursorGaze={setCursorGaze} appearance={appearance} setAppearance={setAppearance} presets={presets} savePreset={savePreset} importPresets={importPresets} deletePreset={deletePreset} onResetPalette={resetAppearance} onReset={() => { setMode("dark"); setCursorGaze(true); resetAppearance(); }} onClose={() => setSettingsOpen(false)} />}
  </div>;
}

function fontStack(font: Appearance["font"]) {
  return font === "Newsreader" ? "Newsreader, serif" : font === "Space Grotesk" ? "Space Grotesk, sans-serif" : font === "DM Mono" ? "DM Mono, monospace" : font === "Roboto" ? "Roboto, sans-serif" : font === "Ubuntu" ? "Ubuntu, sans-serif" : "Manrope, sans-serif";
}
