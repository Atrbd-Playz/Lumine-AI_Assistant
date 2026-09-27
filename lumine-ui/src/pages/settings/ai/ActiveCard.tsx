import { findProvider, type ProviderCatalog, type VoiceProfile } from "../../../features/settings/aiConfigTypes";
import { Icon } from "../../home/components/Icon";

/**
 * What the active profile actually resolves to, drawn as a flow.
 *
 * This is the only place the whole stack is visible at once. The stages screen
 * splits the stack across four tabs precisely so each one can be read on its own,
 * which means the question "so what is she running right now?" no longer has an
 * answer on any one of them. This is the answer, and it is on the Voice screen
 * because a person asks it about the voice.
 *
 * ## The badge tells the truth about the source
 *
 * Once the draft differs from what is saved, the source badge has to change. A
 * profile being edited is no longer "from agent/.env" no matter where its
 * starting values came from, and a badge that claims otherwise is worse than no
 * badge at all.
 */
function stageLabel(catalog: ProviderCatalog, ref: { provider?: string; model?: string } | undefined): string {
  if (!ref?.provider || !ref.model) return "Not set";
  const provider = findProvider(catalog, ref.provider);
  const model = provider?.models.find((entry) => entry.id === ref.model);
  if (!provider || !model) return ref.provider;
  return `${provider.label} · ${model.label}`;
}

function interruptionLabel(profile: VoiceProfile): string {
  const mode =
    profile.kind === "realtime"
      ? profile.realtime?.turnHandling?.interruptionMode
      : profile.pipeline?.turnHandling?.interruptionMode;
  return mode === "finish_response" ? "finishes the reply" : "allows interruption";
}

export type ActiveCardProps = {
  catalog: ProviderCatalog;
  profile: VoiceProfile;
  isEnvironmentBacked: boolean;
  hasUnsavedChanges: boolean;
};

export function ActiveCard({ catalog, profile, isEnvironmentBacked, hasUnsavedChanges }: ActiveCardProps) {
  const badge = hasUnsavedChanges ? "Unsaved edits" : isEnvironmentBacked ? "From agent/.env" : "Saved";

  const header = (
    <header>
      <div>
        <p className="eyebrow">Active configuration</p>
        <h3>{profile.name}</h3>
      </div>
      <span className="active-card-badge">{badge}</span>
    </header>
  );

  if (profile.kind === "realtime" && profile.realtime) {
    const realtime = profile.realtime;
    const output = realtime.output;
    const customTts = output.mode === "custom_tts";
    const ttsRef = customTts && "tts" in output ? output.tts : undefined;
    return (
      <article className="active-card">
        {header}
        <div className="active-flow">
          <div className="active-node">
            <Icon name="mic" size={16} />
            <span className="active-node-label">Realtime</span>
            <span className="active-node-value">{stageLabel(catalog, realtime)}</span>
          </div>
          <span className="active-arrow">→</span>
          <div className="active-node">
            <Icon name="waveform" size={16} />
            <span className="active-node-label">{customTts ? "Custom TTS" : "Model voice"}</span>
            <span className="active-node-value">
              {customTts ? stageLabel(catalog, ttsRef) : (realtime.voice ?? "Model default")}
            </span>
          </div>
        </div>
        <ul className="active-facts">
          <li>
            <Icon name="check" size={13} /> Audio input handled by the realtime model
          </li>
          <li>
            <Icon name="check" size={13} /> Interruption: {interruptionLabel(profile)}
          </li>
        </ul>
      </article>
    );
  }

  const pipeline = profile.pipeline;
  return (
    <article className="active-card">
      {header}
      <div className="active-flow">
        <div className="active-node">
          <Icon name="mic" size={16} />
          <span className="active-node-label">STT</span>
          <span className="active-node-value">{stageLabel(catalog, pipeline?.stt)}</span>
        </div>
        <span className="active-arrow">→</span>
        <div className="active-node">
          <Icon name="spark" size={16} />
          <span className="active-node-label">LLM</span>
          <span className="active-node-value">{stageLabel(catalog, pipeline?.llm)}</span>
        </div>
        <span className="active-arrow">→</span>
        <div className="active-node">
          <Icon name="waveform" size={16} />
          <span className="active-node-label">TTS</span>
          <span className="active-node-value">{stageLabel(catalog, pipeline?.tts)}</span>
        </div>
      </div>
      <ul className="active-facts">
        <li>
          <Icon name="check" size={13} /> Turn detection via{" "}
          {pipeline?.vad?.provider ? (findProvider(catalog, pipeline.vad.provider)?.label ?? pipeline.vad.provider) : "the session default"}
        </li>
        <li>
          <Icon name="check" size={13} /> Interruption: {interruptionLabel(profile)}
        </li>
      </ul>
    </article>
  );
}
