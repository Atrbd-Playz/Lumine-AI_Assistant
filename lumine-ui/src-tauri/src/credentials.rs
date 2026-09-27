//! Credential storage for the AI Control Center.
//!
//! ## Why a credential is addressed by a slot
//!
//! The OS keyring holds one secret per *account name*, and the first version of
//! this module used the provider id as that name. That was correct for every
//! provider with one key and wrong for LiveKit, which needs three: a server URL,
//! an API key and an API secret.
//!
//! `inject_credentials` then wrote the single stored value into all three
//! variables, so a user who pasted their LiveKit API key got
//! `LIVEKIT_URL=APIKEY`, `LIVEKIT_API_KEY=APIKEY` and
//! `LIVEKIT_API_SECRET=APIKEY`. The worker could not register with LiveKit, the
//! room was never joined, and the app reported the credential as *stored* the
//! whole time. Nothing in the UI could show the mistake, because the UI only ever
//! asked "is there a secret for this provider id?".
//!
//! So an entry is now addressed by `(provider, slot)`, where the slot is the
//! environment variable the value belongs in. A provider with one variable keeps
//! the bare provider id as its account name, which is what makes every key stored
//! before this change keep working with no migration at all. A provider with
//! several gets `{id}:{VARIABLE}` per variable, and the legacy bare entry for
//! such a provider is deliberately *not* read -- one value cannot stand in for
//! three, and pretending otherwise is the bug.
//!
//! Two rules still shape this module:
//!
//! 1. **A credential is never returned to the webview.** The trait has no
//!    "give me the plaintext" method for callers outside the worker plumbing.
//!    `status` reports presence and the last four characters, which is enough
//!    for a settings screen.
//! 2. **Local providers need no credential.** Silero runs on-device; asking the
//!    OS keyring to store an empty string for it would be noise.

use std::fmt;

/// Service name used for every Lumine credential in the OS store.
const SERVICE: &str = "com.art.lumine-ui";

/// The keyring account name for one (provider, slot) pair.
///
/// A provider with a single credential keeps the bare provider id, so a key
/// stored by an earlier build is found again. A slotted provider gets the
/// variable name appended, because that is the only thing that distinguishes one
/// of its values from another.
pub fn account_name(provider_id: &str, slot: Option<&str>) -> String {
    match slot.map(str::trim).filter(|value| !value.is_empty()) {
        Some(slot) => format!("{provider_id}:{slot}"),
        None => provider_id.to_string(),
    }
}

/// What the settings UI is allowed to know about a stored credential.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CredentialStatus {
    pub present: bool,
    /// At most the final four characters, for recognising a key in the UI.
    pub last4: Option<String>,
    pub updated_at: Option<String>,
}

impl CredentialStatus {
    pub fn absent() -> Self {
        Self {
            present: false,
            last4: None,
            updated_at: None,
        }
    }
}

/// Errors surfaced to the command layer. Messages are safe to show a user and
/// never contain key material.
#[derive(Debug)]
pub enum CredentialError {
    /// The OS keyring is unavailable (no Secret Service on a headless Linux box,
    /// for example). The app can fall back to `agent/.env`.
    Unavailable(String),
    /// No credential is stored for this provider and slot.
    NotFound,
    Failed(String),
}

impl fmt::Display for CredentialError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CredentialError::Unavailable(detail) => {
                write!(f, "The system credential store is unavailable: {detail}")
            }
            CredentialError::NotFound => write!(f, "No credential is stored for this provider."),
            CredentialError::Failed(detail) => write!(f, "Credential operation failed: {detail}"),
        }
    }
}

impl std::error::Error for CredentialError {}

