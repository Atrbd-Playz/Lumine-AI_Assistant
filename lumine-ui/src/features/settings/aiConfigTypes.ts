/**
 * TypeScript mirror of Lumine's AI configuration contract.
 *
 * These types describe the JSON that crosses three boundaries:
 *
 *   1. the provider catalog  - `agent/providers.py`  -> `agent/provider_catalog.py` -> Tauri
 *   2. the config document   - a Tauri store file, read by Rust and by the worker
 *   3. validation output     - `agent/validation.py` -> Tauri -> this file
 *
 * The Python modules remain the source of truth. Nothing here decides whether a
 * configuration is valid: the UI renders diagnostics that the worker produced, so
 * the settings screen can never promise a combination the runtime will refuse.
 *
 * Keep this file in step with `agent/providers.py`, `agent/validation.py`, and the
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

export type ConfigDocument = {
  version: number;
  activeProfileId: string;
  providers: Record<string, ProviderEntry>;
  profiles: VoiceProfile[];
  agent?: AgentSettings;
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
