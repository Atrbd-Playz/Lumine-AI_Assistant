import { findModel, type OutputMode, type ProviderCatalog, type SavedVoice, type VoiceProfile } from "../../../features/settings/aiConfigTypes";
import { convertProfileKind, defaultModelRef } from "../../../features/settings/profileOps";
import { Hint } from "../../../components/ui/hint";
import { Dropdown } from "../../../components/ui/dropdown";
import { SettingsPageHeader } from "../components/SettingsPageHeader";
import { Field } from "../components/Field";
import { AiSaveFooter, type AiSaveFooterProps } from "../components/AiSaveFooter";
import { ModelOptions } from "./ModelOptions";
import { LocalModels } from "./LocalModels";
import { StageSelect } from "./StageSelect";
import { VoiceControls } from "./VoiceControls";
import { VoiceSelect } from "./VoiceSelect";

/**
 * One stage of the speech stack per tab.
 *
 * ## Why this is four screens
 *
 * These were four fields in one scrolling column, between a stack-type toggle and
 * the save button. The language model carried the most settings, so it pushed the
 * voice controls off the bottom, and finding "which voice" meant reading past
 * everything about transcription. Splitting by stage puts a stage, the model that
 * serves it and that model's own settings in the same column, with nothing else on
 * the screen.
 *
 * ## A realtime stack has no four stages, and says so
 *
 * Switching to a realtime model means one model hears, thinks and speaks. The
 * tabs are still there — the same model genuinely does answer each of those
 * questions — and the notice explains why the four screens name the same model,
 * rather than three of them becoming an empty state asking the reader to undo a
 * choice they made on purpose.
 */
/**
 * The provider whose model list is not knowable in advance.
 *
 * Named here rather than read from the catalog because the answer to "should this
 * tab offer discovery" is a question about one provider, and a catalog-derived
 * rule would be a general mechanism existing for a single case. `local` stays a
 * reserved id in the catalog for inference that ships *with the app*; this is a
 * server the user starts, which is a different promise.
 */
const LOCAL_PROVIDER_ID = "ollama";

export type ModelsPageProps = AiSaveFooterProps & {
  catalog: ProviderCatalog;
  profile: VoiceProfile;
  isEnvironmentBacked: boolean;
  /** Which stage this tab is showing. */
  tab: string;
  onChange: (profile: VoiceProfile) => void;
  /**
   * Voices the user kept, and the only way to change them.
   *
   * Belongs to the document rather than to the profile — a saved voice is
   * something *this install* remembers, not a property of one stack — so it is
   * handed down rather than owned here. Optional because a host with no document
   * to write still has to render a working voice picker; it simply loses the
   * save row.
   */
  savedVoices?: SavedVoice[];
  onSavedVoicesChange?: (voices: SavedVoice[]) => void;
};

