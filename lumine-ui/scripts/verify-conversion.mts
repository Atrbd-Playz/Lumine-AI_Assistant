// Cross-boundary check: does the config the settings screen builds actually pass
// Python's validation and resolve? This is the seam that can drift, because the
// seed values are chosen in TypeScript and judged in Python.
import { execFileSync, type ExecFileSyncOptionsWithStringEncoding } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { register } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { ConfigDocument, ProviderCatalog } from "../src/features/settings/aiConfigTypes.ts";

register("./ts-extension-resolver.mjs", import.meta.url);
const { convertProfileKind } = await import("../src/features/settings/profileOps.ts");

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
    throw new Error(
      `No virtualenv at ${repoRoot}\\.venv. Create one, or point this check at a prepared document.`,
    );
  }
  return found;
}

const python = findPython();

function pythonJson(args: string[], extraEnv?: Record<string, string>): string {
  const options: ExecFileSyncOptionsWithStringEncoding = {
    encoding: "utf-8",
    maxBuffer: 8 * 1024 * 1024,
  };
  if (extraEnv) options.env = { ...process.env, ...extraEnv };
  return execFileSync(python, args, options);
}

const catalog: ProviderCatalog = JSON.parse(pythonJson([resolve(repoRoot, "agent", "provider_catalog.py")]));

let failures = 0;
function check(label: string, condition: boolean, detail = ""): void {
  console.log(condition ? `  ok    ${label}` : `  FAIL  ${label}${detail ? ` — ${detail}` : ""}`);
  if (!condition) failures += 1;
}

const envProfile = {
  id: "env-realtime",
  name: "Gemini Live (from agent/.env)",
  kind: "realtime" as const,
  realtime: {
    provider: "google",
    model: "gemini-3.1-flash-live-preview",
    voice: "Sulafat",
    output: { mode: "model_voice" as const },
    turnHandling: { interruptionMode: "barge_in" as const },
  },
};

const document: ConfigDocument = {
  version: 1,
  activeProfileId: "env-realtime",
  // Credentials live in the OS keyring; an empty map is a legitimate document
  // and is what the screen writes when nothing has been entered yet.
  providers: {
    groq: { enabled: true, keyRef: null },
    cartesia: { enabled: true, keyRef: null },
  },
  profiles: [convertProfileKind(catalog, envProfile, "pipeline")],
};

console.log("what the settings screen produces");
console.log(`  ${JSON.stringify(document.profiles[0], null, 2).split("\n").join("\n  ")}`);

const dir = join(tmpdir(), "lumine-convert-check");
mkdirSync(dir, { recursive: true });
const configPath = join(dir, "lumine.config.json");
writeFileSync(configPath, JSON.stringify(document, null, 2), "utf-8");

console.log("\nwhat Python makes of it");
let payload;
try {
  payload = JSON.parse(pythonJson([resolve(repoRoot, "agent", "validate_config.py"), configPath]));
} catch (error) {
  console.log(`  FAIL  the validator did not run\n${error}`);
  process.exit(1);
}

const blocking = payload.diagnostics.filter((d: { severity: string }) => d.severity === "error");
check("the validator accepted the document", payload.ok === true, JSON.stringify(blocking));
check("there are no blocking diagnostics", blocking.length === 0, JSON.stringify(blocking, null, 2));
for (const diagnostic of payload.diagnostics) {
  console.log(`  ${diagnostic.severity.padEnd(7)} ${diagnostic.code} ${diagnostic.message}`);
}

console.log("\nwhat the worker would do with it");
// `--describe` reports the *active* configuration, so the document has to be
// pointed at the way agent_manager.rs points the worker at it.
const active = JSON.parse(
  pythonJson([resolve(repoRoot, "agent", "validate_config.py"), "--describe"], {
    LUMINE_CONFIG_PATH: configPath,
  }),
);
check("the saved document governs, not the environment", active.source === "ui", `source=${active.source}`);
check("it loads with no problems", active.diagnostics.length === 0, JSON.stringify(active.diagnostics, null, 2));

const loaded = active.document?.profiles?.find(
  (candidate: { id: string }) => candidate.id === active.document.activeProfileId,
);
check("the active profile survives the round trip", loaded?.kind === "pipeline", JSON.stringify(loaded?.kind));
for (const stage of ["stt", "llm", "tts", "vad"]) {
  const entry = loaded?.pipeline?.[stage];
  check(`${stage} kept its selection`, Boolean(entry?.provider && entry?.model), JSON.stringify(entry));
}
check("the voice Lumine is known by survived", loaded?.pipeline?.tts?.voice !== undefined);
check(
  "the credential references survived",
  active.document?.providers?.groq?.enabled === true,
  JSON.stringify(active.document?.providers),
);

console.log(failures === 0 ? "\nAll checks passed." : `\n${failures} check(s) failed.`);
process.exit(failures === 0 ? 0 : 1);

