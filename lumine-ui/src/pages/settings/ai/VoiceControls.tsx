import { useCallback, useEffect, useRef, useState } from "react";

import {
  findModel,
  optionInapplicableReason,
  optionTitle,
  voicesFor,
  type CatalogOption,
  type CatalogVoice,
  type ModelRef,
  type ProviderCatalog,
  type SavedVoice,
} from "../../../features/settings/aiConfigTypes";
import { previewVoice, type VoicePreview } from "../../../features/settings/aiConfigClient";
import { Dropdown } from "../../../components/ui/dropdown";
import { Knob } from "../../../components/ui/knob";
import { Hint } from "../../../components/ui/hint";
import { Disclosure } from "../../../components/ui/disclosure";
import { Switch } from "../../../components/ui/switch";

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
 * The knobs, their ranges, their end labels and their grouping all come from the
 * catalog, which took them from each plugin's own signature. Cartesia sonic-3
 * gets speed, volume and a 59-value emotion list; Google's TTS gets speed only,
 * because that is all its plugin accepts. Adding a knob is a line in
 * `agent/settings/providers.py`, not a branch in this file.
 *
 * ## Three groups, decided by the catalog
 *
 * The section splits into **voice**, **delivery** and **advanced** using only
 * `advanced` — the flag the catalog sets per option, because only the catalog
 * knows that a token cap is uninteresting next to a voice while a sample rate is
 * not. Nothing is sorted by name or by guess.
 *
 * ## A setting the model ignores says so
 *
 * `optionInapplicableReason` repeats a fact only the catalog has: Cartesia
 * writes speed and emotion into the request on the sonic-3 family and drops them
 * everywhere else. A slider that turns and changes nothing is worse than no
 * slider, because the user cannot tell "this did nothing" from "I did not hear
 * it". The value stays editable — someone comparing models should find their
 * speed waiting when they switch back.
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
  /**
   * Voices the user kept, newest first as saved.
   *
   * Empty when the host has no document to keep them in. The picker reads it
   * either way, so a host that omits it gets a working voice picker and no save
   * affordance — rather than a save button that does nothing.
   */
  savedVoices?: SavedVoice[];
  /** Replaces the whole saved list. Add and remove are the caller's mutation. */
  onSavedVoicesChange?: (voices: SavedVoice[]) => void;
};