export function ModelsPage({
  catalog,
  profile,
  isEnvironmentBacked,
  tab,
  onChange,
  savedVoices,
  onSavedVoicesChange,
  ...footer
}: ModelsPageProps) {
  const isRealtime = profile.kind === "realtime";

  const setPipeline = (
    mutate: (pipeline: NonNullable<VoiceProfile["pipeline"]>) => NonNullable<VoiceProfile["pipeline"]>,
  ) => {
    if (!profile.pipeline) return;
    onChange({ ...profile, pipeline: mutate(profile.pipeline) });
  };

  const setRealtime = (
    mutate: (realtime: NonNullable<VoiceProfile["realtime"]>) => NonNullable<VoiceProfile["realtime"]>,
  ) => {
    if (!profile.realtime) return;
    onChange({ ...profile, realtime: mutate(profile.realtime) });
  };

  const realtimeModel = profile.realtime
    ? findModel(catalog, profile.realtime.provider, profile.realtime.model, "realtime")
    : undefined;

  return (
    <div className="settings-page">
      <SettingsPageHeader section="models" />

      <section className="settings-block">
        <div className="settings-block-head flex items-center gap-1.5">
          <h2>Stack type</h2>
          <Hint label="What the two stack types are">
            A pipeline runs speech recognition, a language model and speech synthesis as separate stages, so
            each can be chosen independently. A realtime model does all three in one, which is lower
            latency but fixes the model for the whole turn.
          </Hint>
        </div>
        <Field
          label="How Lumine is put together"
          hint={
            isRealtime
              ? "The tabs below all name the same model, because a realtime model does all four jobs."
              : "Each tab is one stage. Switch back to Realtime and the stages are replaced by a single model."
          }
        >
          <Dropdown
            label="Stack type"
            value={isRealtime ? "realtime" : "pipeline"}
            onChange={(next) => onChange(convertProfileKind(catalog, profile, next === "realtime" ? "realtime" : "pipeline"))}
            options={[
              { value: "pipeline", label: "Pipeline", hint: "Three stages, each chosen on its own" },
              { value: "realtime", label: "Realtime", hint: "One model does all three" },
            ]}
          />
        </Field>
      </section>

      {tab === "tts" && <AudioPathNotice catalog={catalog} profile={profile} onChange={onChange} />}

      {isRealtime ? (
        <RealtimeStages
          tab={tab}
          catalog={catalog}
          profile={profile}
          realtimeModel={realtimeModel}
          isEnvironmentBacked={isEnvironmentBacked}
          setRealtime={setRealtime}
          savedVoices={savedVoices}
          onSavedVoicesChange={onSavedVoicesChange}
        />
      ) : (
        <PipelineStages
          tab={tab}
          catalog={catalog}
          profile={profile}
          setPipeline={setPipeline}
          savedVoices={savedVoices}
          onSavedVoicesChange={onSavedVoicesChange}
        />
      )}

      <AiSaveFooter {...footer} />
    </div>
  );
}

/* ------------------------------------------------------------------------- */

/**
 * Which stage is actually speaking, and whether what is tuned here reaches it.
 *
 * Per-reply emotion has a path or it does not. It needs a language model
 * streaming text into a separate synthesizer — the pipeline — and cannot exist
 * on a realtime model, which hears, reasons and speaks in a single call so
 * there is no synthesizer to talk to. The stack control that decides it sits
 * directly above, so the answer is stated here rather than left to be
 * discovered: a person who has just chosen a voice is owed the news *before*
 * they choose a delivery to go with it, not afterwards in a forum post.
 *
 * Shown on the speech tab only. It is a fact about the voice, the other three
 * tabs are not about the voice, and a banner that appears on every screen is a
 * banner that stops being read.
 */
function AudioPathNotice({
  catalog,
  profile,
  onChange,
}: {
  catalog: ProviderCatalog;
  profile: VoiceProfile;
  onChange: (profile: VoiceProfile) => void;
}) {
  const realtime = profile.kind === "realtime";
  const pipeline = profile.pipeline;

  const label = (ref: { provider: string; model: string } | undefined, capability: "stt" | "llm" | "tts" | "realtime") => {
    if (!ref) return "";
    const found = findModel(catalog, ref.provider, ref.model, capability);
    // A model the catalog has never heard of still names itself; falling back
    // to the raw id keeps the path readable rather than leaving a blank where
    // the stage should be.
    return found?.label ?? ref.model;
  };

  const segments: Array<{ text: string; speaking?: boolean }> = realtime
    ? [{ text: label(profile.realtime, "realtime"), speaking: true }]
    : [
        { text: label(pipeline?.stt, "stt") },
        { text: label(pipeline?.llm, "llm") },
        { text: label(pipeline?.tts, "tts"), speaking: true },
      ].filter((segment) => segment.text);

  const speaking = realtime
    ? label(profile.realtime, "realtime")
    : label(pipeline?.tts, "tts");

  return (
    <section className="audio-path">
      <div className="audio-path-head flex items-center justify-between gap-3">
        <h3>Your audio path</h3>
        {realtime && (
          <button
            type="button"
            className="settings-quiet py-[9px] px-1.5 text-soft bg-transparent text-[12px] cursor-pointer"
            onClick={() => onChange(convertProfileKind(catalog, profile, "pipeline"))}
          >
            Use a pipeline instead
          </button>
        )}
      </div>

      <p className="audio-path-stages flex flex-wrap items-center gap-1 m-0 text-foreground text-[13.5px]">
        {segments.map((segment, index) => (
          <span key={segment.text}>
            {index > 0 && <span className="audio-path-arrow" aria-hidden="true">→</span>}
            <span className={segment.speaking ? "audio-path-stage is-speaking" : "audio-path-stage"}>
              {segment.text}
            </span>
          </span>
        ))}
        {realtime && <span className="audio-path-all">hears, reasons and speaks</span>}
      </p>

      <p className="audio-path-note max-w-[74ch] m-0 text-soft text-[11.5px] leading-[1.55]">
        {realtime ? (
          <>
            A realtime model speaks for itself, so per-reply emotion and Cartesia's delivery controls do not
            reach it — there is no separate synthesizer to send them to.
          </>
        ) : (
          <>
            Every reply is spoken by <strong>{speaking}</strong>, so per-reply emotion and the delivery
            controls below reach the voice.
          </>
        )}
      </p>
    </section>
  );
}

