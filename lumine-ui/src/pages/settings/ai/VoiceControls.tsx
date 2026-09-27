import { useCallback, useEffect, useRef, useState } from "react";

import {
  findModel,
  voicesFor,
  type CatalogOption,
  type ModelRef,
  type ProviderCatalog,
} from "../../../features/settings/aiConfigTypes";
import { previewVoice, type VoicePreview } from "../../../features/settings/aiConfigClient";
import { Dropdown } from "../../../components/ui/dropdown";
import { Knob } from "../../../components/ui/knob";
import { Hint } from "../../../components/ui/hint";

/**
 * How Lumine sounds: which voice, how fast, how coloured, how loud.
 *
 * ## Why this is its own section
 *
 * The stage chooser answered "which synthesiser", and the answer to that is a
 * provider and a model id. Everything after it — the voice, the pace, the
 * delivery — was not on the page at all, because the pipeline screen renders
 * `ModelOptions` for the language model and nothing for speech. A user who wanted
 * Lumine to speak faster had no way to say so except an environment variable.
 *
 * ## Nothing here names a provider
 *
 * The knobs, their ranges, and their end labels all come from the catalog, which
 * took them from each plugin's own signature. Cartesia sonic-3 gets speed, volume
 * and a 59-value emotion list; Google's TTS gets speed only, because that is all
 * its plugin accepts. Adding a knob is a line in `agent/providers.py`, not a
 * branch in this file.
 *
 * ## Why there is a preview
 *
 * Speed, volume and emotion are the only settings in the product that cannot be
 * judged by reading them. `speed: 1.25` is a number; what it sounds like is not
 * something a label can convey. So the section can speak a phrase with the current
 * draft — unsaved changes included, through the worker's own stage builder, so
 * what you hear is the voice the session will use.
 */
export type VoiceControlsProps = {
  catalog: ProviderCatalog;
  /** The TTS stage being edited. Never undefined — the caller picks one. */
  stage: ModelRef & { voice?: string };
  onChange: (stage: ModelRef & { voice?: string }) => void;
};

export function VoiceControls({ catalog, stage, onChange }: VoiceControlsProps) {
  const model = findModel(catalog, stage.provider, stage.model, "tts");
  const options = stage.options ?? {};
  const voices = voicesFor(catalog, stage.provider);

  const setOption = useCallback(
    (name: string, value: string) => {
      const next = { ...options };
      // An emptied setting is deleted rather than sent as "". The worker treats an
      // absent key as "the provider decides"; a present empty string is a value the
      // provider will reject.
      if (value === "") {
        delete next[name];
      } else {
        next[name] = value;
      }
      const merged: ModelRef & { voice?: string } = { ...stage, options: next };
      if (Object.keys(next).length === 0) {
        delete merged.options;
      }
      onChange(merged);
    },
    [options, stage, onChange],
  );

  const setVoice = useCallback(
    (voice: string) => {
      const next: ModelRef & { voice?: string } = { ...stage };
      if (voice === "") {
        delete next.voice;
      } else {
        next.voice = voice;
      }
      onChange(next);
    },
    [stage, onChange],
  );

  if (!model) {
    return (
      <p className="text-[12.5px] text-faint">
        This synthesizer is not in the catalog, so its settings cannot be shown. The saved values are still sent.
      </p>
    );
  }

  // `voice` is drawn as its own dropdown rather than as one row among the options,
  // because a provider's voice list is the one setting whose values are worth a
  // search box of their own.
  const voiceOption = model.options.find((option) => option.name === "voice");
  const knobs = model.options.filter(
    (option) => option.name !== "voice" && option.control === "slider" && option.minimum !== null && option.maximum !== null,
  );
  const choices = model.options.filter(
    (option) => option.name !== "voice" && (option.control === "combobox" || option.control === "select"),
  );
  const free = model.options.filter(
    (option) => option.name !== "voice" && option.control !== "slider" && option.values.length === 0,
  );

  return (
    <div className="flex flex-col gap-5">
      <VoicePicker
        catalog={catalog}
        providerId={stage.provider}
        value={stage.voice ?? ""}
        curated={voices}
        option={voiceOption}
        onChange={setVoice}
      />

      {knobs.length > 0 && (
        <div className="grid gap-5 sm:grid-cols-2">
          {knobs.map((option) => (
            <Knob
              key={option.name}
              name={option.name}
              value={options[option.name] ?? ""}
              onChange={(value) => setOption(option.name, value)}
              minimum={option.minimum!}
              maximum={option.maximum!}
              step={option.step ?? 0.05}
              ends={endsFor(option)}
              help={option.notes}
            />
          ))}
        </div>
      )}

      {choices.length > 0 && (
        <div className="flex flex-col gap-3">
          {choices.map((option) => (
            <div key={option.name} className="flex min-w-0 flex-col gap-1.5">
              <div className="flex items-center gap-1.5">
                <span className="text-[12.5px] font-medium text-foreground">{title(option.name)}</span>
                {option.notes && <Hint>{option.notes}</Hint>}
              </div>
              <Dropdown
                label={title(option.name)}
                value={options[option.name] ?? ""}
                onChange={(value) => setOption(option.name, value)}
                placeholder="The voice's own delivery"
                options={option.values.map((value) => ({ value, label: value }))}
              />
            </div>
          ))}
        </div>
      )}

      {free.length > 0 && (
        <div className="flex flex-wrap gap-4">
          {free.map((option) => (
            <label key={option.name} className="flex min-w-[8rem] flex-1 flex-col gap-1.5">
              <span className="text-[12.5px] font-medium text-foreground">{title(option.name)}</span>
              <input
                type="text"
                value={options[option.name] ?? ""}
                placeholder={placeholderFor(option)}
                spellCheck={false}
                autoComplete="off"
                onChange={(event) => setOption(option.name, event.target.value.trim())}
                className="h-9 rounded-sm bg-surface-muted px-3 text-[13px] text-foreground shadow-elev-1 outline-none placeholder:text-faint focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
              />
            </label>
          ))}
        </div>
      )}

      <PreviewBar stage={stage} />
    </div>
  );
}