export function VoiceControls({
  catalog,
  stage,
  onChange,
  savedVoices,
  onSavedVoicesChange,
}: VoiceControlsProps) {
  const model = findModel(catalog, stage.provider, stage.model, "tts");
  const options = stage.options ?? {};
  const saved = savedVoices ?? [];

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

  // `voice` is drawn as its own section rather than as one row among the options,
  // because a provider's voice list is the one setting whose values are worth a
  // list of their own.
  const voiceOption = model.options.find((option) => option.name === "voice");
  const drawable = model.options.filter((option) => !option.hidden && option.name !== "voice");
  const delivery = drawable.filter((option) => !option.advanced);
  const advanced = drawable.filter((option) => option.advanced);

  const knobs = delivery.filter((option) => isKnob(option));
  const choices = delivery.filter((option) => isChoice(option));
  const free = delivery.filter((option) => !isKnob(option) && !isChoice(option) && !isSwitch(option));

  // A disclosure that silently holds a value somebody set is a value they cannot
  // find again, so it counts. Only values that are actually present count.
  const advancedSet = advanced.filter((option) => (options[option.name] ?? "") !== "").length;

  return (
    <div className="voice-controls flex flex-col gap-4.5 min-w-0">
      <Section
        title="Voice"
        blurb={
          voiceOption?.notes ??
          "Which voice speaks the reply."
        }
      >
        <VoicePicker
          catalog={catalog}
          providerId={stage.provider}
          value={stage.voice ?? ""}
          curated={voicesFor(catalog, stage.provider)}
          saved={saved}
          onChange={setVoice}
          onSavedVoicesChange={onSavedVoicesChange}
        />
      </Section>

      {delivery.length > 0 && (
        <Section
          title="Delivery"
          blurb="How the words come out. Everything here is judged by ear, so there is a preview at the bottom."
        >
          {knobs.length > 0 && (
            <div className="voice-grid">
              {knobs.map((option) => (
                <Gated key={option.name} option={option} modelId={model.id}>
                  <Knob
                    name={option.name}
                    value={options[option.name] ?? ""}
                    onChange={(value) => setOption(option.name, value)}
                    minimum={option.minimum!}
                    maximum={option.maximum!}
                    step={option.step ?? 0.05}
                    ends={endsFor(option)}
                    help={option.notes}
                  />
                </Gated>
              ))}
            </div>
          )}

          {choices.map((option) => (
            <Gated key={option.name} option={option} modelId={model.id}>
              <div className="field">
                <div className="field-row flex gap-2" style={{ alignItems: "center" }}>
                  <span className="field-label text-soft text-[12px]">{optionTitle(option.name)}</span>
                  {option.notes && <Hint>{option.notes}</Hint>}
                </div>
                <Dropdown
                  label={optionTitle(option.name)}
                  value={options[option.name] ?? ""}
                  onChange={(value) => setOption(option.name, value)}
                  placeholder={placeholderFor(option)}
                  options={option.values.map((value) => ({ value, label: value }))}
                  // An enumerated id is worth reaching for by hand: a locale, a
                  // sample rate the plugin added after this build.
                  editable="Enter a value"
                />
              </div>
            </Gated>
          ))}

          {free.map((option) => (
            <Gated key={option.name} option={option} modelId={model.id}>
              <label className="field" htmlFor={`voice-opt-${option.name}`}>
                <span className="field-label text-soft text-[12px]">{optionTitle(option.name)}</span>
                <input
                  id={`voice-opt-${option.name}`}
                  type="text"
                  value={options[option.name] ?? ""}
                  placeholder={placeholderFor(option)}
                  spellCheck={false}
                  autoComplete="off"
                  onChange={(event) => setOption(option.name, event.target.value.trim())}
                />
                {option.notes && <span className="field-hint text-faint text-[11.5px] leading-[1.5]">{option.notes}</span>}
              </label>
            </Gated>
          ))}
        </Section>
      )}

      {advanced.length > 0 && (
        <Disclosure label="Advanced" count={advancedSet}>
          <div className="voice-advanced flex flex-col gap-4">
            {advanced.map((option) =>
              isSwitch(option) ? (
                <Gated key={option.name} option={option} modelId={model.id}>
                  <Switch
                    label={optionTitle(option.name)}
                    value={options[option.name] ?? ""}
                    fallback={option.default === true || String(option.default).toLowerCase() === "true"}
                    onChange={(value) => setOption(option.name, value)}
                    help={option.notes}
                  />
                </Gated>
              ) : isKnob(option) ? (
                <Gated key={option.name} option={option} modelId={model.id}>
                  <Knob
                    name={option.name}
                    value={options[option.name] ?? ""}
                    onChange={(value) => setOption(option.name, value)}
                    minimum={option.minimum!}
                    maximum={option.maximum!}
                    step={option.step ?? 0.05}
                    ends={endsFor(option)}
                    help={option.notes}
                  />
                </Gated>
              ) : (
                <OptionField
                  key={option.name}
                  option={option}
                  modelId={model.id}
                  value={options[option.name] ?? ""}
                  onChange={(value) => setOption(option.name, value)}
                />
              ),
            )}
          </div>
        </Disclosure>
      )}

      <PreviewBar stage={stage} />
    </div>
  );
}

/* ------------------------------------------------------------------------- */
/* Structure                                                                  */
/* ------------------------------------------------------------------------- */

/**
 * A labelled group inside the stage's own recessed panel.
 *
 * The parent already framed this content — `.nested-fields` recesses it below
 * the stage chooser — so a section here gets an overline and a hairline rather
 * than the page-level `.settings-block`'s 24px of air. Boxing what is already
 * boxed is the settings screen's most common way of looking busy.
 */
