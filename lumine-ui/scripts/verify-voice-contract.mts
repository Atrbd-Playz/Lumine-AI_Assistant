// Cross-boundary check on the one sentence the settings screen makes a promise
// out of: *per-reply emotion reaches the voice only when a separate synthesizer
// is in your audio path.*
//
// Three parties have to agree, and no one of them can see the other two:
//
//   1. `agent/settings/providers.py` publishes which delivery controls exist and
//      which models they mean anything on. That is the catalogue, and the screen
//      draws exactly what it says.
//   2. `agent/runtime/emotion_voice.py` maps a demonstrated emotion onto a name
//      Cartesia will accept, on a model family it will accept it from. It holds
//      its own copy of the vocabulary because `settings/providers.py` must stay
//      importable without livekit.
//   3. `ModelsPage.tsx` tells the user when that path does not exist at all.
//
// A drift between 1 and 2 renders as a control that moves and a voice that does
// not change. A drift between 2 and 3 renders as a user who spent ten minutes
// tuning a stage nothing was reading. Both are silent, so both are checked here
// rather than trusted.
import { execFileSync, type ExecFileSyncOptionsWithStringEncoding } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { ProviderCatalog } from "../src/features/settings/aiConfigTypes.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");

/** The repository interpreter, which is the only one with the agent's packages. */
function findPython(): string {
  const name = process.platform === "win32" ? "python.exe" : "python";
  const found = [
    resolve(repoRoot, ".venv", "Scripts", name),
    resolve(repoRoot, ".venv", "bin", name),
  ].find((path) => existsSync(path));
  if (!found) {
    throw new Error(`No virtualenv at ${repoRoot}\\.venv. This check needs the agent's packages.`);
  }
  return found;
}

const python = findPython();

function pythonJson(args: string[], fromRepoRoot = false): string {
  const options: ExecFileSyncOptionsWithStringEncoding = {
    encoding: "utf-8",
    maxBuffer: 8 * 1024 * 1024,
  };
  if (fromRepoRoot) options.cwd = repoRoot;
  return execFileSync(python, args, options);
}

const catalog: ProviderCatalog = JSON.parse(pythonJson([resolve(repoRoot, "agent", "provider_catalog.py")]));

/**
 * The bridge's own tables, read from the module rather than copied into here.
 *
 * A hand-typed copy would make this check agree with itself, which is the one
 * kind of check that is worse than none.
 */
const bridge = JSON.parse(
  pythonJson(
    [
      "-c",
      [
        "import json",
        "from agent.runtime.emotion_voice import EMOTION_TO_TTS, SUPPORTED_MODEL_PREFIX",
        "from agent.runtime.emotion_contract import CANONICAL_EMOTIONS",
        "print(json.dumps({",
        "  'map': EMOTION_TO_TTS,",
        "  'prefix': SUPPORTED_MODEL_PREFIX,",
        "  'canonical': sorted(CANONICAL_EMOTIONS),",
        "}))",
      ].join("\n"),
    ],
    true,
  ),
) as { map: Record<string, string>; prefix: string; canonical: string[] };

let failures = 0;
function check(label: string, condition: boolean, detail = ""): void {
  console.log(condition ? `  ok    ${label}` : `  FAIL  ${label}${detail ? ` — ${detail}` : ""}`);
  if (!condition) failures += 1;
}

const cartesia = catalog.providers.find((provider) => provider.id === "cartesia");
const ttsModels = (cartesia?.models ?? []).filter((model) => model.capability === "tts");

console.log("what the catalogue publishes");

check("Cartesia is a speech provider", Boolean(cartesia), "no `cartesia` in the catalog");
check("it offers speech models", ttsModels.length > 0, `${ttsModels.length} tts models`);

const emotionModel = ttsModels.find((model) => model.options.some((option) => option.name === "emotion"));
const emotion = emotionModel?.options.find((option) => option.name === "emotion");
const optionOn = (name: string) => emotionModel?.options.find((option) => option.name === name);

check("a delivery `emotion` control exists", Boolean(emotion), "no `emotion` option on any Cartesia model");
const listChoosers = ["select", "combobox"];
check(
  "it is offered as a chooser from a declared list",
  Boolean(emotion && listChoosers.includes(emotion.control)) && (emotion?.values.length ?? 0) > 0,
  `control=${emotion?.control} values=${emotion?.values.length ?? 0}`,
);

