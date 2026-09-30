/**
 * TypeScript mirror of Lumine's AI configuration contract.
 *
 * These types describe the JSON that crosses three boundaries:
 *
 *   1. the provider catalog  - `agent/settings/providers.py`  -> `agent/provider_catalog.py` -> Tauri
 *   2. the config document   - a Tauri store file, read by Rust and by the worker
 *   3. validation output     - `agent/settings/validation.py` -> Tauri -> this file
 *
 * The Python modules remain the source of truth. Nothing here decides whether a
 * configuration is valid: the UI renders diagnostics that the worker produced, so
 * the settings screen can never promise a combination the runtime will refuse.
 *
 * Keep this file in step with `agent/settings/providers.py`, `agent/settings/validation.py`, and the
 * golden example at `agent/tests/fixtures/lumine.config.example.json`.
 */

/** The capability vocabulary. Open-ended on purpose. */
export type Capability = "stt" | "llm" | "tts" | "realtime" | "vad" | "transport";

export type ModelStatus = "available" | "deprecated" | "retired";

/** Realtime-only capability flags. `null` for non-realtime models. */
export type RealtimeModelFlags = {
  nativeAudio: boolean;
  /**
   * Whether LiveKit can drive this model in text-only mode so a separate TTS
   * speaks instead. This is the flag the half-cascade rule keys off.
   */
  textOnlyModality: boolean;
  /**
   * Whether "finish reply" can be honoured provider-side. A realtime model that
   * owns turn detection cannot have interruptions disabled at session level:
   * LiveKit rejects that outright.
   */
  finishResponse: boolean;
  proactivity: boolean;
  affectiveDialog: boolean;
  asyncFunctionCalling: boolean;
};

export type CatalogVoice = {
  id: string;
  label: string;
  languages: string[];
  default: boolean;
  notes: string;
};

/**
 * One tunable setting a model accepts.
 *
 * Declared by the backend rather than hardcoded here, so the settings screen can
 * offer exactly what a model takes. `values` empty means free-form; a non-empty
 * list is a closed set, and offering anything else produces a request the
 * provider rejects — in a voice session, silence.
 */
export type CatalogOption = {
  name: string;
  values: string[];
  /** The object the provider takes this inside, when not a plain argument. */
  nest: string;
  notes: string;
  /**
   * The widget the backend expects: `slider`, `select`, `combobox`, `switch`,
   * `number`, `voice`, or `text`. Declared rather than guessed, so a 59-value
   * list renders as something you can search instead of 59 rows.
   */
  control: OptionControl;
  /**
   * The provider's own bounds, when it has any. A slider is drawn between these
   * and stops there: the alternative is a free-text box that only tells you the
   * value was rejected after you pressed Save, and in a voice session a rejected
   * setting looks like a muted microphone.
   */
  minimum: number | null;
  maximum: number | null;
  step: number | null;
  /**
   * Whether the value leaves the worker as an integer rather than as a float.
   *
   * The settings screen never sees the difference — it writes strings either
   * way — but this is also read by diagnostics, and an option the wire type of
   * which is not stated is one nobody can check. `sample_rate` sent as
   * `"24000"` is a request Cartesia refuses; sent as `24000.0` it is a request
   * Cartesia refuses differently.
   */
  integer: boolean;
  /**
   * Model ids this option does anything on. Empty means every model that
   * declares it.
   *
   * The catalog is the only place that knows: Cartesia writes `speed`,
   * `emotion` and `volume` into the request only on the sonic-3 family, and
   * silently drops them everywhere else. Without this the screen drew a slider
   * against `sonic-2` that moved and a voice that did not change. The dimmed
   * row that says so is worth more than the slider itself.
   */
  models: string[];
  /**
   * Whether this sits behind the "Advanced" disclosure rather than on the first
   * screen of its stage.
   *
   * Declared by the backend rather than guessed from the name here, because the
   * same option is ordinary on one model and meaningless on another. A token cap
   * is worth showing to somebody who has hit one; it is not worth showing to
   * somebody who has not, and it is not worth showing at all next to voice and
   * speed.
   */
  advanced: boolean;
  /**
   * A value Lumine sends when the profile sets none.
   *
   * The provider's own default is not always expressible as an absent argument.
   * A local model server has no authentication at all, but the OpenAI client
   * refuses to construct without a key -- so the worker sends a literal string
   * the server ignores. This is where that comes from, which is why it is
   * declared in the catalog next to the option instead of being invented in the
   * factory: one place to read when a provider is added that needs it.
   */
  default: string | number | boolean | null;
  /**
   * Sent, but not drawn.
   *
   * For a default nobody can usefully change. A key field on a provider that has
   * no key reads as a setup wizard that cannot be completed.
   */
  hidden: boolean;
};