function Section({
  title,
  blurb,
  children,
}: {
  title: string;
  blurb?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="voice-section flex flex-col gap-2.5 min-w-0">
      <header className="voice-section-head flex flex-col gap-1">
        <h3>{title}</h3>
        {blurb && <p>{blurb}</p>}
      </header>
      {children}
    </section>
  );
}

/**
 * A control the selected model will not act on, and the reason.
 *
 * The control stays live. Turning a slider and watching nothing happen is the
 * failure; dimming the control so it cannot be touched would swap that for a
 * second failure, where the user cannot put a value back before switching model
 * again. So the change is the note, not the widget.
 */
function Gated({
  option,
  modelId,
  children,
}: {
  option: CatalogOption;
  modelId: string;
  children: React.ReactNode;
}) {
  const reason = optionInapplicableReason(option, modelId);
  if (!reason) return <>{children}</>;
  return (
    <div className="voice-gated flex flex-col gap-1.5 min-w-0">
      {children}
      <p className="voice-gate flex items-baseline gap-[7px] max-w-[54ch] m-0 text-soft text-[11.5px] leading-[1.5]">{reason}</p>
    </div>
  );
}

/** One advanced option in the shape it wants, without a branch per widget. */
function OptionField({
  option,
  modelId,
  value,
  onChange,
}: {
  option: CatalogOption;
  modelId: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <Gated option={option} modelId={modelId}>
      <label className="field" htmlFor={`voice-adv-${option.name}`}>
        <div className="field-row flex gap-2" style={{ alignItems: "center" }}>
          <span className="field-label text-soft text-[12px]">{optionTitle(option.name)}</span>
          {option.notes && <Hint>{option.notes}</Hint>}
        </div>
        {isChoice(option) ? (
          <Dropdown
            label={optionTitle(option.name)}
            value={value}
            onChange={onChange}
            placeholder="Provider default"
            options={option.values.map((candidate) => ({ value: candidate, label: candidate }))}
            editable="Enter a value"
          />
        ) : (
          <input
            id={`voice-adv-${option.name}`}
            type="text"
            value={value}
            placeholder={placeholderFor(option)}
            spellCheck={false}
            autoComplete="off"
            onChange={(event) => onChange(event.target.value.trim())}
          />
        )}
        {option.notes && <span className="field-hint text-faint text-[11.5px] leading-[1.5]">{option.notes}</span>}
      </label>
    </Gated>
  );
}

/* ------------------------------------------------------------------------- */
/* Voice                                                                      */
/* ------------------------------------------------------------------------- */

/**
 * The sentinel row in the voice dropdown that reveals the free-text field.
 *
 * A leading space, because a voice id is a provider slug or a UUID and a leading
 * space is legal in neither — so this cannot collide with a real one. It is not
 * `Dropdown`'s own `"custom-value"`, which is used inside that component for its
 * `editable` row; the two sentinels are in different value spaces only because
 * this one never reaches `onChange` (the picker intercepts it), but they stay
 * distinct so neither can be mistaken for the other in a profile on disk.
 */
const CUSTOM = " custom";

/**
 * A plain text field in this panel.
 *
 * The same string `Dropdown` uses when it swaps its own trigger for a field, so a
 * box you type a name into and a box you type an id into are the same box — the
 * settings screen already had one raised-surface text field, and two is how a
 * form starts reading as three conventions.
 *
 * `.field input` is deliberately not used here: it is monospaced, because almost
 * every provider option *is* an identifier or a locale, and a person's name set
 * in a mono face reads as an id.
 */
const TEXT_FIELD =
  "h-9 min-w-0 flex-1 rounded-sm bg-surface-muted px-3 text-[13px] text-foreground shadow-elev-1 outline-none " +
  "placeholder:text-faint focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

/**
 * The voice chooser: one list of everything the user can pick from, plus a way
 * in for an id nobody has written down.
 *
 * The catalog's list is a shortcut and not a limit. Providers publish far more
 * voices than a catalog can carry, and a cloned voice has no name anyone could
 * have written down — so a custom id still has to be typeable, and it now also
 * has somewhere to *stay*. Without that, "another voice id" is a value typed
 * once and lost the next time the dropdown is opened, which makes a saved voice
 * a label rather than a feature.
 */