/// The seam the rest of the app should program against.
///
/// Implemented once per backend so the worker plumbing does not care whether
/// secrets live in the OS keyring or, later, in a Stronghold vault.
pub trait CredentialStore {
    fn set(&self, provider_id: &str, slot: Option<&str>, secret: &str) -> Result<(), CredentialError>;
    fn delete(&self, provider_id: &str, slot: Option<&str>) -> Result<(), CredentialError>;
    /// Redacted status only. Implementations must not expose the value.
    fn status(&self, provider_id: &str, slot: Option<&str>) -> Result<CredentialStatus, CredentialError>;
    /// For worker spawn and provider tests. Callers must not pass the result to
    /// the webview.
    fn secret_for_worker(
        &self,
        provider_id: &str,
        slot: Option<&str>,
    ) -> Result<String, CredentialError>;
    /// Every slot this provider has a value for, in the order given.
    ///
    /// Used where a provider needs *all* of its values at once -- the worker
    /// spawn, a credential probe, a voice preview. Reading only the first is the
    /// mistake this module exists to prevent, so the multi-value read is a named
    /// operation rather than a loop each caller writes for itself.
    fn secrets_for_worker(
        &self,
        provider_id: &str,
        slots: &[String],
    ) -> Vec<(String, String)> {
        slots
            .iter()
            .filter_map(|name| {
                // A single-variable provider may still hold a pre-slot key under
                // the bare provider id, so both spellings are tried. A provider
                // with several variables only ever reads its own names.
                let value = if slots.len() == 1 {
                    self.secret_for_worker(provider_id, Some(name))
                        .or_else(|_| self.secret_for_worker(provider_id, None))
                } else {
                    self.secret_for_worker(provider_id, Some(name))
                };
                value.ok().map(|secret| (name.clone(), secret))
            })
            .collect()
    }
}

/// OS-native implementation backed by the `keyring` crate.
pub struct OsCredentialStore;

fn entry(provider_id: &str, slot: Option<&str>) -> Result<keyring::Entry, CredentialError> {
    if provider_id.trim().is_empty() {
        return Err(CredentialError::Failed("empty provider id".into()));
    }
    keyring::Entry::new(SERVICE, &account_name(provider_id, slot))
        .map_err(|err| CredentialError::Unavailable(err.to_string()))
}

fn map_get_error(who: &str, err: keyring::Error) -> CredentialError {
    match err {
        keyring::Error::NoEntry => CredentialError::NotFound,
        other => CredentialError::Failed(format!("{who}: {other}")),
    }
}

impl CredentialStore for OsCredentialStore {
    fn set(&self, provider_id: &str, slot: Option<&str>, secret: &str) -> Result<(), CredentialError> {
        if secret.is_empty() {
            // Storing an empty secret would shadow a real key with a blank one.
            return self.delete(provider_id, slot);
        }
        entry(provider_id, slot)?
            .set_password(secret)
            .map_err(|err| CredentialError::Failed(format!("{provider_id}: {err}")))
    }

    fn delete(&self, provider_id: &str, slot: Option<&str>) -> Result<(), CredentialError> {
        match entry(provider_id, slot)?.delete_credential() {
            Ok(()) => Ok(()),
            // Deleting something that was never there is the desired end state.
            Err(keyring::Error::NoEntry) => Ok(()),
            Err(err) => Err(CredentialError::Failed(format!("{provider_id}: {err}"))),
        }
    }

    fn status(
        &self,
        provider_id: &str,
        slot: Option<&str>,
    ) -> Result<CredentialStatus, CredentialError> {
        match self.secret_for_worker(provider_id, slot) {
            Ok(secret) => {
                let tail: String = secret
                    .chars()
                    .rev()
                    .take(4)
                    .collect::<Vec<_>>()
                    .into_iter()
                    .rev()
                    .collect();
                Ok(CredentialStatus {
                    present: true,
                    last4: (!tail.is_empty()).then_some(tail),
                    updated_at: None,
                })
            }
            // A missing key is a normal state, not an error worth surfacing.
            Err(CredentialError::NotFound) => Ok(CredentialStatus::absent()),
            Err(other) => Err(other),
        }
    }

    fn secret_for_worker(
        &self,
        provider_id: &str,
        slot: Option<&str>,
    ) -> Result<String, CredentialError> {
        let who = account_name(provider_id, slot);
        entry(provider_id, slot)?
            .get_password()
            .map_err(|err| map_get_error(&who, err))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Mutex, MutexGuard, OnceLock};

    #[test]
    fn a_single_value_provider_keeps_the_bare_provider_id_as_its_account() {
        // The whole point: a key stored by an earlier build is still found.
        assert_eq!(account_name("groq", None), "groq");
        assert_eq!(account_name("groq", Some("GROQ_API_KEY")), "groq:GROQ_API_KEY");
    }