/* ------------------------------------------------------------------------- */

function PipelineStages({
  tab,
  catalog,
  profile,
  setPipeline,
  savedVoices,
  onSavedVoicesChange,
}: {
  tab: string;
  catalog: ProviderCatalog;
  profile: VoiceProfile;
  setPipeline: (mutate: (pipeline: NonNullable<VoiceProfile["pipeline"]>) => NonNullable<VoiceProfile["pipeline"]>) => void;
  savedVoices?: SavedVoice[];
  onSavedVoicesChange?: (voices: SavedVoice[]) => void;
}) {
  const pipeline = profile.pipeline;
  if (!pipeline) return null;

  if (tab === "stt") {
    const model = findModel(catalog, pipeline.stt.provider, pipeline.stt.model, "stt");
    return (
      <>
        <StageSelect
          catalog={catalog}
          label="Speech recognition"
          capability="stt"
          value={pipeline.stt}
          onChange={(next) => setPipeline((current) => ({ ...current, stt: next }))}
        />
        {model?.requiresVad && (
          <p className="notice is-warning">
            {model.label} transcribes whole segments rather than a stream, so Lumine waits for you to stop
            speaking before it hears a word. A streaming model responds mid-turn.
          </p>
        )}
        <ModelOptions
          model={model}
          values={pipeline.stt.options ?? {}}
          onChange={(options) =>
            setPipeline((current) => ({
              ...current,
              stt: { ...current.stt, ...(Object.keys(options).length > 0 ? { options } : {}) },
            }))
          }
          emptyHint="This model takes no extra settings; the provider decides."
        />
      </>
    );
  }

  if (tab === "llm") {
    const model = findModel(catalog, pipeline.llm.provider, pipeline.llm.model, "llm");
    const llmOptions = pipeline.llm.options ?? {};
    return (
      <>
        <StageSelect
          catalog={catalog}
          label="Language model"
          capability="llm"
          value={pipeline.llm}
          onChange={(next) => setPipeline((current) => ({ ...current, llm: next }))}
        />
        {/* Only for a local provider, and only because the catalog cannot know
            what is installed here. A hosted provider's list is finished, so a
            discovery panel beside it would be a button that can only say "here
            is the list again". */}
        {pipeline.llm.provider === LOCAL_PROVIDER_ID && (
          <LocalModels
            baseUrl={llmOptions.base_url ?? ""}
            selected={pipeline.llm.model}
            onPick={(modelId) => setPipeline((current) => ({ ...current, llm: { ...current.llm, model: modelId } }))}
          />
        )}
        <ModelOptions
          model={model}
          values={llmOptions}
          onChange={(options) =>
            setPipeline((current) => ({
              ...current,
              llm: { ...current.llm, ...(Object.keys(options).length > 0 ? { options } : {}) },
            }))
          }
          emptyHint="This model takes no extra settings; the provider decides."
        />
      </>
    );
  }

  if (tab === "tts") {
    return (
      <>
        <StageSelect
          catalog={catalog}
          label="Speech synthesis"
          capability="tts"
          value={pipeline.tts}
          onChange={(next) => setPipeline((current) => ({ ...current, tts: { ...next, voice: current.tts.voice } }))}
        />
        <div className="nested-fields">
          <VoiceControls
            catalog={catalog}
            stage={pipeline.tts}
            onChange={(tts) => setPipeline((current) => ({ ...current, tts }))}
            savedVoices={savedVoices}
            onSavedVoicesChange={onSavedVoicesChange}
          />
        </div>
      </>
    );
  }

  // Turn detection. Not a model in the language sense, but it decides when a turn
  // is over, so it belongs beside the other three rather than under advanced.
  return (
    <>
      <StageSelect
        catalog={catalog}
        label="Voice activity detection"
        capability="vad"
        value={pipeline.vad}
        onChange={(next) => setPipeline((current) => ({ ...current, vad: next }))}
        hint="Some transcription backends need this to know when a segment of speech ended."
      />
      <Interruption profile={profile} setPipeline={setPipeline} />
    </>
  );
}