// The three controls move as a unit. A gate applied to one and not the others
// is how you get a speed slider that lands and an emotion that is silently
// dropped, on the same model, in the same reply.
const gateOf = (name: string): string[] | undefined => optionOn(name)?.models;
const emotionGate = gateOf("emotion");
check("its model gate is explicit, not inherited", Boolean(emotionGate && emotionGate.length > 0));
check("the gate is the sonic-3 family", (emotionGate ?? []).every((id) => id.startsWith("sonic-3")), JSON.stringify(emotionGate));
check(
  "`speed` gates to the same models as `emotion`",
  JSON.stringify(gateOf("speed")) === JSON.stringify(emotionGate),
  `speed=${JSON.stringify(gateOf("speed"))} emotion=${JSON.stringify(emotionGate)}`,
);
check(
  "`volume` gates to the same models as `emotion`",
  JSON.stringify(gateOf("volume")) === JSON.stringify(emotionGate),
  `volume=${JSON.stringify(gateOf("volume"))} emotion=${JSON.stringify(emotionGate)}`,
);
// Choosing a voice is not a model-dependent act: it is the one setting on this
// stage that has to survive a model swap.
check(
  "`voice` is not gated, so a chosen voice survives a model change",
  (optionOn("voice")?.models.length ?? 0) === 0,
  JSON.stringify(optionOn("voice")?.models),
);

// A default outside the declared list is a value the screen can show but never
// draw as selected, and a value the profile can be seeded with and then fail to
// validate.
const declaredDefault = emotion?.default;
check(
  "the shipped `emotion` default is one of its own values",
  Boolean(typeof declaredDefault === "string" && emotion?.values.includes(declaredDefault)),
  `default=${JSON.stringify(declaredDefault)}`,
);

console.log("\nwhat the bridge does with it");

const bridgeTargets = Object.values(bridge.map);
const uncatalogued = [...new Set(bridgeTargets)].filter((target) => !emotion?.values.includes(target));
check(
  "every mapped emotion is one the settings screen offers",
  uncatalogued.length === 0,
  `not offered: ${JSON.stringify(uncatalogued)}`,
);

const unknownKeys = Object.keys(bridge.map).filter((key) => !bridge.canonical.includes(key));
check(
  "every mapped emotion is one Lumine can actually demonstrate",
  unknownKeys.length === 0,
  `not canonical: ${JSON.stringify(unknownKeys)}`,
);

check(
  "the bridge's model family is the catalogue's gate",
  (emotionGate ?? []).every((id) => id.startsWith(bridge.prefix)),
  `prefix=${bridge.prefix} gate=${JSON.stringify(emotionGate)}`,
);
const defaultTts = ttsModels.find((model) => model.default);
check(
  "the default Cartesia model is inside the gate, so a fresh install gets the feature",
  Boolean(defaultTts && emotionGate?.includes(defaultTts.id)),
  `default=${defaultTts?.id} gate=${JSON.stringify(emotionGate)}`,
);

console.log("\nwhat the screen says about it");

const modelsPage = readFileSync(resolve(repoRoot, "lumine-ui", "src", "pages", "settings", "ai", "ModelsPage.tsx"), "utf-8");

check(
  "the audio path is drawn, and only on the speech tab",
  /tab === "tts" &&\s*<AudioPathNotice/.test(modelsPage),
  "ModelsPage no longer renders AudioPathNotice on tab === \"tts\"",
);
check(
  "the notice reads the stack kind rather than a hard-coded provider",
  /profile\.kind === "realtime"/.test(modelsPage),
  "the caveat no longer depends on what kind of stack is configured",
);
check(
  "it says what does not reach the voice",
  /per-reply emotion/i.test(modelsPage),
  "the notice no longer names the thing that is missing",
);
check(
  "it offers the switch it claims to",
  /convertProfileKind\(catalog, profile, "pipeline"\)/.test(modelsPage),
  "the one-click move to a pipeline is gone",
);

console.log(failures === 0 ? "\nAll checks passed." : `\n${failures} check(s) failed.`);
process.exit(failures === 0 ? 0 : 1);
