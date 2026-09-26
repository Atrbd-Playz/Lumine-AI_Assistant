import {
  findProvider,
  modelsFor,
  providersFor,
  type Capability,
  type ConfigDocument,
  type InterruptionMode,
  type ModelRef,
  type ProfileKind,
  type ProviderCatalog,
  type VoiceProfile,
} from "./aiConfigTypes";

/**
 * Pure operations on a voice profile.
 *
 * Kept free of React so the tricky part — turning a realtime profile into a
 * pipeline one and back — is a plain function that can be reasoned about and
 * tested without a renderer.
 */

/** The environment label is not a real profile name; it is a provenance marker. */
const ENV_NAME_SUFFIX = " (from agent/.env)";

export function isEnvironmentName(name: string): boolean {
  return name.trim().endsWith(ENV_NAME_SUFFIX);
}

/** The first available model for a capability, preferring each provider's default. */
export function defaultModelRef(
  catalog: ProviderCatalog,
  capability: Capability,
): ModelRef | undefined {
  const capable = providersFor(catalog, capability);
  // The catalog names the provider Lumine actually runs for each capability.
  // Without that, seeding would follow catalog order and quietly move the
  // conversation to whichever vendor happens to sort first.
  const preferred = capable.filter((provider) => provider.preferredFor.includes(capability));
  for (const provider of preferred.length > 0 ? preferred : capable) {
    const models = modelsFor(catalog, provider.id, capability);
    const chosen = models.find((model) => model.default) ?? models[0];
    if (chosen) return { provider: provider.id, model: chosen.id };
  }
  return undefined;
}

/** The default voice a provider or realtime model suggests, if it publishes one. */
export function defaultVoice(catalog: ProviderCatalog, providerId: string, modelId?: string): string | undefined {
  if (modelId) {
    const provider = findProvider(catalog, providerId);
    const model = provider?.models.find((candidate) => candidate.id === modelId);
    if (model?.voices.length) {
      return model.voices[0];
    }
  }
  const voices = findProvider(catalog, providerId)?.voices;
  return voices?.find((voice) => voice.default)?.id ?? voices?.[0]?.id;
}

/**
 * A complete profile of the requested architecture, seeded from the catalog.
 *
 * Split out of `convertProfileKind` so "make me a profile of this kind" has one
 * implementation. Passing an already-correct `kind` to `convertProfileKind` would
 * return its input untouched, which silently produces an empty profile.
 */
function seedProfile(
  catalog: ProviderCatalog,
  kind: ProfileKind,
  name: string,
  id: string,
  interruptionMode: InterruptionMode,
): VoiceProfile | undefined {
  if (kind === "realtime") {
    const realtime = defaultModelRef(catalog, "realtime");
    if (!realtime) return undefined;
    return {
      id,
      name,
      kind: "realtime",
      realtime: {
        provider: realtime.provider,
        model: realtime.model,
        voice: defaultVoice(catalog, realtime.provider, realtime.model),
        output: { mode: "model_voice" },
        turnHandling: { interruptionMode },
      },
    };
  }

  const stt = defaultModelRef(catalog, "stt");
  const llm = defaultModelRef(catalog, "llm");
  const tts = defaultModelRef(catalog, "tts");
  if (!stt || !llm || !tts) return undefined;

  const vad = defaultModelRef(catalog, "vad");
  return {
    id,
    name,
    kind: "pipeline",
    pipeline: {
      stt,
      llm,
      tts: { ...tts, voice: defaultVoice(catalog, tts.provider, tts.model) },
      ...(vad ? { vad } : {}),
      turnHandling: { interruptionMode },
    },
  };
}

/**
 * Switch a profile between the pipeline and realtime architectures.
 *
 * The stages of the new architecture are seeded from catalog defaults, because a
 * profile has to be complete before the worker will accept it. Anything the user
 * had chosen in the *other* architecture is not carried over: there is nothing
 * sensible to map a Gemini Live model onto a Groq STT call.
 *
 * A user-chosen name survives the switch; the environment-derived one does not,
 * because it names an architecture rather than a profile.
 */
export function convertProfileKind(
  catalog: ProviderCatalog,
  profile: VoiceProfile,
  kind: ProfileKind,
): VoiceProfile {
  if (profile.kind === kind) return profile;

  const keepName = !isEnvironmentName(profile.name);
  const name = keepName ? profile.name : kind === "realtime" ? "Realtime" : "Pipeline";
  const interruptionMode =
    profile.pipeline?.turnHandling?.interruptionMode ??
    profile.realtime?.turnHandling?.interruptionMode ??
    "barge_in";

  const seeded = seedProfile(catalog, kind, name, profile.id, interruptionMode);
  if (!seeded) return profile;
  // Capabilities a user had set belong to the old architecture, so they are
  // dropped rather than carried onto a model that may not support them.
  return { ...seeded, options: undefined };
}

