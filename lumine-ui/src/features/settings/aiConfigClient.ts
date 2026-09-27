import { invoke } from "@tauri-apps/api/core";
import type { ConfigDocument, Diagnostic, ProviderCatalog } from "./aiConfigTypes";

/**
 * Every Tauri call the AI Control Center makes.
 *
 * The settings surface never talks to Python, LiveKit, or a provider directly.
 * Two boundaries matter:
 *
 * - **Credentials are write-only.** `setCredential` takes a secret and returns
 *   redacted status; `getCredentialStatus` returns presence and a last-four tail.
 *   There is deliberately no function here that reads a stored secret back.
 * - **Validation is server-side.** The worker owns the capability rules, so the UI
 *   renders the diagnostics it returns rather than deciding validity itself.
 */

export class DesktopUnavailableError extends Error {
  constructor() {
    super("This screen needs the Lumine desktop app. Run `npm run tauri dev`.");
    this.name = "DesktopUnavailableError";
  }
}

/** The catalog shape this screen was written against. See `agent/providers.py`. */
const CATALOG_VERSION = 7;

export class CatalogTooNewError extends Error {
  constructor(received: number) {
    super(
      `The agent ships a provider catalog this build does not understand (v${received}, expected v${CATALOG_VERSION}). Update the app.`,
    );
    this.name = "CatalogTooNewError";
  }
}

/**
 * Refuse a catalog this screen was not written for.
 *
 * The catalog is the only description of what providers, models, and voices
 * exist. Reading a field that is not there would throw somewhere deep in a
 * render, which is a confusing way to learn the agent and the app disagree.
 */
function assertCatalogVersion(catalog: ProviderCatalog): ProviderCatalog {
  if (typeof catalog?.version !== "number") {
    throw new CatalogTooNewError(Number.NaN);
  }
  if (catalog.version > CATALOG_VERSION) {
    throw new CatalogTooNewError(catalog.version);
  }
  return catalog;
}

function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** Run a Tauri command, turning "not running inside the app" into a clear error. */
async function call<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  if (!isTauri()) throw new DesktopUnavailableError();
  return invoke<T>(command, args);
}

export async function getProviderCatalog(): Promise<ProviderCatalog> {
  return assertCatalogVersion(await call<ProviderCatalog>("get_provider_catalog"));
}

export type EffectiveConfig = {
  /** "ui" when a saved document governs; "env" when agent/.env still does. */
  source: "ui" | "env";
  document: ConfigDocument;
  diagnostics: Diagnostic[];
};

/** The effective configuration, with its provenance. */
export function getConfig(): Promise<EffectiveConfig> {
  return call<EffectiveConfig>("get_config");
}

export function saveConfig(document: ConfigDocument): Promise<void> {
  return call<void>("save_config", { document });
}

export type ValidationResult = {
  ok: boolean;
  errorCount: number;
  warningCount: number;
  diagnostics: Diagnostic[];
};

export function validateConfig(document: ConfigDocument): Promise<ValidationResult> {
  return call<ValidationResult>("validate_config", { document: JSON.stringify(document) });
}

export type CredentialStatus = {
  present: boolean;
  last4: string | null;
  updatedAt: string | null;
};

export function setCredential(provider: string, secret: string): Promise<CredentialStatus> {
  return call<CredentialStatus>("set_credential", { provider, secret });
}

export function deleteCredential(provider: string): Promise<void> {
  return call<void>("delete_credential", { provider });
}

export function getCredentialStatus(provider: string): Promise<CredentialStatus> {
  return call<CredentialStatus>("get_credential_status", { provider });
}

/**
 * What a connectivity test concluded.
 *
 * Three values, not two. `rejected` is the only one that means the credential is
 * at fault: `inconclusive` covers a provider outage and a malformed request of
 * ours, and telling a user their key is broken in either case would send them to
 * re-enter a working credential.
 */
export type ProbeVerdict = "valid" | "rejected" | "inconclusive" | "no_probe" | "no_secret";

export type ProbeOutcome = {
  provider: string;
  verdict: ProbeVerdict;
  status?: number;
  latencyMs?: number;
  /** A short provider message, or why nothing could be concluded. Never a secret. */
  detail?: string;
};

export function testProviderCredential(provider: string): Promise<ProbeOutcome> {
  return call<ProbeOutcome>("test_provider_credential", { provider });
}

export type AgentStatus = {
  state: string;
  running: boolean;
  connected: boolean;
  pid: number | null;
  error: string | null;
};

export function getAgentStatus(): Promise<AgentStatus> {
  return call<AgentStatus>("get_agent_status");
}
