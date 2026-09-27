import { useCallback, useEffect, useState } from "react";
import {
  getSetupStatus,
  type CredentialSlotStatus,
  type RequiredCredential,
  type SetupStatus,
} from "./aiConfigClient";

/**
 * Whether this install can hold a conversation yet.
 *
 * The gate exists because the alternative was: press Connect, wait through room
 * creation, token minting and worker startup, and only then discover that
 * `CARTESIA_API_KEY` was never set. That failure looks exactly like a broken
 * microphone, and the person who hits it has no way to tell which it was.
 *
 * ## What it deliberately does not do
 *
 * It does not block the app. Appearance, Diagnostics and the Avatar Lab all work
 * without a credential, and blocking them would make the settings needed to
 * *fix* the problem unreachable. Only the voice path is gated, and only on a
 * `false` that is genuinely false.
 *
 * It also does not cache a `true`. A credential can be deleted from another
 * window, or revoked at the provider, between two connects; re-reading on each
 * connect is one call against a local process and keeps the gate from becoming
 * a stale promise.
 */
export type SetupState = {
  status: SetupStatus | null;
  /** null means "not known yet", which must not be read as "not ready". */
  error: string | null;
  loading: boolean;
  /** Re-read on demand, e.g. after the user saves a key. */
  refresh: () => Promise<void>;
};

export function useSetupStatus(): SetupState {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await getSetupStatus());
      setError(null);
    } catch (cause) {
      // An unanswered question must not open the gate, so this is an error
      // state rather than a silent `ready: true`.
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { status, error, loading, refresh };
}

/** What a provider is being asked to do, in words a person would use. */
const CAPABILITY_WORDS: Record<string, string> = {
  stt: "speech to text",
  llm: "thinking and replies",
  tts: "speech",
  realtime: "live speech",
  vad: "turn detection",
  transport: "the voice room",
};

export function describeRole(credential: RequiredCredential): string {
  if (credential.local) {
    return "Runs on this device. Nothing to set up.";
  }
  const words = credential.capabilities
    .map((capability) => CAPABILITY_WORDS[capability] ?? capability)
    .filter((word, index, all) => all.indexOf(word) === index);
  if (words.length === 0) {
    return "Needed for this profile.";
  }
  const list =
    words.length === 1
      ? words[0]
      : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
  return `For ${list}.`;
}

/**
 * Whether this provider can already be used, and where each value came from.
 *
 * The sentence uses the *slot* names, not the environment variable names, for
 * anything it names as still owed. A wizard that says "still needs
 * LIVEKIT_API_SECRET" has stopped talking to a person and started talking to a
 * shell prompt, and the label the field above it already carries — "API secret" —
 * is the word they were looking for.
 */
export function describeAvailability(credential: RequiredCredential): string {
  if (credential.local) {
    return "Ready.";
  }
  const owed = credential.missing.map(
    (name) => credential.keySlots.find((slot) => slot.env === name)?.label ?? name,
  );
  // Partly filled reads as partly filled, and it is the state that needs saying
  // most: "Saved in the system keyring" beside a provider that needs three values
  // is a sentence that cannot both be true and be useful.
  if (credential.stored && owed.length > 0) {
    return `Partly saved — still needs ${listNames(owed)}.`;
  }
  if (credential.stored && credential.inEnv) {
    return "Saved, and also set in the environment.";
  }
  if (credential.stored) {
    return credential.storedLast4
      ? `Saved in the system keyring ····${credential.storedLast4}`
      : "Saved in the system keyring.";
  }
  if (credential.inEnv) {
    return "Set in the environment.";
  }
  return "Not set.";
}

/** `A`, `A and B`, `A, B and C`. */
function listNames(names: string[]): string {
  if (names.length === 1) return names[0];
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/**
 * One drawable row per variable of a provider's credential, with its own state.
 *
 * ## Why this exists
 *
 * LiveKit's credential is three values, and the first version of the quickstart
 * drew three fields inside a panel that closed when *one* of them was saved. The
 * consequences were all reported as the same bug: pressing Save on the server URL
 * looked like saving the whole credential, and reopening it showed three empty
 * boxes with nothing to suggest that one of them was already filled — because
 * there was nowhere on the page that could say so.
 *
 * The fix is not a bigger Save button. It is that each variable has a visible
 * state of its own, and that saving one leaves the others exactly where they
 * were.
 *
 * ## The fallback
 *
 * `slots` is absent when the desktop layer predates it. Rather than make every
 * consumer handle both shapes, this derives one row per `keySlots` entry and
 * then overlays the reported state by variable name. With no `slots` at all, a
 * row falls back to the provider-level answer, which is exactly as much as the
 * older layer knew.
 */
export function credentialSlots(credential: RequiredCredential): CredentialSlotStatus[] {
  const descriptors =
    credential.keySlots.length > 0
      ? credential.keySlots
      : credential.keyEnv.map((env) => ({
          env,
          label: `${credential.label} key`,
          kind: "secret" as const,
          help: "",
        }));
  const reported = new Map((credential.slots ?? []).map((slot) => [slot.env, slot]));

  return descriptors.map((descriptor) => {
    const slot = reported.get(descriptor.env);
    if (slot) return slot;
    // No per-slot report. Fall back to the provider-level answer, which is only
    // trustworthy for a single-variable provider -- and saying so is better than
    // reporting every one of three LiveKit variables as stored.
    const stored = credential.stored;
    return {
      ...descriptor,
      stored: descriptors.length === 1 ? stored : false,
      storedLast4: descriptors.length === 1 ? credential.storedLast4 : null,
      inEnv: credential.inEnv,
    };
  });
}

/** `1 of 3 set`, and how many are still owed. */
export function slotProgress(slots: CredentialSlotStatus[]): {
  done: number;
  total: number;
  label: string;
} {
  const done = slots.filter((slot) => slot.stored || slot.inEnv).length;
  return {
    done,
    total: slots.length,
    label: `${done} of ${slots.length} set`,
  };
}

/**
 * The providers still missing something, in the order they were listed.
 *
 * This mirrors the Rust reduction exactly rather than re-deriving it. The gate
 * opens on `status.ready`; this list is what the onboarding screen shows, and a
 * screen that disagreed with the boolean would offer to fix things that are
 * already fine while staying silent about the ones that are not.
 */
export function missingCredentials(status: SetupStatus | null): RequiredCredential[] {
  if (!status) {
    return [];
  }
  return status.required.filter((credential) => !credential.local && credential.missing.length > 0);
}