// ---------------------------------------------------------------------------
// Profile collections
//
// Every operation takes and returns a whole document rather than mutating one.
// The draft is React state, so an operation that mutated in place would not
// trigger a re-render, and an operation that guessed a new id would risk
// colliding with one already in the file.
// ---------------------------------------------------------------------------

/**
 * An id that cannot collide with an existing profile.
 *
 * A counter is used rather than a random suffix so a profile created twice from
 * the same starting point gets the same id, which keeps the document stable and
 * diffable instead of churning on every press.
 */
export function uniqueProfileId(document: ConfigDocument, base: string): string {
  const taken = new Set(document.profiles.map((profile) => profile.id));
  if (!taken.has(base)) return base;
  for (let suffix = 2; ; suffix += 1) {
    const candidate = `${base}-${suffix}`;
    if (!taken.has(candidate)) return candidate;
  }
}

/** A name that is not already used, so the list stays readable. */
export function uniqueProfileName(document: ConfigDocument, base: string): string {
  const taken = new Set(document.profiles.map((profile) => profile.name.trim().toLowerCase()));
  if (!taken.has(base.trim().toLowerCase())) return base;
  for (let suffix = 2; ; suffix += 1) {
    const candidate = `${base} ${suffix}`;
    if (!taken.has(candidate.trim().toLowerCase())) return candidate;
  }
}

/**
 * Add a new profile and make it active.
 *
 * A new profile is seeded from the catalog rather than copied from the active
 * one: a fresh profile is a fresh start, and inheriting somebody else's models
 * without being asked is how a user ends up not knowing what a session runs.
 */
export function createProfile(
  catalog: ProviderCatalog,
  document: ConfigDocument,
  kind: ProfileKind,
  name: string,
): ConfigDocument {
  const label = name.trim() || (kind === "realtime" ? "Realtime" : "Pipeline");
  // Seeded fresh rather than copied from the active profile: a new profile is a
  // new start, and inheriting somebody else's models without being asked is how
  // a user ends up not knowing what a session runs.
  const seeded = seedProfile(
    catalog,
    kind,
    uniqueProfileName(document, label),
    uniqueProfileId(document, `profile-${kind}`),
    "barge_in",
  );
  if (!seeded) return document;
  return {
    ...document,
    activeProfileId: seeded.id,
    profiles: [...document.profiles, seeded],
  };
}

/**
 * Copy a profile, keeping every selection, under a new name and id.
 *
 * Duplicating is how a user explores a change without risking the setup they
 * already have working, so the copy is exact and then activated.
 */
export function duplicateProfile(document: ConfigDocument, profileId: string): ConfigDocument {
  const source = document.profiles.find((profile) => profile.id === profileId);
  if (!source) return document;
  const copy: VoiceProfile = {
    ...structuredClone(source),
    id: uniqueProfileId(document, `${source.id}-copy`),
    name: uniqueProfileName(document, `${source.name} copy`),
  };
  return {
    ...document,
    activeProfileId: copy.id,
    profiles: [...document.profiles, copy],
  };
}

export function renameProfile(
  document: ConfigDocument,
  profileId: string,
  name: string,
): ConfigDocument {
  const trimmed = name.trim();
  if (!trimmed) return document;
  if (!document.profiles.some((profile) => profile.id === profileId)) return document;
  return {
    ...document,
    profiles: document.profiles.map((profile) =>
      profile.id === profileId ? { ...profile, name: trimmed } : profile,
    ),
  };
}

export function activateProfile(document: ConfigDocument, profileId: string): ConfigDocument {
  if (!document.profiles.some((profile) => profile.id === profileId)) return document;
  if (document.activeProfileId === profileId) return document;
  return { ...document, activeProfileId: profileId };
}

/**
 * Whether any profile may be deleted right now.
 *
 * The worker needs a profile to run. Deleting the last one would leave it on the
 * environment with no way back from the settings screen, so the final profile is
 * kept. Deleting the *active* one is fine: `deleteProfile` promotes a neighbour.
 */
export function canDeleteProfile(document: ConfigDocument): boolean {
  return document.profiles.length > 1;
}

/**
 * Remove a profile, promoting another when the active one goes.
 *
 * Promotion picks the profile before the removed one, so deleting the first of
 * three does not silently jump the user to the end of their own list.
 */
export function deleteProfile(document: ConfigDocument, profileId: string): ConfigDocument {
  if (!canDeleteProfile(document)) return document;
  const index = document.profiles.findIndex((profile) => profile.id === profileId);
  if (index < 0) return document;
  const profiles = document.profiles.filter((profile) => profile.id !== profileId);
  if (document.activeProfileId !== profileId) {
    return { ...document, profiles };
  }
  const promoted = profiles[Math.min(index, profiles.length - 1)];
  return { ...document, profiles, activeProfileId: promoted.id };
}
