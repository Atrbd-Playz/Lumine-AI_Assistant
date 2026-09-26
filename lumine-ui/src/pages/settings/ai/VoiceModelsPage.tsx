import {
  findModel,
  findProvider,
  modelsFor,
  providersFor,
  voicesFor,
  type Capability,
  type CatalogModel,
  type ConfigDocument,
  type ModelRef,
  type OutputMode,
  type ProviderCatalog,
  type VoiceProfile,
} from "../../../features/settings/aiConfigTypes";
import { convertProfileKind, defaultModelRef } from "../../../features/settings/profileOps";
import { Icon } from "../../home/components/Icon";
import { ModelOptions } from "./ModelOptions";
import { ProfileList } from "./ProfileList";

/* -------------------------------------------------------------------------
 * Presentational pieces. Every label comes from the catalog, so no provider
 * name is ever hardcoded in JSX.
 * ---------------------------------------------------------------------- */

function stageLabel(catalog: ProviderCatalog, ref: ModelRef | undefined, capability: Capability): string {
  if (!ref?.provider) return "Not set";
  const provider = findProvider(catalog, ref.provider);
  const model = findModel(catalog, ref.provider, ref.model, capability);
  if (!provider || !model) return ref.provider;
  return `${provider.label} · ${model.label}`;
}

/** A single provider/model pair chooser, generated from the catalog. */
function StageSelect({
  catalog,
  label,
  capability,
  value,
  onChange,
  hint,
}: {
  catalog: ProviderCatalog;
  label: string;
  capability: Capability;
  value: ModelRef | undefined;
  onChange: (next: ModelRef) => void;
  hint?: string;
}) {
  const providers = providersFor(catalog, capability);
  const current = value?.provider ?? providers[0]?.id ?? "";
  const models = current ? modelsFor(catalog, current, capability) : [];

  // Changing the model drops the previous model's settings rather than carrying
  // them over: what one model accepts, another may reject, and a stale value
  // would be a request the provider refuses.
  const changeModel = (provider: string, model: string) => {
    onChange({
      provider,
      model,
      ...(value?.language ? { language: value.language } : {}),
    });
  };

  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <div className="field-row">
        <select
          value={current}
          onChange={(event) => {
            const provider = event.target.value;
            const first = modelsFor(catalog, provider, capability)[0];
            changeModel(provider, first?.id ?? "");
          }}
        >
          {providers.map((provider) => (
            <option key={provider.id} value={provider.id}>
              {provider.label}
            </option>
          ))}
        </select>
        <select value={value?.model ?? ""} onChange={(event) => changeModel(current, event.target.value)}>
          {models.length === 0 && <option value="">No models available</option>}
          {models.map((model) => (
            <option key={model.id} value={model.id}>
              {modelLabel(model)}
            </option>
          ))}
        </select>
      </div>
      {hint && <small className="field-hint">{hint}</small>}
    </label>
  );
}

function modelLabel(model: CatalogModel): string {
  const suffix =
    model.status === "deprecated" ? " (deprecated)" : model.status === "retired" ? " (retired)" : model.default ? " · default" : "";
  return `${model.label}${suffix}`;
}

function VoiceSelect({
  catalog,
  providerId,
  value,
  onChange,
}: {
  catalog: ProviderCatalog;
  providerId: string;
  value: string | undefined;
  onChange: (voice: string) => void;
}) {
  const voices = voicesFor(catalog, providerId);
  const listId = `voices-${providerId}`;
  // A text input with a datalist rather than a <select>: the curated list is a
  // shortcut, and any other voice name can still be typed. Providers publish far
  // more voices than we curate, and cloned voices have no name we could list.
  return (
    <label className="field">
      <span className="field-label">Voice</span>
      <input
        list={listId}
        value={value ?? ""}
        placeholder={voices[0]?.id ?? "voice name"}
        onChange={(event) => onChange(event.target.value)}
        spellCheck={false}
        autoComplete="off"
      />
      {voices.length > 0 && (
        <datalist id={listId}>
          {voices.map((voice) => (
            <option key={voice.id} value={voice.id}>
              {voice.label}
            </option>
          ))}
        </datalist>
      )}
      <small className="field-hint">
        {voices.length > 0
          ? `${voices.length} curated for ${findProvider(catalog, providerId)?.label ?? providerId}. Any other name can be typed.`
          : "This provider publishes no curated voice list; type the provider's voice id."}
      </small>
    </label>
  );
}

