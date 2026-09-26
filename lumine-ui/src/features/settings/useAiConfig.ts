import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CONFIG_VERSION,
  hasBlockingDiagnostics,
  type ConfigDocument,
  type Diagnostic,
  type ProviderCatalog,
  type VoiceProfile,
} from "./aiConfigTypes";
import {
  DesktopUnavailableError,
  getConfig,
  getProviderCatalog,
  saveConfig,
  validateConfig,
  type ValidationResult,
} from "./aiConfigClient";

/**
 * State for the AI Control Center.
 *
 * Two rules shape this hook:
 *
 * - **The worker decides validity.** Validation runs in Python and the result is
 *   rendered as-is, so the settings screen can never offer a combination the
 *   runtime will refuse.
 * - **Nothing is saved until it validates.** `save()` is refused while a
 *   blocking diagnostic exists, and the draft is what the user edits, so a
 *   half-finished configuration never replaces a working one.
 */

export type LoadState = "loading" | "ready" | "error";

/** The document the settings screen edits, plus its provenance. */
export type AiConfigState = {
  state: LoadState;
  /** null means "no document has ever been saved": the environment is in charge. */
  document: ConfigDocument | null;
  /** True when the active profile is the read-only one derived from agent/.env. */
  isEnvironmentBacked: boolean;
  catalog: ProviderCatalog | null;
  error: string | null;
};

const EMPTY_DIAGNOSTICS: Diagnostic[] = [];

export function useAiConfig() {
  const [state, setState] = useState<AiConfigState>({
    state: "loading",
    document: null,
    isEnvironmentBacked: true,
    catalog: null,
    error: null,
  });
  const [draft, setDraft] = useState<ConfigDocument | null>(null);
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>(EMPTY_DIAGNOSTICS);
  const [validating, setValidating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const validationId = useRef(0);

  const applyValidation = useCallback((result: ValidationResult) => {
    setDiagnostics(result.diagnostics);
    setState((current) => ({ ...current, error: null }));
  }, []);

  const runValidation = useCallback(
    async (document: ConfigDocument) => {
      const id = ++validationId.current;
      setValidating(true);
      try {
        const result = await validateConfig(document);
        // Ignore a result that a newer keystroke has already superseded.
        if (id === validationId.current) applyValidation(result);
        return result;
      } catch (cause) {
        if (id === validationId.current) {
          setDiagnostics([]);
          setState((current) => ({
            ...current,
            error: cause instanceof Error ? cause.message : "Validation failed.",
          }));
        }
        return null;
      } finally {
        if (id === validationId.current) setValidating(false);
      }
    },
    [applyValidation],
  );

  const load = useCallback(async () => {
    setState((current) => ({ ...current, state: "loading", error: null }));
    try {
      const [catalog, effective] = await Promise.all([getProviderCatalog(), getConfig()]);
      // source === "env" is a normal state, not a failure: it means the user has
      // never saved anything and agent/.env still governs the voice stack. The
      // document is still returned so the settings screen can show it.
      const document = effective.document;
      setDraft(document);
      setState({
        state: "ready",
        document,
        isEnvironmentBacked: effective.source === "env",
        catalog,
        error: null,
      });
      return document;
    } catch (cause) {
      setState((current) => ({
        ...current,
        state: "error",
        error:
          cause instanceof DesktopUnavailableError
            ? cause.message
            : cause instanceof Error
              ? cause.message
              : "Could not load the AI configuration.",
      }));
      return null;
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** Replace the active profile, then revalidate. */
  const updateActiveProfile = useCallback(
    (updater: (profile: VoiceProfile) => VoiceProfile) => {
      setDraft((current) => {
        if (!current) return current;
        const next: ConfigDocument = {
          ...current,
          profiles: current.profiles.map((profile) =>
            profile.id === current.activeProfileId ? updater(profile) : profile,
          ),
        };
        void runValidation(next);
        return next;
      });
    },
    [runValidation],
  );

  /**
   * Apply a pure document operation to the draft.
   *
   * One path for every structural change — activating, creating, duplicating,
   * deleting — so none of them can forget to revalidate. The operation receives
   * the current draft and returns the next one; an operation that returns its
   * input unchanged is a no-op, which is how the pure helpers report "refused".
   */
  const mutateDocument = useCallback(
    (operation: (document: ConfigDocument) => ConfigDocument) => {
      setDraft((current) => {
        if (!current) return current;
        const next = operation(current);
        if (next === current) return current;
        void runValidation(next);
        return next;
      });
    },
    [runValidation],
  );

  const canSave = useMemo(
    () => !hasBlockingDiagnostics(diagnostics) && draft !== null && !saving,
    [diagnostics, draft, saving],
  );

  const save = useCallback(async () => {
    if (!draft) return false;
    setSaving(true);
    setSaveError(null);
    try {
      // Validate once more immediately before writing, so a document that
      // became invalid while the user was away is not persisted.
      const result = await validateConfig(draft);
      if (!result.ok) {
        applyValidation(result);
        setSaveError("Fix the problems above before saving.");
        return false;
      }
      await saveConfig(draft);
      setState((current) => ({ ...current, document: draft, isEnvironmentBacked: false }));
      return true;
    } catch (cause) {
      setSaveError(cause instanceof Error ? cause.message : "Could not save the configuration.");
      return false;
    } finally {
      setSaving(false);
    }
  }, [applyValidation, draft]);

  const discard = useCallback(() => {
    setDraft(state.document);
    setSaveError(null);
    if (state.document) void runValidation(state.document);
  }, [runValidation, state.document]);

  const activeProfile = useMemo(
    () => draft?.profiles.find((profile) => profile.id === draft.activeProfileId) ?? null,
    [draft],
  );

  const hasUnsavedChanges = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(state.document),
    [draft, state.document],
  );

  return {
    ...state,
    draft,
    activeProfile,
    diagnostics,
    validating,
    saving,
    saveError,
    canSave,
    hasUnsavedChanges,
    load,
    updateActiveProfile,
    mutateDocument,
    save,
    discard,
  };
}

/** Build a `{provider, model}` reference, dropping empty parts. */
/** A minimal valid document, used when bootstrapping from nothing. */
export function blankDocument(profile: VoiceProfile): ConfigDocument {
  return {
    version: CONFIG_VERSION,
    activeProfileId: profile.id,
    providers: {},
    profiles: [profile],
    agent: {},
  };
}