/**
 * The sentinel row in the voice dropdown that reveals the free-text field.
 *
 * It cannot collide with a real voice id: those are a provider slug or a UUID, and
 * a leading space is not valid in either.
 */
const CUSTOM = " custom";

/**
 * The voice chooser: a dropdown of the curated list, with an escape hatch.
 *
 * The curated list is a shortcut and not a limit. Providers publish far more
 * voices than a catalog can carry, and a cloned voice has no name anyone could
 * have written down, so a custom name still has to be typeable — just not the
 * default answer, which is why it is one row at the bottom rather than a free-text
 * box next to a list.
 */
function VoicePicker({
  catalog,
  providerId,
  value,
  curated,
  option,
  onChange,
}: {
  catalog: ProviderCatalog;
  providerId: string;
  value: string;
  curated: ReturnType<typeof voicesFor>;
  option: CatalogOption | undefined;
  onChange: (voice: string) => void;
}) {
  const [custom, setCustom] = useState(false);
  // A curated id must not keep `custom` on if the provider is swapped underneath it.
  const known = curated.some((voice) => voice.id === value);
  const showCustom = custom || (value !== "" && !known);
  const providerLabel = catalog.providers.find((entry) => entry.id === providerId)?.label ?? providerId;

  if (curated.length === 0) {
    return (
      <label className="flex min-w-0 flex-col gap-1.5">
        <div className="flex items-center gap-1.5">
          <span className="text-[12.5px] font-medium text-foreground">Voice</span>
          {option?.notes && <Hint>{option.notes}</Hint>}
        </div>
        <input
          type="text"
          value={value}
          placeholder="the provider's voice id"
          spellCheck={false}
          autoComplete="off"
          onChange={(event) => onChange(event.target.value)}
          className="h-9 rounded-sm bg-surface-muted px-3 text-[13px] text-foreground shadow-elev-1 outline-none placeholder:text-faint focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        />
        <p className="text-[11.5px] text-faint">{providerLabel} publishes no curated voice list.</p>
      </label>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <div className="flex items-center gap-1.5">
        <span className="text-[12.5px] font-medium text-foreground">Voice</span>
        {option?.notes && <Hint>{option.notes}</Hint>}
      </div>
      {showCustom ? (
        <div className="flex items-center gap-2">
          <input
            type="text"
            // `defaultValue`, not `value`: this box appears already holding whatever
            // is saved, and steering it is a fresh edit that either commits or is
            // abandoned with the row. A controlled input here would rewrite the
            // profile on every keystroke, including a half-typed voice id.
            defaultValue={value}
            placeholder="voice id"
            spellCheck={false}
            autoComplete="off"
            autoFocus
            onChange={(event) => onChange(event.target.value)}
            className="h-9 min-w-0 flex-1 rounded-sm bg-surface-muted px-3 font-mono text-[12.5px] text-foreground shadow-elev-1 outline-none placeholder:text-faint focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          />
          <button
            type="button"
            onClick={() => {
              setCustom(false);
              // Returning to the list means picking from it, so the default voice is
              // the honest landing spot rather than leaving an unsaved id in place.
              onChange(curated.find((entry) => entry.default)?.id ?? curated[0].id);
            }}
            className="shrink-0 text-[12px] text-soft underline-offset-2 transition-colors hover:text-accent hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            Use the list
          </button>
        </div>
      ) : (
        <Dropdown
          label="Voice"
          value={value}
          onChange={(next) => {
            if (next === CUSTOM) {
              setCustom(true);
              return;
            }
            onChange(next);
          }}
          placeholder="The provider's default"
          options={[
            ...curated.map((voice) => ({
              value: voice.id,
              label: voice.label,
              hint: voice.notes || (voice.languages.length > 0 ? voice.languages.join(", ") : undefined),
            })),
            // Not a voice: a way out of the list. The sentinel cannot collide with a
            // real id because a UUID or a provider slug will not equal this.
            { value: CUSTOM, label: "Another voice id…" },
          ]}
        />
      )}
    </div>
  );
}

/**
 * The audition button.
 *
 * It speaks the *draft*, not the saved profile, because the change being judged is
 * usually not saved yet. Every failure is reported in place rather than as a toast:
 * a toast scrolls away, and "the preview did not work" is only useful while you are
 * still looking at the settings that caused it.
 */
function PreviewBar({ stage }: { stage: ModelRef & { voice?: string } }) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [state, setState] = useState<"idle" | "working" | "played" | "failed">("idle");
  const [message, setMessage] = useState<string>("");
  const [preview, setPreview] = useState<VoicePreview | null>(null);
  const [phrase, setPhrase] = useState("");

  // A preview that is still loading when the settings change is a preview of
  // something that is no longer on screen. Dropping it is the only honest option:
  // playing it would answer a question the user has already moved on from.
  const signature = JSON.stringify(stage);
  useEffect(() => {
    setPreview(null);
    setState("idle");
    setMessage("");
  }, [signature]);

  // The element is created on demand and released on unmount rather than parked in
  // the DOM, so a settings dialog that is opened and closed fifty times does not
  // leave fifty decoders behind.
  useEffect(
    () => () => {
      audioRef.current?.pause();
      audioRef.current = null;
    },
    [],
  );

  const play = useCallback(async () => {
    setState("working");
    setMessage("");
    try {
      const result = await previewVoice(
        {
          provider: stage.provider,
          model: stage.model,
          ...(stage.voice ? { voice: stage.voice } : {}),
          options: stage.options ?? {},
        },
        // An empty box means the script's own line, which is the one that shows
        // emotion as well as pace. Passing "" would be read as "say nothing".
        phrase.trim() || undefined,
      );
      const audio = new Audio(result.dataUri);
      audioRef.current = audio;
      setPreview(result);
      await audio.play();
      setState("played");
      setMessage(`${result.durationSeconds.toFixed(1)}s of ${result.sampleRate / 1000} kHz audio`);
    } catch (cause) {
      setState("failed");
      setMessage(cause instanceof Error ? cause.message : "The preview could not be created.");
    }
  }, [stage, phrase]);

  return (
    <div className="flex flex-col gap-2 rounded-md bg-surface p-3 shadow-elev-1">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void play()}
          disabled={state === "working"}
          className={
            "rounded-sm bg-accent px-3.5 py-2 text-[13px] font-medium text-accent-foreground shadow-elev-1 " +
            "transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 " +
            "focus-visible:outline-accent disabled:opacity-50"
          }
        >
          {state === "working" ? "Synthesizing…" : "Hear it"}
        </button>
        {state === "played" && preview && (
          <button
            type="button"
            onClick={() => void play()}
            className="text-[12.5px] text-soft underline-offset-2 transition-colors hover:text-accent hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            Again
          </button>
        )}
        {/*
          A line of your own. It is the only way to hear how a *name* sounds — "how
          do you pronounce Lumine" is a question the fixed sample cannot answer, and
          it costs the user nothing to type.
        */}
        <input
          type="text"
          value={phrase}
          placeholder="…or type a line to hear"
          spellCheck={false}
          onChange={(event) => setPhrase(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void play();
            }
          }}
          className="h-9 min-w-0 flex-1 rounded-sm bg-surface-muted px-3 text-[13px] text-foreground shadow-elev-1 outline-none placeholder:text-faint focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        />
        <Hint label="What this does">
          Speaks one short phrase with the settings above, including changes you have not saved, and plays it
          here. It goes through the same synthesizer the session will use, and costs one short request. It does
          not start a voice session, and nothing is sent to the room.
        </Hint>
      </div>
      {message && (
        <p
          className={
            "text-[11.5px] leading-relaxed " + (state === "failed" ? "text-danger" : "text-faint")
          }
        >
          {state === "failed" ? message : `Played ${message} of audio.`}
        </p>
      )}
    </div>
  );
}

/**
 * Words for the two ends of a slider.
 *
 * "0.6" and "2.0" are bounds; "unhurried" and "hurried" are a judgement. A voice
 * turn wants the second kind, and a person who has never heard a synthesiser talk
 * has no idea whether 1.3 is fast. Only the settings whose range means something
 * get them — a temperature slider's ends are not worth words.
 */
function endsFor(option: CatalogOption): [string, string] | undefined {
  if (option.name === "speed") return ["Unhurried", "Hurried"];
  if (option.name === "volume") return ["Softer", "Louder"];
  if (option.name === "top_p") return ["Focused", "Varied"];
  return undefined;
}

function placeholderFor(option: CatalogOption): string {
  if (option.name === "language") return "en-US";
  return "provider default";
}

function title(name: string): string {
  const spaced = name.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** Exported for the catalog-driven tests, which assert the ranges they expect. */
export { endsFor as voiceControlEnds };