/** The pipeline / realtime summary card. */
function ActiveCard({
  catalog,
  profile,
  isEnvironmentBacked,
  hasUnsavedChanges,
}: {
  catalog: ProviderCatalog;
  profile: VoiceProfile;
  isEnvironmentBacked: boolean;
  hasUnsavedChanges: boolean;
}) {
  const stage = (capability: Capability, ref: ModelRef | undefined) => {
    const provider = ref?.provider ? findProvider(catalog, ref.provider) : undefined;
    return { provider, label: stageLabel(catalog, ref, capability) };
  };

  // Once the draft differs from what is saved, the environment label is
  // misleading: the card is showing edits, not what agent/.env says. A badge
  // that lies is worse than no badge.
  const badge = hasUnsavedChanges ? "Unsaved edits" : isEnvironmentBacked ? "From agent/.env" : "Saved";

  if (profile.kind === "realtime" && profile.realtime) {
    const realtime = profile.realtime;
    const output = realtime.output;
    const customTts = output.mode === "custom_tts";
    const ttsStage = customTts && "tts" in output ? stage("tts", output.tts) : null;
    return (
      <article className="active-card">
        <header>
          <div>
            <p className="eyebrow">Active configuration</p>
            <h3>{profile.name}</h3>
          </div>
          <span className="active-card-badge">{badge}</span>
        </header>
        <div className="active-flow">
          <div className="active-node">
            <Icon name="mic" size={16} />
            <span className="active-node-label">Realtime</span>
            <span className="active-node-value">{stageLabel(catalog, realtime, "realtime")}</span>
          </div>
          <span className="active-arrow">→</span>
          <div className="active-node">
            <Icon name="waveform" size={16} />
            <span className="active-node-label">{customTts ? "Custom TTS" : "Model voice"}</span>
            <span className="active-node-value">
              {customTts ? (ttsStage?.label ?? "Not set") : (realtime.voice ?? "Model default")}
            </span>
          </div>
        </div>
        <ul className="active-facts">
          <li>
            <Icon name="check" size={13} /> Audio input handled by the realtime model
          </li>
          <li>
            <Icon name="check" size={13} /> Audio output via {customTts ? "a separate TTS" : "the model's own voice"}
          </li>
        </ul>
      </article>
    );
  }

  const pipeline = profile.pipeline;
  const stt = stage("stt", pipeline?.stt);
  const llm = stage("llm", pipeline?.llm);
  const tts = stage("tts", pipeline?.tts);
  return (
    <article className="active-card">
      <header>
        <div>
          <p className="eyebrow">Active configuration</p>
          <h3>{profile.name}</h3>
        </div>
        <span className="active-card-badge">{badge}</span>
      </header>
      <div className="active-flow">
        <div className="active-node">
          <Icon name="mic" size={16} />
          <span className="active-node-label">STT</span>
          <span className="active-node-value">{stt.label}</span>
        </div>
        <span className="active-arrow">→</span>
        <div className="active-node">
          <Icon name="spark" size={16} />
          <span className="active-node-label">LLM</span>
          <span className="active-node-value">{llm.label}</span>
        </div>
        <span className="active-arrow">→</span>
        <div className="active-node">
          <Icon name="waveform" size={16} />
          <span className="active-node-label">TTS</span>
          <span className="active-node-value">{tts.label}</span>
        </div>
      </div>
      <ul className="active-facts">
        <li>
          <Icon name="check" size={13} /> Turn detection via {pipeline?.vad?.provider ? findProvider(catalog, pipeline.vad.provider)?.label : "the session default"}
        </li>
        <li>
          <Icon name="check" size={13} /> Interruption: {interruptionLabel(profile)}
        </li>
      </ul>
    </article>
  );
}

function interruptionLabel(profile: VoiceProfile): string {
  const mode =
    profile.kind === "realtime"
      ? profile.realtime?.turnHandling?.interruptionMode
      : profile.pipeline?.turnHandling?.interruptionMode;
  return mode === "finish_response" ? "finishes the reply" : "allows interruption";
}