function VoicePicker({
  catalog,
  providerId,
  value,
  curated,
  saved,
  onChange,
  onSavedVoicesChange,
}: {
  catalog: ProviderCatalog;
  providerId: string;
  value: string;
  curated: CatalogVoice[];
  saved: SavedVoice[];
  onChange: (voice: string) => void;
  onSavedVoicesChange?: (voices: SavedVoice[]) => void;
}) {
  const [custom, setCustom] = useState(false);
  const [draftLabel, setDraftLabel] = useState("");
  const providerLabel = catalog.providers.find((entry) => entry.id === providerId)?.label ?? providerId;

  // The catalog's voices and the user's own, in that order. The saved ones are
  // typed as catalog voices so there is one list to draw and one dropdown to
  // open — a second chooser beside the first would be the "wall of cards"
  // problem in a smaller room.
  const mine: CatalogVoice[] = saved.map((entry) => ({
    id: entry.id,
    label: entry.label,
    languages: entry.language ? [entry.language] : [],
    default: false,
    notes: entry.language ? `Saved by you · ${entry.language}` : "Saved by you",
  }));
  const inCatalog = new Set(curated.map((voice) => voice.id));
  const voices = [...curated, ...mine.filter((voice) => !inCatalog.has(voice.id))];

  // A curated or saved id must not keep `custom` on if the provider is swapped
  // underneath it.
  const known = voices.some((voice) => voice.id === value);
  const showCustom = custom || (value !== "" && !known);
  const canSave = onSavedVoicesChange !== undefined && value !== "" && !known;

  const save = () => {
    if (!onSavedVoicesChange || value === "") return;
    onSavedVoicesChange([
      // Newest first, and the id is the identity so saving twice replaces rather
      // than duplicating — the same voice renamed should not appear twice.
      { id: value, label: draftLabel.trim() || value, addedAt: new Date().toISOString() },
      ...saved.filter((entry) => entry.id !== value),
    ]);
    setDraftLabel("");
    setCustom(false);
  };

  if (curated.length === 0 && saved.length === 0) {
    return (
      <div className="flex min-w-0 flex-col gap-1.5">
        <input
          type="text"
          value={value}
          placeholder="the provider's voice id"
          spellCheck={false}
          autoComplete="off"
          onChange={(event) => onChange(event.target.value)}
          className={`${TEXT_FIELD} w-full flex-none font-mono`}
        />
        <p className="field-hint text-faint text-[11.5px] leading-[1.5]">{providerLabel} publishes no curated voice list.</p>
      </div>
    );
  }

  // No visible label of its own. This whole block sits under the section
  // heading, and `Dropdown`'s `label` is what names it for a screen reader — a
  // second "Voice" over a dropdown in a section already called Voice is the
  // settings screen's habit of restating itself, and the restatement is the
  // line people stop reading.
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="flex min-w-0 flex-col gap-2">
        {showCustom ? (
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={value}
                placeholder="voice id"
                spellCheck={false}
                autoComplete="off"
                autoFocus
                // The box arrives already holding the voice that is selected, and
                // the first keystroke of a *new* id should replace it rather than
                // append to it — otherwise typing into an id produces a longer id
                // and the save row below offers to keep it.
                onFocus={(event) => event.currentTarget.select()}
                onChange={(event) => onChange(event.target.value)}
                className={`${TEXT_FIELD} font-mono`}
              />
              <button
                type="button"
                onClick={() => {
                  setCustom(false);
                  // Returning to the list means picking from it, so the default voice
                  // is the honest landing spot rather than leaving an unsaved id in
                  // place. Merged, not curated: a provider that publishes no voices
                  // of its own still has the ones kept here, and `curated[0]` on an
                  // empty list is an exception thrown by a button doing a kindness.
                  onChange(voices.find((entry) => entry.default)?.id ?? voices[0]?.id ?? "");
                }}
                className="settings-quiet py-[9px] px-1.5 text-soft bg-transparent text-[12px] cursor-pointer"
              >
                Use the list
              </button>
            </div>

            {/*
              The point of the whole feature: a name, then keep it. Two fields
              rather than a dialog, because the id is already on screen above and
              a modal for one text field is a modal the user has to read.
            */}
            {canSave && (
              <div className="voice-save flex items-center gap-2">
                <input
                  type="text"
                  value={draftLabel}
                  placeholder="Name this voice, e.g. “Morning”"
                  spellCheck={false}
                  onChange={(event) => setDraftLabel(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      save();
                    }
                  }}
                  className={TEXT_FIELD}
                />
                <button type="button" onClick={save} className="settings-primary">
                  Save voice
                </button>
              </div>
            )}
          </div>
        ) : (
          <Dropdown
            label="Which voice"
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
              ...voices.map((voice) => ({ value: voice.id, label: voice.label, hint: voice.notes || undefined })),
              // Not a voice: a way out of the list.
              { value: CUSTOM, label: "Another voice id…" },
            ]}
          />
        )}
      </div>

      {saved.length > 0 && (
        <SavedVoices
          saved={saved}
          current={value}
          canRemove={onSavedVoicesChange !== undefined}
          onRemove={(id) => onSavedVoicesChange?.(saved.filter((entry) => entry.id !== id))}
        />
      )}
    </div>
  );
}