/* ------------------------------------------------------------------------- */

function RealtimeStages({
  tab,
  catalog,
  profile,
  realtimeModel,
  isEnvironmentBacked,
  setRealtime,
  savedVoices,
  onSavedVoicesChange,
}: {
  tab: string;
  catalog: ProviderCatalog;
  profile: VoiceProfile;
  realtimeModel: ReturnType<typeof findModel>;
  isEnvironmentBacked: boolean;
  setRealtime: (mutate: (realtime: NonNullable<VoiceProfile["realtime"]>) => NonNullable<VoiceProfile["realtime"]>) => void;
  savedVoices?: SavedVoice[];
  onSavedVoicesChange?: (voices: SavedVoice[]) => void;
}) {
  const realtime = profile.realtime;
  if (!realtime) return null;

  const output = realtime.output;
  const outputMode: OutputMode = (output?.mode ?? "model_voice") as OutputMode;
  // Seeded from the catalog like every other stage, so a provider added on the
  // Python side needs no change here. Undefined only if the catalog has no TTS at
  // all, in which case the control is disabled rather than left half-set.
  const customTtsSeed = defaultModelRef(catalog, "tts");
  const supportsCustomTts = realtimeModel?.realtime?.textOnlyModality ?? false;

  const speaker = (
    <>
      <p className="notice">
        A realtime model does all four jobs at once, so every tab names this one model.{" "}
        {isEnvironmentBacked && "It is currently derived from agent/.env."}
      </p>
      <StageSelect
        catalog={catalog}
        label="Realtime model"
        capability="realtime"
        value={realtime}
        onChange={(next) => setRealtime((current) => ({ ...current, ...next }))}
      />
      <VoiceSelect
        catalog={catalog}
        providerId={realtime.provider}
        value={realtime.voice}
        onChange={(voice) => setRealtime((current) => ({ ...current, voice }))}
        label="Voice"
      />
    </>
  );

  if (tab === "vad") {
    return (
      <>
        {speaker}
        <Interruption profile={profile} setRealtime={setRealtime} />
      </>
    );
  }

  if (tab === "tts") {
    return (
      <>
        {speaker}
        <Field
          label="Who speaks the reply"
          help="The model's own voice, or a separate speech synthesizer. A native-audio model cannot be split, because it speaks as it thinks."
          hint={
            supportsCustomTts
              ? undefined
              : `${realtimeModel?.label ?? "This model"} speaks for itself and cannot be paired with a separate speech synthesizer.`
          }
        >
          <Dropdown
            label="Output"
            value={outputMode}
            onChange={(next) =>
              setRealtime((current) =>
                next === "custom_tts" && customTtsSeed
                  ? { ...current, output: { mode: "custom_tts", tts: customTtsSeed } }
                  : { ...current, output: { mode: "model_voice" } },
              )
            }
            options={[
              { value: "model_voice", label: "The model's own voice" },
              { value: "custom_tts", label: "A separate speech synthesizer", disabled: !supportsCustomTts || !customTtsSeed },
            ]}
          />
        </Field>
        {outputMode === "custom_tts" && output?.mode === "custom_tts" && (
          <div className="nested-fields">
            <StageSelect
              catalog={catalog}
              label="Speech synthesizer"
              capability="tts"
              value={output.tts}
              onChange={(next) =>
                setRealtime((current) => ({
                  ...current,
                  output: { ...current.output, mode: "custom_tts", tts: next } as NonNullable<VoiceProfile["realtime"]>["output"],
                }))
              }
            />
            <VoiceControls
              catalog={catalog}
              // The realtime document keeps `voice` beside the stage rather than
              // inside it, so the control is handed one merged shape and its answer
              // is split back the same way. Changing that would mean a second
              // document layout for the same concept.
              stage={{ ...output.tts, voice: output.voice }}
              onChange={(next) =>
                setRealtime((current) => {
                  const { voice, ...tts } = next;
                  return {
                    ...current,
                    output: {
                      ...current.output,
                      mode: "custom_tts",
                      tts,
                      ...(voice ? { voice } : {}),
                    } as NonNullable<VoiceProfile["realtime"]>["output"],
                  };
                })
              }
              savedVoices={savedVoices}
              onSavedVoicesChange={onSavedVoicesChange}
            />
          </div>
        )}
      </>
    );
  }

  return (
    <>
      {speaker}
      <ModelOptions
        model={realtimeModel}
        values={realtime.options ?? {}}
        onChange={(options) =>
          setRealtime((current) => {
            // The same rule as a pipeline stage: an option set that has been reset
            // is removed rather than written as `{}`, so a profile that has been
            // cleared reads as cleared instead of as a profile that sends nothing.
            const { options: cleared, ...rest } = current;
            void cleared;
            return Object.keys(options).length > 0 ? { ...rest, options } : rest;
          })
        }
        emptyHint="This model takes no extra settings; the provider decides."
      />
    </>
  );
}