    #[test]
    fn a_slotted_provider_names_each_value_after_its_variable() {
        // LiveKit's three values have to be three entries, or they overwrite
        // each other and the worker gets one value three times.
        assert_eq!(
            account_name("livekit", Some("LIVEKIT_URL")),
            "livekit:LIVEKIT_URL"
        );
        assert_ne!(
            account_name("livekit", Some("LIVEKIT_API_KEY")),
            account_name("livekit", Some("LIVEKIT_API_SECRET"))
        );
    }

    #[test]
    fn a_blank_slot_is_the_same_as_no_slot() {
        assert_eq!(account_name("groq", Some("  ")), "groq");
    }

    /// Serializes access to the OS credential store.
    ///
    /// The Windows Credential Manager is a shared system resource and does not
    /// reliably serve several threads hammering it in a short window: entries
    /// written by one thread intermittently read back as absent for another. The
    /// production code is unaffected -- one process talks to the store from one
    /// place at a time -- but parallel `cargo test` does exactly that, so the
    /// tests take a lock.
    fn os_store_guard() -> MutexGuard<'static, ()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(()))
            .lock()
            // Recover from poisoning: one failing test should not cascade into
            // every other test reporting a spurious failure.
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// These tests touch the real OS keyring, so they run in parallel against
    /// shared external state. Each therefore uses its own probe id: reusing one
    /// id would let a concurrent test delete the value another is asserting on.
    fn probe(tag: &str) -> String {
        format!("lumine-credential-spike-{tag}-{}", std::process::id())
    }