/**
 * The voices the user kept, as a list rather than as cards.
 *
 * Selected in the dropdown above; managed here. Splitting the two is what keeps
 * this from becoming a second picker — nothing in this list chooses a voice, so
 * it is rows with a remove affordance and no radio behaviour to guess at.
 */
function SavedVoices({
  saved,
  current,
  canRemove,
  onRemove,
}: {
  saved: SavedVoice[];
  current: string;
  canRemove: boolean;
  onRemove: (id: string) => void;
}) {
  return (
    <div className="voice-saved flex flex-col gap-2">
      <p className="voice-saved-head m-0 text-faint text-[10.5px] font-semibold tracking-[0.09em] uppercase">Saved by you</p>
      <ul>
        {saved.map((entry) => (
          <li key={entry.id} className={entry.id === current ? "is-current" : undefined}>
            <span className="voice-saved-name">{entry.label}</span>
            <code className="voice-saved-id flex-1 min-w-0 overflow-hidden text-faint font-mono text-[11px] text-ellipsis whitespace-nowrap">{entry.id}</code>
            {entry.id === current && <span className="voice-saved-badge">in use</span>}
            {canRemove && (
              <button
                type="button"
                onClick={() => onRemove(entry.id)}
                aria-label={`Remove ${entry.label} from the saved voices`}
                className="voice-saved-remove"
              >
                Remove
              </button>
            )}
          </li>
        ))}
      </ul>
      <p className="field-hint text-faint text-[11.5px] leading-[1.5]">
        Kept with your settings, so an id you pasted once is there next time. Removing it does not change the
        voice Lumine is using right now.
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------------- */
/* Preview                                                                    */
/* ------------------------------------------------------------------------- */

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
          className={TEXT_FIELD}
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

/* ------------------------------------------------------------------------- */
/* Shape of an option                                                         */
/* ------------------------------------------------------------------------- */

function isKnob(option: CatalogOption): boolean {
  return option.control === "slider" && option.minimum !== null && option.maximum !== null;
}

function isChoice(option: CatalogOption): boolean {
  return option.control === "select" || option.control === "combobox" || option.values.length > 0;
}

function isSwitch(option: CatalogOption): boolean {
  return option.control === "switch";
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
  if (option.name === "volume") return ["Softer", "Loud"];
  if (option.name === "top_p") return ["Focused", "Varied"];
  return undefined;
}

function placeholderFor(option: CatalogOption): string {
  if (option.name === "language") return "en-US";
  if (option.name === "pronunciation_dict_id") return "dictionary id";
  return "Provider default";
}

/** Exported for the catalog-driven tests, which assert the ranges they expect. */
export { endsFor as voiceControlEnds };
