//! Credential storage for the AI Control Center.
//!
//! **Status: spike.** The backend choice is proven here, but nothing is wired
//! into the Tauri command surface yet, so the app's behaviour is unchanged. See
//! `docs/ai-control-center.md`.
//!
//! Two rules shape this module:
//!
//! 1. **A credential is never returned to the webview.** The trait deliberately
//!    has no "give me the plaintext" method for callers outside the worker
//!    plumbing. `status` reports whether a key exists and its last four
//!    characters, which is enough for a settings screen.
//! 2. **Local providers need no credential.** Silero runs on-device; asking the
//!    OS keyring to store an empty string for it would be noise.

use std::fmt;

/// Service name used for every Lumine credential in the OS store.
const SERVICE: &str = "com.art.lumine-ui";

/// What the settings UI is allowed to know about a stored credential.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CredentialStatus {
    pub present: bool,
    /// At most the final four characters, for recognising a key in the UI.
    pub last4: Option<String>,
    pub updated_at: Option<String>,
}

/// Errors surfaced to the command layer. Messages are safe to show a user and
/// never contain key material.
#[derive(Debug)]
pub enum CredentialError {
    /// The OS keyring is unavailable (no Secret Service on a headless Linux box,
    /// for example). The app can fall back to `agent/.env`.
    Unavailable(String),
    /// No credential is stored for this provider.
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
    fn set(&self, provider_id: &str, secret: &str) -> Result<(), CredentialError>;
    fn delete(&self, provider_id: &str) -> Result<(), CredentialError>;
    /// Redacted status only. Implementations must not expose the value.
    fn status(&self, provider_id: &str) -> Result<CredentialStatus, CredentialError>;
    /// For worker spawn and provider tests. Callers must not pass the result to
    /// the webview.
    fn secret_for_worker(&self, provider_id: &str) -> Result<String, CredentialError>;
}

/// OS-native implementation backed by the `keyring` crate.
pub struct OsCredentialStore;

fn entry(provider_id: &str) -> Result<keyring::Entry, CredentialError> {
    if provider_id.trim().is_empty() {
        return Err(CredentialError::Failed("empty provider id".into()));
    }
    keyring::Entry::new(SERVICE, provider_id)
        .map_err(|err| CredentialError::Unavailable(err.to_string()))
}

fn map_get_error(provider_id: &str, err: keyring::Error) -> CredentialError {
    match err {
        keyring::Error::NoEntry => CredentialError::NotFound,
        other => CredentialError::Failed(format!("{provider_id}: {other}")),
    }
}

impl CredentialStore for OsCredentialStore {
    fn set(&self, provider_id: &str, secret: &str) -> Result<(), CredentialError> {
        if secret.is_empty() {
            // Storing an empty secret would shadow a real key with a blank one.
            return self.delete(provider_id);
        }
        entry(provider_id)?
            .set_password(secret)
            .map_err(|err| CredentialError::Failed(format!("{provider_id}: {err}")))
    }

    fn delete(&self, provider_id: &str) -> Result<(), CredentialError> {
        match entry(provider_id)?.delete_credential() {
            Ok(()) => Ok(()),
            // Deleting something that was never there is the desired end state.
            Err(keyring::Error::NoEntry) => Ok(()),
            Err(err) => Err(CredentialError::Failed(format!("{provider_id}: {err}"))),
        }
    }

    fn status(&self, provider_id: &str) -> Result<CredentialStatus, CredentialError> {
        match self.secret_for_worker(provider_id) {
            Ok(secret) => {
                let tail: String = secret.chars().rev().take(4).collect::<Vec<_>>().into_iter().rev().collect();
                Ok(CredentialStatus {
                    present: true,
                    last4: (!tail.is_empty()).then_some(tail),
                    updated_at: None,
                })
            }
            // A missing key is a normal state, not an error worth surfacing.
            Err(CredentialError::NotFound) => Ok(CredentialStatus {
                present: false,
                last4: None,
                updated_at: None,
            }),
            Err(other) => Err(other),
        }
    }

    fn secret_for_worker(&self, provider_id: &str) -> Result<String, CredentialError> {
        entry(provider_id)?
            .get_password()
            .map_err(|err| map_get_error(provider_id, err))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Mutex, MutexGuard, OnceLock};

    /// Serializes access to the OS credential store.
    ///
    /// The Windows Credential Manager is a shared system resource and does not
    /// reliably serve several threads hammering it in a short window: entries
    /// written by one thread intermittently read back as absent for another. The
    /// production code is unaffected — one process talks to the store from one
    /// place at a time — but parallel `cargo test` does exactly that, so the
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
    struct Cleanup(String);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = OsCredentialStore.delete(&self.0);
        }
    }

    /// Start from a known-empty state, holding the store lock for the test body.
    ///
    /// `Cleanup` cannot run if a previous test process was killed mid-test, which
    /// would leave a credential behind and make a later run fail on "probe must
    /// start empty". Clearing first makes these tests idempotent across runs.
    fn fresh(id: &str) -> (MutexGuard<'static, ()>, Cleanup) {
        let guard = os_store_guard();
        let _ = OsCredentialStore.delete(id);
        (guard, Cleanup(id.to_string()))
    }

    #[test]
    fn round_trips_and_reports_only_a_redacted_tail() {
        let store = OsCredentialStore;
        let id = probe("roundtrip");
        let (_guard, _cleanup) = fresh(&id);
        let secret = "sk-spike-not-a-real-key-9f3a";

        assert!(!store.status(&id).expect("status").present, "probe must start empty");

        store.set(&id, secret).expect("set should succeed");
        assert_eq!(store.secret_for_worker(&id).expect("get"), secret);

        let status = store.status(&id).expect("status");
        assert!(status.present);
        assert_eq!(status.last4.as_deref(), Some("9f3a"));
        // The redaction contract: the full value must not be reconstructible.
        assert!(!format!("{status:?}").contains(secret));

        store.delete(&id).expect("delete");
        assert!(!store.status(&id).expect("status").present);
    }

    #[test]
    fn deleting_an_absent_credential_is_not_an_error() {
        let id = probe("absent");
        let (_guard, _cleanup) = fresh(&id);
        OsCredentialStore.delete(&id).expect("deleting nothing should succeed");
    }

    #[test]
    fn setting_an_empty_secret_clears_the_stored_value() {
        let store = OsCredentialStore;
        let id = probe("empty");
        let (_guard, _cleanup) = fresh(&id);
        store.set(&id, "temporary").expect("set");
        store.set(&id, "").expect("empty set should clear");
        assert!(!store.status(&id).expect("status").present);
    }

    #[test]
    fn a_short_secret_still_reports_a_tail() {
        let store = OsCredentialStore;
        let id = probe("short");
        let (_guard, _cleanup) = fresh(&id);
        store.set(&id, "abc").expect("set");
        let status = store.status(&id).expect("status");
        assert!(status.present);
        assert_eq!(status.last4.as_deref(), Some("abc"));
    }

    #[test]
    fn a_secret_replaced_by_another_reads_back_as_the_new_one() {
        // Guards against a stale-cache style bug: reporting `present` for a value
        // that is no longer the one stored.
        let store = OsCredentialStore;
        let id = probe("replace");
        let (_guard, _cleanup) = fresh(&id);
        store.set(&id, "first-value").expect("first set");
        store.set(&id, "second-value").expect("second set");
        assert_eq!(store.secret_for_worker(&id).expect("get"), "second-value");
        assert_eq!(store.status(&id).expect("status").last4.as_deref(), Some("alue"));
    }
}