function DiagnosticsList({ diagnostics }: { diagnostics: { severity: string; message: string; hint?: string; path?: string }[] }) {
  if (diagnostics.length === 0) {
    return (
      <p className="validation is-ok">
        <Icon name="check" size={15} /> This configuration is ready to use.
      </p>
    );
  }
  return (
    <ul className="validation-list">
      {diagnostics.map((diagnostic, index) => (
        <li key={`${diagnostic.path ?? ""}-${index}`} className={`validation is-${diagnostic.severity}`}>
          <Icon name={diagnostic.severity === "error" ? "close" : "clock"} size={14} />
          <span>
            <strong>{diagnostic.message}</strong>
            {diagnostic.hint && <em>{diagnostic.hint}</em>}
          </span>
        </li>
      ))}
    </ul>
  );
}

/* -------------------------------------------------------------------------
 * Page
 * ---------------------------------------------------------------------- */

type VoiceModelsPageProps = {
  catalog: ProviderCatalog;
  profile: VoiceProfile;
  document: ConfigDocument;
  isEnvironmentBacked: boolean;
  diagnostics: { severity: string; message: string; hint?: string; path?: string }[];
  validating: boolean;
  onChange: (profile: VoiceProfile) => void;
  onMutateDocument: (operation: (document: ConfigDocument) => ConfigDocument) => void;
  onSave: () => void;
  onDiscard: () => void;
  canSave: boolean;
  saving: boolean;
  hasUnsavedChanges: boolean;
  saveError: string | null;
};

