/**
 * Checks for the pure profile operations in `src/features/settings/profileOps.ts`.
 *
 * The frontend has no test runner, and the project rule is not to add one for a
 * convenience. Node can strip the type annotations on its own, so these checks
 * run against the real source with no extra dependency.
 *
 *   npm run check:profile-ops
 *
 * The catalog comes from the Python side, which is the source of truth for what
 * providers, models, and voices actually exist. Generating it here keeps the two
 * halves honest: a model added to `agent/providers.py` shows up in these results.
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { register } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { ProviderCatalog, VoiceProfile } from "../src/features/settings/aiConfigTypes.ts";

// The source under test uses extensionless relative imports, so the resolver
// hook has to be registered before the module is pulled in. Registering is a
// side effect, hence the dynamic import.
register("./ts-extension-resolver.mjs", import.meta.url);
const {
  activateProfile,
  canDeleteProfile,
  convertProfileKind,
  createProfile,
  defaultVoice,
  deleteProfile,
  duplicateProfile,
  isEnvironmentName,
  renameProfile,
  uniqueProfileId,
  uniqueProfileName,
} = await import("../src/features/settings/profileOps.ts");

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");

function loadCatalog(): ProviderCatalog {
  // An explicit path wins, so a CI job can pin a fixture instead of the venv.
  if (process.argv[2]) {
    return JSON.parse(readFileSync(resolve(process.argv[2]), "utf-8"));
  }

  const venv = process.platform === "win32" ? "python.exe" : "python";
  const candidates = [
    resolve(repoRoot, ".venv", "Scripts", venv),
    resolve(repoRoot, ".venv", "bin", venv),
  ];
  const python = candidates.find((path) => existsSync(path));
  if (!python) {
    throw new Error(
      `No repository virtualenv found. Create .venv at the repo root, or pass a catalog path:\n  npm run check:profile-ops -- <path-to-catalog.json>`,
    );
  }

  return JSON.parse(
    execFileSync(python, [resolve(repoRoot, "agent", "provider_catalog.py")], {
      encoding: "utf-8",
      maxBuffer: 8 * 1024 * 1024,
    }),
  );
}

let failures = 0;
function check(label: string, condition: boolean, detail = ""): void {
  if (condition) {
    console.log(`  ok    ${label}`);
    return;
  }
  failures += 1;
  console.log(`  FAIL  ${label}${detail ? ` — ${detail}` : ""}`);
}

const catalog = loadCatalog();

const envProfile: VoiceProfile = {
  id: "env-realtime",
  name: "Gemini Live (from agent/.env)",
  kind: "realtime",
  realtime: {
    provider: "google",
    model: "gemini-3.1-flash-live-preview",
    voice: "Sulafat",
    output: { mode: "model_voice" },
    turnHandling: { interruptionMode: "barge_in" },
  },
};

console.log("realtime -> pipeline");
const pipeline = convertProfileKind(catalog, envProfile, "pipeline");
check("kind becomes pipeline", pipeline.kind === "pipeline");
check("the environment label is dropped", pipeline.name === "Pipeline", `got ${pipeline.name}`);
check("the realtime section is cleared", pipeline.realtime === undefined);
check("speech recognition is seeded", Boolean(pipeline.pipeline?.stt?.model));
check("the language model is seeded", Boolean(pipeline.pipeline?.llm?.model));
check("speech synthesis is seeded", Boolean(pipeline.pipeline?.tts?.model));
check("speech synthesis has a voice", Boolean(pipeline.pipeline?.tts?.voice));
check("voice activity detection is seeded", Boolean(pipeline.pipeline?.vad?.model));
check(
  "the interruption mode carries over",
  pipeline.pipeline?.turnHandling?.interruptionMode === "barge_in",
);

console.log("pipeline -> realtime (round trip)");
const back = convertProfileKind(catalog, pipeline, "realtime");
check("kind becomes realtime", back.kind === "realtime");
check("the pipeline section is cleared", back.pipeline === undefined);
check("a realtime model is seeded", Boolean(back.realtime?.model));
check("a realtime voice is seeded", Boolean(back.realtime?.voice));
check(
  "the interruption mode survives the round trip",
  back.realtime?.turnHandling?.interruptionMode === "barge_in",
);

console.log("naming");
const named = { ...envProfile, name: "My careful setup" };
check("an environment name is recognised", isEnvironmentName("Gemini Live (from agent/.env)"));
check("a chosen name is not mistaken for one", !isEnvironmentName("My careful setup"));
check(
  "a chosen name survives a switch",
  convertProfileKind(catalog, named, "pipeline").name === "My careful setup",
);
check(
  "converting to the current kind is a no-op",
  convertProfileKind(catalog, envProfile, "realtime") === envProfile,
);

console.log("voices");
const google = catalog.providers.find((provider) => provider.id === "google");
const googleVoiceIds = (google?.voices ?? []).map((voice) => voice.id);
check(
  "Google's merged voice list has no duplicates",
  new Set(googleVoiceIds).size === googleVoiceIds.length,
  googleVoiceIds.join(", "),
);
check("a realtime model can be reached", Boolean(google?.models.some((m) => m.capability === "realtime")));
check(
  "every voice a realtime model offers is in the provider list",
  (google?.models ?? [])
    .filter((model) => model.capability === "realtime")
    .flatMap((model) => model.voices)
    .every((voiceId) => googleVoiceIds.includes(voiceId)),
);
const cartesia = catalog.providers.find((provider) => provider.id === "cartesia");
check(
  "the pipeline speech synthesis voice list is populated",
  (cartesia?.voices.length ?? 0) >= 4,
  `got ${cartesia?.voices.length ?? 0}`,
);
check("a provider default voice is preferred", defaultVoice(catalog, "cartesia") !== undefined);
check(
  "a model without its own voices falls back to the provider",
  Boolean(defaultVoice(catalog, "google", "gemini-2.5-flash")),
);

// --- profile collections (Phase 4) ---

function documentWith(...profiles: VoiceProfile[]) {
  return {
    version: 1,
    activeProfileId: profiles[0]?.id ?? "",
    providers: {},
    profiles,
  };
}

console.log("create");
const oneProfile = documentWith(envProfile);
const withPipeline = createProfile(catalog, oneProfile, "pipeline", "Careful");
check("a profile is added", withPipeline.profiles.length === 2);
check("the new profile is active", withPipeline.activeProfileId !== oneProfile.activeProfileId);
const created = withPipeline.profiles.find((p) => p.id === withPipeline.activeProfileId);
check("the new profile is a pipeline", created?.kind === "pipeline", created?.kind);
check("the new profile is named", created?.name === "Careful", created?.name);
check("it is seeded, not empty", Boolean(created?.pipeline?.stt?.model && created?.pipeline?.llm?.model));
check("the original is untouched", oneProfile.profiles.length === 1);
check("the input document is not mutated", oneProfile.profiles[0] === envProfile);

const second = createProfile(catalog, withPipeline, "realtime", "Careful");
check("a duplicate name is made unique", second.profiles[2]?.name === "Careful 2", second.profiles[2]?.name);
check("ids stay unique", new Set(second.profiles.map((p) => p.id)).size === second.profiles.length);
check("an unnamed create falls back to its kind", createProfile(catalog, oneProfile, "realtime", "   ").profiles[1]?.name === "Realtime");

console.log("unique ids");
check("an unused id is used as-is", uniqueProfileId(oneProfile, "profile-pipeline") === "profile-pipeline");
check("a taken id is numbered", uniqueProfileId(oneProfile, envProfile.id) === `${envProfile.id}-2`);
check("a name is used as-is when free", uniqueProfileName(oneProfile, "Fresh") === "Fresh");
check("a taken name is numbered", uniqueProfileName(documentWith({ ...envProfile, name: "Fresh" }), "Fresh") === "Fresh 2");

console.log("duplicate");
const copied = duplicateProfile(second, envProfile.id);
check("the copy is added", copied.profiles.length === 4);
check("the copy becomes active", copied.activeProfileId !== second.activeProfileId);
const copy = copied.profiles.find((p) => p.id === copied.activeProfileId);
check("the copy keeps the settings", copy?.realtime?.model === envProfile.realtime?.model);
check("the copy is renamed", copy?.name.endsWith("copy") === true, copy?.name);
check("the copy has a new id", copy?.id !== envProfile.id);
check("the source still exists", copied.profiles.some((p) => p.id === envProfile.id));
check("duplicating nothing changes nothing", duplicateProfile(second, "nope") === second);

console.log("activate");
check("activation moves the pointer", activateProfile(second, second.profiles[1].id).activeProfileId === second.profiles[1].id);
check("activating the active one is a no-op", activateProfile(second, second.activeProfileId) === second);
check("activating a stranger is a no-op", activateProfile(second, "nope") === second);

console.log("rename");
const renamed = renameProfile(second, envProfile.id, "  Night mode  ");
check("the name is trimmed", renamed.profiles[0]?.name === "Night mode", renamed.profiles[0]?.name);
check("an empty name is refused", renameProfile(second, envProfile.id, "   ") === second);
check("an unknown id is a no-op", renameProfile(second, "nope", "X") === second);

console.log("delete");
const three = second; // env-realtime, profile-pipeline, profile-realtime
check("the last profile cannot be deleted", !canDeleteProfile(documentWith(envProfile)));
check("several profiles can be", canDeleteProfile(three));
const removedMiddle = deleteProfile(three, three.profiles[1].id);
check("the profile is gone", removedMiddle.profiles.length === 2);
check("a non-active delete keeps the active one", removedMiddle.activeProfileId === three.activeProfileId);
const removedActive = deleteProfile(three, three.activeProfileId);
check("deleting the active one promotes another", removedActive.activeProfileId !== three.activeProfileId);
check("the promoted profile still exists", removedActive.profiles.some((p) => p.id === removedActive.activeProfileId));
check("at least one profile survives", removedActive.profiles.length === 2);
const removedFirst = deleteProfile(three, three.profiles[0].id);
check("deleting the first promotes the next one, not the last", removedFirst.profiles[0].id === three.profiles[1].id, removedFirst.profiles[0].id);
check("deleting the last profile is refused", deleteProfile(documentWith(envProfile), envProfile.id).profiles.length === 1);
check("deleting nothing is a no-op", deleteProfile(three, "nope") === three);

console.log("every document stays internally consistent");
// The invariant the worker's validator enforces. A structural operation that
// breaks it produces a document that saves and then refuses to run.
for (const [label, doc] of [
  ["created", withPipeline],
  ["created twice", second],
  ["duplicated", copied],
  ["renamed", renamed],
  ["deleted middle", removedMiddle],
  ["deleted active", removedActive],
] as const) {
  const ids = doc.profiles.map((p) => p.id);
  check(
    `${label}: ids are unique`,
    ids.length === new Set(ids).size,
    ids.join(", "),
  );
  check(`${label}: the active id resolves`, ids.includes(doc.activeProfileId), doc.activeProfileId);
  check(`${label}: at least one profile remains`, doc.profiles.length >= 1);
  check(
    `${label}: every profile is complete`,
    doc.profiles.every((p) =>
      p.kind === "realtime"
        ? Boolean(p.realtime?.model)
        : Boolean(p.pipeline?.stt?.model && p.pipeline?.llm?.model && p.pipeline?.tts?.model),
    ) === true,
  );
}

console.log(failures === 0 ? "\nAll checks passed." : `\n${failures} check(s) failed.`);
process.exit(failures === 0 ? 0 : 1);