export type OptionControl =
  | "slider"
  | "select"
  | "combobox"
  | "switch"
  | "number"
  | "voice"
  | "text";

export type CatalogModel = {
  id: string;
  label: string;
  capability: Capability;
  status: ModelStatus;
  default: boolean;
  /** Successor model id when this one is deprecated. */
  replaces: string | null;
  notes: string;
  /** The backend transcribes whole segments, so a VAD must delimit them. */
  requiresVad: boolean;
  /**
   * What Lumine can hand this model, on the path it is reached by.
   *
   * Not the same as what the model could accept in principle: Google's Gemini 3
   * Flash takes images through the Generative API, but the LiveKit LLM stage
   * sends it a chat history of strings and has nowhere to put a frame. The
   * capability screen reports these, and the camera control reads them — a button
   * that turns on a track nothing consumes is the one control in the app that
   * would be lying.
   */
  inputModalities: string[];
  /**
   * Thinking levels this model accepts. Empty means it is not configured by
   * level and none is sent. The worker must never invent one: Google rejects an
   * unsupported level with a 400, which in a voice session means no audio.
   */
  thinkingLevels: string[];
  /** Every setting this model accepts, declared by the backend. */
  options: CatalogOption[];
  realtime: RealtimeModelFlags | null;
  voices: string[];
};

/**
 * One field of a provider's credential.
 *
 * Most providers have one. LiveKit has three -- a server address, an API key and
 * an API secret -- and they are not interchangeable, which is why they are three
 * fields here rather than one field that received the same value three times.
 *
 * `env` is the environment variable the value belongs in, and it doubles as the
 * keyring slot name, so the field drawn and the variable the worker reads cannot
 * drift apart. It is documentation, never a value.
 */
export type CatalogKeySlot = {
  env: string;
  /** A name a person can act on: "Server URL", not "LIVEKIT_URL". */
  label: string;
  /**
   * How to draw the field. `url` holds a hostname and is shown in the clear so a
   * typo is visible; `secret` is masked; `text` is a public identifier. A value
   * named like a credential is always `secret` -- the Python catalog refuses to
   * declare otherwise.
   */
  kind: "secret" | "text" | "url";
  /** Optional one-line guidance specific to this field. */
  help: string;
};

export type CatalogProvider = {
  id: string;
  label: string;
  requiresKey: boolean;
  /** Environment variable *names* the worker reads. Never a value. */
  keyEnv: string[];
  /**
   * One entry per `keyEnv` variable, in the same order. Empty when the provider
   * needs no credential. The settings screen renders exactly this list, so it
   * never has to know which providers have several values.
   */
  keySlots: CatalogKeySlot[];
  setupUrl: string;
  local: boolean;
  notes: string;
  capabilities: Capability[];
  /**
   * Capabilities this provider is Lumine's own default for. New stages are
   * seeded from these, so switching stack type lands on the stack the app
   * already runs rather than on whichever provider sorts first.
   */
  preferredFor: Capability[];
  /**
   * How to prove this provider's credential works, or `null` when there is no
   * cheap authenticated request. Absent is honest: the page then says the key is
   * stored without claiming it works.
   */
  probe: {
    method: string;
    url: string;
    /**
     * `"header"` puts one value in one header. `"livekit_token"` mints a JWT from
     * several values, which is why LiveKit's credential is tested as a unit and
     * cannot be reduced to a single header.
     */
    authKind: string;
    /** Rooted path appended to the server address, for the token kind only. */
    tokenPath: string;
    authHeader: string;
    authPrefix: string;
    /** Fixed headers the provider needs, such as an API version. */
    headers: Record<string, string>;
    /** Statuses that mean the credential itself is not usable. */
    invalidStatus: number[];
    costs: string;
  } | null;
  models: CatalogModel[];
  voices: CatalogVoice[];
};