export function VoiceModelsPage(props: VoiceModelsPageProps) {
  const {
    catalog,
    profile,
    isEnvironmentBacked,
    diagnostics,
    validating,
    onChange,
    hasUnsavedChanges,
  } = props;
  const isRealtime = profile.kind === "realtime";

  const setPipeline = (mutate: (pipeline: NonNullable<VoiceProfile["pipeline"]>) => NonNullable<VoiceProfile["pipeline"]>) => {
    if (!profile.pipeline) return;
    onChange({ ...profile, pipeline: mutate(profile.pipeline) });
  };

  const setRealtime = (mutate: (realtime: NonNullable<VoiceProfile["realtime"]>) => NonNullable<VoiceProfile["realtime"]>) => {
    if (!profile.realtime) return;
    onChange({ ...profile, realtime: mutate(profile.realtime) });
  };

  const realtimeModel = profile.realtime
    ? findModel(catalog, profile.realtime.provider, profile.realtime.model, "realtime")
    : undefined;
  const supportsCustomTts = realtimeModel?.realtime?.textOnlyModality ?? false;
  // Seeded from the catalog like every other stage, so a provider added on the
  // Python side needs no change here. Undefined only if the catalog has no TTS
  // at all, in which case the control is disabled rather than left half-set.
  const customTtsSeed = defaultModelRef(catalog, "tts");
  const outputMode: OutputMode = (profile.realtime?.output?.mode ?? "model_voice") as OutputMode;

  return (
    <div className="settings-page">
      <header className="settings-page-head">
        <div>
          <p className="eyebrow">AI</p>
          <h1>Voice &amp; Models</h1>
          <p>Choose the speech stack Lumine uses. Changes apply to the next voice session.</p>
        </div>
      </header>

      <ProfileList
        catalog={catalog}
        document={props.document}
        isEnvironmentBacked={isEnvironmentBacked}
        hasUnsavedChanges={hasUnsavedChanges}
        onMutate={props.onMutateDocument}
        onDiscard={props.onDiscard}
      />

      <label className="field">
        <span className="field-label">Profile name</span>
        <input
          value={profile.name}
          onChange={(event) => onChange({ ...profile, name: event.target.value })}
          placeholder="Lumine Default"
          spellCheck={false}
        />
      </label>

      <ActiveCard
        catalog={catalog}
        profile={profile}
        isEnvironmentBacked={isEnvironmentBacked}
        hasUnsavedChanges={hasUnsavedChanges}
      />

      {isEnvironmentBacked && (
        <p className="notice">
          This profile is derived from <code>agent/.env</code>. Edit it below and save to take control of the
          stack from the desktop app.
        </p>
      )}

      <section className="settings-block">
        <div className="settings-block-head">
          <h2>Stack type</h2>
          <p>
            A pipeline runs speech recognition, a language model, and speech synthesis as separate
            stages, so each can be chosen independently. A realtime model does all three in one, which
            is lower latency but fixes the model for the whole turn.
          </p>
        </div>
        <div className="segmented">
          <button
            type="button"
            className={!isRealtime ? "active" : ""}
            onClick={() => onChange(convertProfileKind(catalog, profile, "pipeline"))}
          >
            Pipeline
          </button>
          <button
            type="button"
            className={isRealtime ? "active" : ""}
            onClick={() => onChange(convertProfileKind(catalog, profile, "realtime"))}
          >
            Realtime
          </button>
        </div>
        {isRealtime ? (
          <small className="field-hint">
            Speech recognition, the language model, and the voice are fixed to this model. Switch to
            Pipeline to choose them independently.
          </small>
        ) : (
          <small className="field-hint">
            Each stage is chosen below. Switching back to Realtime will pick a default realtime model,
            because these stages have no equivalent to map onto.
          </small>
        )}
      </section>

      {isRealtime && profile.realtime ? (
        <>
          <section className="settings-block">
            <div className="settings-block-head">
              <h2>Realtime model</h2>
              <p>The model that hears the user and produces the reply.</p>
            </div>
            <StageSelect
              catalog={catalog}
              label="Provider and model"
              capability="realtime"
              value={profile.realtime}
              onChange={(next) => setRealtime((realtime) => ({ ...realtime, ...next }))}
            />
            <VoiceSelect
              catalog={catalog}
              providerId={profile.realtime.provider}
              value={profile.realtime.voice}
              onChange={(voice) => setRealtime((realtime) => ({ ...realtime, voice }))}
            />
          </section>

          <section className="settings-block">
            <div className="settings-block-head">
              <h2>Output</h2>
              <p>Who speaks the reply.</p>
            </div>
            <div className="segmented">
              <button
                type="button"
                className={outputMode === "model_voice" ? "active" : ""}
                onClick={() => setRealtime((realtime) => ({ ...realtime, output: { mode: "model_voice" } }))}
              >
                Model voice
              </button>
              <button
                type="button"
                className={outputMode === "custom_tts" ? "active" : ""}
                disabled={!supportsCustomTts || !customTtsSeed}
                onClick={() =>
                  setRealtime((realtime) =>
                    customTtsSeed
                      ? {
                          ...realtime,
                          output: { mode: "custom_tts", tts: customTtsSeed },
                        }
                      : realtime,
                  )
                }
              >
                Custom TTS
              </button>
            </div>
            {!supportsCustomTts && (
              <p className="notice is-warning">
                {realtimeModel?.label ?? "This model"} speaks for itself and cannot be paired with a separate
                speech synthesizer. Choose a non-native-audio realtime model to use a custom voice.
              </p>
            )}
            {outputMode === "custom_tts" && profile.realtime.output?.mode === "custom_tts" && (
              <div className="nested-fields">
                <StageSelect
                  catalog={catalog}
                  label="Speech synthesizer"
                  capability="tts"
                  value={profile.realtime.output.tts}
                  onChange={(next) =>
                    setRealtime((realtime) => ({
                      ...realtime,
                      output: { ...realtime.output, mode: "custom_tts", tts: next } as NonNullable<VoiceProfile["realtime"]>["output"],
                    }))
                  }
                />
                <VoiceSelect
                  catalog={catalog}
                  providerId={profile.realtime.output.tts.provider}
                  value={profile.realtime.output.voice}
                  onChange={(voice) =>
                    setRealtime((realtime) => ({
                      ...realtime,
                      output: { ...realtime.output, voice } as NonNullable<VoiceProfile["realtime"]>["output"],
                    }))
                  }
                />
              </div>
            )}
          </section>
        </>
      ) : (
        profile.pipeline && (
          <>
            <section className="settings-block">
              <div className="settings-block-head">
                <h2>Stages</h2>
                <p>Each stage is picked independently. The providers only need a credential for the stages you use.</p>
              </div>
              <StageSelect
                catalog={catalog}
                label="Speech recognition"
                capability="stt"
                value={profile.pipeline.stt}
                onChange={(next) => setPipeline((pipeline) => ({ ...pipeline, stt: next }))}
              />
              <StageSelect
                catalog={catalog}
                label="Language model"
                capability="llm"
                value={profile.pipeline.llm}
                onChange={(next) => setPipeline((pipeline) => ({ ...pipeline, llm: next }))}
              />
              <ModelOptions
                model={findModel(catalog, profile.pipeline.llm.provider, profile.pipeline.llm.model, "llm")}
                values={profile.pipeline.llm.options ?? {}}
                onChange={(options) =>
                  setPipeline((pipeline) => ({
                    ...pipeline,
                    llm: { ...pipeline.llm, ...(Object.keys(options).length > 0 ? { options } : {}) },
                  }))
                }
                emptyHint="This model takes no extra settings; the provider decides."
              />
              <StageSelect
                catalog={catalog}
                label="Speech synthesis"
                capability="tts"
                value={profile.pipeline.tts}
                onChange={(next) =>
                  setPipeline((pipeline) => ({ ...pipeline, tts: { ...next, voice: pipeline.tts.voice } }))
                }
              />
              <VoiceSelect
                catalog={catalog}
                providerId={profile.pipeline.tts.provider}
                value={profile.pipeline.tts.voice}
                onChange={(voice) => setPipeline((pipeline) => ({ ...pipeline, tts: { ...pipeline.tts, voice } }))}
              />
              <StageSelect
                catalog={catalog}
                label="Turn detection"
                capability="vad"
                value={profile.pipeline.vad}
                onChange={(next) => setPipeline((pipeline) => ({ ...pipeline, vad: next }))}
                hint="Some transcription backends need this to know when a segment of speech ended."
              />
            </section>
          </>
        )
      )}

      <section className="settings-block">
        <div className="settings-block-head">
          <h2>Interruption</h2>
          <p>What happens when the user speaks while Lumine is answering.</p>
        </div>
        <div className="segmented">
          <button
            type="button"
            className={interruptionOf(profile) === "finish_response" ? "active" : ""}
            onClick={() => setInterruption(profile, onChange, "finish_response")}
          >
            Finish reply
          </button>
          <button
            type="button"
            className={interruptionOf(profile) === "barge_in" ? "active" : ""}
            onClick={() => setInterruption(profile, onChange, "barge_in")}
          >
            Allow interruption
          </button>
        </div>
        <small className="field-hint">Applies to the next voice session.</small>
      </section>

      <section className="settings-block">
        <div className="settings-block-head">
          <h2>Checks</h2>
          <p>Run by the agent itself, so these rules match what will actually start.</p>
        </div>
        {validating && <p className="validation is-pending">Checking…</p>}
        <DiagnosticsList diagnostics={diagnostics} />
      </section>

      <footer className="settings-actions">
        <button type="button" className="settings-secondary" onClick={props.onDiscard} disabled={!hasUnsavedChanges}>
          Discard
        </button>
        <button type="button" className="settings-primary" onClick={props.onSave} disabled={!props.canSave}>
          {props.saving ? "Saving…" : "Save configuration"}
        </button>
        {props.saveError && <span className="settings-action-error">{props.saveError}</span>}
      </footer>
    </div>
  );
}

function interruptionOf(profile: VoiceProfile): string {
  return profile.kind === "realtime"
    ? (profile.realtime?.turnHandling?.interruptionMode ?? "barge_in")
    : (profile.pipeline?.turnHandling?.interruptionMode ?? "barge_in");
}

function setInterruption(profile: VoiceProfile, onChange: (next: VoiceProfile) => void, mode: "barge_in" | "finish_response") {
  if (profile.kind === "realtime" && profile.realtime) {
    onChange({
      ...profile,
      realtime: { ...profile.realtime, turnHandling: { interruptionMode: mode } },
    });
  } else if (profile.pipeline) {
    onChange({
      ...profile,
      pipeline: { ...profile.pipeline, turnHandling: { interruptionMode: mode } },
    });
  }
}