    /// Removes whatever the test created, including on an early assertion failure.
    struct Cleanup(Vec<(String, Option<String>)>);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            for (provider, slot) in &self.0 {
                let _ = OsCredentialStore.delete(provider, slot.as_deref());
            }
        }
    }

    /// Start from a known-empty state, holding the store lock for the test body.
    fn fresh(entries: &[(&str, Option<&str>)]) -> (MutexGuard<'static, ()>, Cleanup) {
        let guard = os_store_guard();
        for (provider, slot) in entries {
            let _ = OsCredentialStore.delete(provider, *slot);
        }
        (
            guard,
            Cleanup(
                entries
                    .iter()
                    .map(|(provider, slot)| (provider.to_string(), slot.map(str::to_string)))
                    .collect(),
            ),
        )
    }

    #[test]
    fn round_trips_and_reports_only_a_redacted_tail() {
        let store = OsCredentialStore;
        let id = probe("roundtrip");
        let (_guard, _cleanup) = fresh(&[(&id, None)]);
        let secret = "sk-spike-not-a-real-key-9f3a";

        assert!(!store.status(&id, None).expect("status").present, "probe must start empty");

        store.set(&id, None, secret).expect("set should succeed");
        assert_eq!(store.secret_for_worker(&id, None).expect("get"), secret);

        let status = store.status(&id, None).expect("status");
        assert!(status.present);
        assert_eq!(status.last4.as_deref(), Some("9f3a"));
        // The redaction contract: the full value must not be reconstructible.
        assert!(!format!("{status:?}").contains(secret));

        store.delete(&id, None).expect("delete");
        assert!(!store.status(&id, None).expect("status").present);
    }

    #[test]
    fn deleting_an_absent_credential_is_not_an_error() {
        let id = probe("absent");
        let (_guard, _cleanup) = fresh(&[(&id, None)]);
        OsCredentialStore.delete(&id, None).expect("deleting nothing should succeed");
    }

    #[test]
    fn setting_an_empty_secret_clears_the_stored_value() {
        let store = OsCredentialStore;
        let id = probe("empty");
        let (_guard, _cleanup) = fresh(&[(&id, None)]);
        store.set(&id, None, "temporary").expect("set");
        store.set(&id, None, "").expect("empty set should clear");
        assert!(!store.status(&id, None).expect("status").present);
    }

    #[test]
    fn a_short_secret_still_reports_a_tail() {
        let store = OsCredentialStore;
        let id = probe("short");
        let (_guard, _cleanup) = fresh(&[(&id, None)]);
        store.set(&id, None, "abc").expect("set");
        let status = store.status(&id, None).expect("status");
        assert!(status.present);
        assert_eq!(status.last4.as_deref(), Some("abc"));
    }

    #[test]
    fn a_secret_replaced_by_another_reads_back_as_the_new_one() {
        // Guards against a stale-cache style bug: reporting `present` for a value
        // that is no longer the one stored.
        let store = OsCredentialStore;
        let id = probe("replace");
        let (_guard, _cleanup) = fresh(&[(&id, None)]);
        store.set(&id, None, "first-value").expect("first set");
        store.set(&id, None, "second-value").expect("second set");
        assert_eq!(store.secret_for_worker(&id, None).expect("get"), "second-value");
        assert_eq!(store.status(&id, None).expect("status").last4.as_deref(), Some("alue"));
    }

    /// The regression test for the bug this module was rekeyed to fix.
    #[test]
    fn three_slots_hold_three_different_values() {
        let store = OsCredentialStore;
        let id = probe("livekit-shape");
        let slots = [
            "LIVEKIT_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
        ];
        let owned: Vec<Option<String>> = slots.iter().map(|s| Some(s.to_string())).collect();
        let refs: Vec<(&str, Option<&str>)> = slots.iter().map(|s| (id.as_str(), Some(*s))).collect();
        let (_guard, _cleanup) = fresh(&refs);

        store.set(&id, Some(slots[0]), "wss://example.livekit.cloud").unwrap();
        store.set(&id, Some(slots[1]), "APIkeyone").unwrap();
        store.set(&id, Some(slots[2]), "secretvalue").unwrap();

        // Each slot reads back its own value, so the URL is not the API key.
        assert_eq!(
            store.secret_for_worker(&id, Some(slots[0])).unwrap(),
            "wss://example.livekit.cloud"
        );
        assert_eq!(store.secret_for_worker(&id, Some(slots[1])).unwrap(), "APIkeyone");
        assert_eq!(
            store.secret_for_worker(&id, Some(slots[2])).unwrap(),
            "secretvalue"
        );

        let resolved = store.secrets_for_worker(&id, &slots.iter().map(|s| s.to_string()).collect::<Vec<_>>());
        assert_eq!(resolved.len(), 3);
        assert_eq!(resolved[0].1, "wss://example.livekit.cloud");
        assert_eq!(resolved[1].1, "APIkeyone");
        assert_eq!(resolved[2].1, "secretvalue");
        let _ = owned;
    }

    #[test]
    fn a_multi_slot_provider_never_falls_back_to_the_legacy_bare_entry() {
        // The legacy entry is one value that was being copied into all three
        // variables. Reading it here would reinstate exactly that bug, so a
        // three-slot provider reads nothing when its slots are empty even if a
        // bare entry happens to exist.
        let store = OsCredentialStore;
        let id = probe("legacy-split");
        let slots = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"];
        let mut entries: Vec<(&str, Option<&str>)> = vec![(id.as_str(), None)];
        entries.extend(slots.iter().map(|slot| (id.as_str(), Some(*slot))));
        let (_guard, _cleanup) = fresh(&entries);

        store.set(&id, None, "the-one-pasted-value").unwrap();
        let resolved = store.secrets_for_worker(&id, &slots.iter().map(|s| s.to_string()).collect::<Vec<_>>());
        assert!(resolved.is_empty(), "legacy entry must not stand in for {slots:?}");
    }

    #[test]
    fn a_single_slot_provider_does_fall_back_to_the_legacy_entry() {
        // The other half of the rule: this is what makes the rekey a non-event
        // for every provider that only ever had one key.
        let store = OsCredentialStore;
        let id = probe("legacy-single");
        let (_guard, _cleanup) = fresh(&[(id.as_str(), None), (id.as_str(), Some("GROQ_API_KEY"))]);

        store.set(&id, None, "gsk-legacy").unwrap();
        let resolved = store.secrets_for_worker(&id, &["GROQ_API_KEY".to_string()]);
        assert_eq!(resolved, vec![("GROQ_API_KEY".to_string(), "gsk-legacy".to_string())]);
    }
}