/* ------------------------------------------------------------------------- */

/**
 * What happens if you speak over Lumine.
 *
 * On the turn-detection tab rather than in a settings block of its own, because
 * it is a property of the conversation rather than of any one stage: the same
 * choice applies to a pipeline's stages and to a realtime model alike.
 */
function Interruption({
  profile,
  setPipeline,
  setRealtime,
}: {
  profile: VoiceProfile;
  setPipeline?: (mutate: (pipeline: NonNullable<VoiceProfile["pipeline"]>) => NonNullable<VoiceProfile["pipeline"]>) => void;
  setRealtime?: (mutate: (realtime: NonNullable<VoiceProfile["realtime"]>) => NonNullable<VoiceProfile["realtime"]>) => void;
}) {
  const current =
    profile.kind === "realtime"
      ? (profile.realtime?.turnHandling?.interruptionMode ?? "barge_in")
      : (profile.pipeline?.turnHandling?.interruptionMode ?? "barge_in");

  return (
    <Field
      label="Interruption"
      help="Finish reply waits for the current sentence to end. Allow interruption cuts the answer off mid-sentence, which is faster but can clip a word."
      hint="Applies to the next voice session."
    >
      <Dropdown
        label="Interruption"
        value={current}
        onChange={(next) => {
          const mode = next === "finish_response" ? "finish_response" : "barge_in";
          if (profile.kind === "realtime" && setRealtime) {
            setRealtime((realtime) => ({ ...realtime, turnHandling: { interruptionMode: mode } }));
          } else if (profile.pipeline && setPipeline) {
            setPipeline((pipeline) => ({ ...pipeline, turnHandling: { interruptionMode: mode } }));
          }
        }}
        options={[
          { value: "barge_in", label: "Allow interruption", hint: "Cuts the answer off mid-sentence" },
          { value: "finish_response", label: "Finish the reply", hint: "Waits for the end of the sentence" },
        ]}
      />
    </Field>
  );
}