export type ProviderCatalog = {
  /** Bumped when the catalog shape changes; refuse to render an unknown one. */
  version: number;
  capabilities: Capability[];
  /** Ids reserved for future local/offline providers. */
  reservedProviderIds: string[];
  providers: CatalogProvider[];
};

// ---------------------------------------------------------------------------
// Configuration document
// ---------------------------------------------------------------------------

export const CONFIG_VERSION = 1;

export type InterruptionMode = "barge_in" | "finish_response";

export type ProfileKind = "pipeline" | "realtime";

export type OutputMode = "model_voice" | "custom_tts";

/** A `{provider, model}` pair for one capability, plus that model's own settings. */
export type ModelRef = {
  provider: string;
  model: string;
  language?: string;
  /**
   * Settings this model accepts, keyed by the name the backend declares.
   *
   * Left out entirely when empty, so an untouched stage sends nothing and each
   * provider applies its own default. A value the model does not accept is
   * refused by validation rather than reaching the provider.
   */
  options?: Record<string, string>;
};

export type PipelineProfile = {
  stt: ModelRef;
  llm: ModelRef;
  tts: ModelRef & { voice?: string };
  /** Optional: some STT backends cannot segment speech without a VAD. */
  vad?: ModelRef;
  turnHandling?: { interruptionMode?: InterruptionMode };
};

export type RealtimeOutput =
  | { mode: "model_voice" }
  | { mode: "custom_tts"; tts: ModelRef; voice?: string };

export type RealtimeProfile = {
  provider: string;
  model: string;
  /** Only meaningful for `model_voice` output. */
  voice?: string;
  /**
   * Settings the realtime model accepts, by option name.
   *
   * Present in the type because the worker already reads it — `config_store`
   * resolves a realtime stage's options and `validation` checks them against the
   * model — and absent from this file because the screen that edits them was never
   * written. The thinking level, the one control a realtime profile has, was
   * therefore unreachable from the settings and had to be set in `agent/.env`.
   */
  options?: Record<string, string>;
  output: RealtimeOutput;
  turnHandling?: { interruptionMode?: InterruptionMode };
};

/** Capability-gated extras. Setting an unsupported one warns rather than blocks. */
export type RealtimeOptions = {
  proactivity?: boolean;
  affectiveDialog?: boolean;
  asyncFunctionCalling?: boolean;
};

export type VoiceProfile = {
  id: string;
  name: string;
  kind: ProfileKind;
  pipeline?: PipelineProfile;
  realtime?: RealtimeProfile;
  options?: RealtimeOptions;
};

/**
 * A provider entry. `keyRef` points at stored credential material; it is never
 * the credential itself, so this document is safe to keep unencrypted.
 */
export type ProviderEntry = {
  enabled: boolean;
  keyRef: string | null;
};

export type AgentSettings = {
  disabledTools?: string[];
  enableAppLaunch?: boolean;
};

/**
 * A voice the user found and chose to keep.
 *
 * The picker can only offer what the catalog declares, plus whatever somebody
 * pastes in. Without somewhere to put it, a pasted voice id is a value typed
 * once and lost the next time the dropdown is opened — which makes "add your
 * own voice" a label rather than a feature.
 *
 * It lives in the config document rather than in local storage so it travels
 * with the settings the worker already reads: the same file that says which TTS
 * stage to build says which voices that stage may be given. The worker itself
 * never looks at this array — it reads the id off the profile — which is why
 * nothing in Python had to change for it to exist.
 */
export type SavedVoice = {
  /** The provider's own voice id. This is what reaches synthesis. */
  id: string;
  /**
   * A name a person chose. A list of ids is a list of opaque strings; a list of
   * names is a list of choices, and the id is still what gets sent.
   */
  label: string;
  /** Language tag, when the person knows it. Used for grouping, never required. */
  language?: string;
  /** ISO timestamp of when it was added, so recent voices can come first. */
  addedAt: string;
};

export type ConfigDocument = {
  version: number;
  activeProfileId: string;
  providers: Record<string, ProviderEntry>;
  profiles: VoiceProfile[];
  agent?: AgentSettings;
  /**
   * Voices the user added themselves. Absent or empty means the picker offers
   * only what the catalog declares.
   */
  voices?: SavedVoice[];
};

// ---------------------------------------------------------------------------
// Validation output
// ---------------------------------------------------------------------------

export type DiagnosticSeverity = "error" | "warn" | "info";

export type Diagnostic = {
  severity: DiagnosticSeverity;
  /** Stable machine-readable code, e.g. `realtime.custom_tts_unsupported`. */
  code: string;
  /** Dotted path into the profile, e.g. `realtime.output.mode`. */
  path: string;
  message: string;
  /** Actionable next step, when there is an obvious one. */
  hint: string;
};

/** Activation is blocked only when at least one diagnostic is an error. */
export function hasBlockingDiagnostics(diagnostics: readonly Diagnostic[]): boolean {
  return diagnostics.some((diagnostic) => diagnostic.severity === "error");
}

// ---------------------------------------------------------------------------
// Small catalog helpers (pure; no provider logic beyond lookups)
// ---------------------------------------------------------------------------

export function findProvider(
  catalog: ProviderCatalog,
  providerId: string,
): CatalogProvider | undefined {
  return catalog.providers.find((provider) => provider.id === providerId);
}

/** Models for a provider and capability, with retired models filtered out. */
export function modelsFor(
  catalog: ProviderCatalog,
  providerId: string,
  capability: Capability,
  { includeRetired = false }: { includeRetired?: boolean } = {},
): CatalogModel[] {
  const provider = findProvider(catalog, providerId);
  if (!provider) return [];
  return provider.models.filter(
    (model) =>
      model.capability === capability &&
      (includeRetired || model.status !== "retired"),
  );
}

/** Providers that can actually serve a capability right now. */
export function providersFor(
  catalog: ProviderCatalog,
  capability: Capability,
): CatalogProvider[] {
  return catalog.providers.filter((provider) =>
    provider.models.some(
      (model) => model.capability === capability && model.status !== "retired",
    ),
  );
}

export function findModel(
  catalog: ProviderCatalog,
  providerId: string,
  modelId: string,
  capability?: Capability,
): CatalogModel | undefined {
  const provider = findProvider(catalog, providerId);
  if (!provider) return undefined;
  return provider.models.find(
    (model) =>
      model.id === modelId && (capability === undefined || model.capability === capability),
  );
}

/** Curated voices for a provider. Providers without a catalog expose none. */
export function voicesFor(catalog: ProviderCatalog, providerId: string): CatalogVoice[] {
  return findProvider(catalog, providerId)?.voices ?? [];
}

/**
 * A readable name for a setting, derived from its key.
 *
 * One copy rather than the three it had been: `ModelOptions` called it `label`,
 * `VoiceControls` called it `title`, and `Knob` had a third. They agreed by
 * coincidence, which is the sort of coincidence that stops the first time
 * somebody adds a camelCase option.
 */
export function optionTitle(name: string): string {
  const spaced = name.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/**
 * Whether `option` does anything on `modelId`.
 *
 * An empty `models` means every model that declares the option, which is the
 * case for all but a handful.
 */
export function optionAppliesTo(option: CatalogOption, modelId: string): boolean {
  return option.models.length === 0 || option.models.includes(modelId);
}

/**
 * Why `option` is being ignored on `modelId`, or `null` when it is not.
 *
 * The catalog is the only place that knows this — Cartesia writes `speed` into
 * the request on the sonic-3 family and drops it everywhere else — so the screen
 * can only repeat what it was told. It has to, though: a slider whose turn
 * produces no change is worse than no slider, because the user has no way to
 * tell the difference between "this did nothing" and "I did not hear it".
 *
 * The value stays editable on purpose. Someone who has set a speed, then
 * changed model to compare, and then changes back, should find their speed
 * still there.
 */
export function optionInapplicableReason(option: CatalogOption, modelId: string): string | null {
  if (optionAppliesTo(option, modelId)) return null;
  return `Not sent to ${modelId} — it applies to ${option.models.join(", ")} only.`;
}
