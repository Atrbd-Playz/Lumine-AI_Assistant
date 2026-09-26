//! Hands the worker the credentials held in the OS keyring.
//!
//! ## Why this exists
//!
//! The Providers screen stores a key where the frontend cannot read it back. Until
//! this module existed, the stored key went nowhere: the worker read
//! `agent/.env`, so a user could add a key, see it reported as stored, and watch
//! the session behave exactly as before. That is the same silent split that
//! `LUMINE_CONFIG_PATH` was introduced to fix for the configuration file.
//!
//! ## How the values travel
//!
//! Into the child process's environment, and nowhere else. The value is never
//! logged, never written to disk, never returned to the webview, and never
//! included in a Tauri event. Only the variable *names* and the provider ids are
//! ever printed.
//!
//! ## Why the environment wins over `agent/.env`
//!
//! `agent.py` calls `load_dotenv()` with the default `override=False`, which
//! leaves an already-set variable alone. An injected value therefore takes
//! precedence, which is the behaviour a user expects after replacing a key.

use std::collections::BTreeMap;
use std::process::Command;

use serde_json::Value;

use crate::credentials::CredentialStore;

/// The provider ids to environment variable names, as the catalog defines them.
///
/// The mapping is catalog knowledge — it lives in `agent/providers.py` next to the
/// models it belongs to — so it is read from there rather than duplicated in
/// Rust. A provider added on the Python side needs no change here.
pub fn key_env_by_provider(catalog: &Value) -> BTreeMap<String, Vec<String>> {
    let mut mapping = BTreeMap::new();
    let Some(providers) = catalog.get("providers").and_then(Value::as_array) else {
        return mapping;
    };
    for provider in providers {
        let Some(id) = provider.get("id").and_then(Value::as_str) else {
            continue;
        };
        // A local provider needs no credential, so it has no variable to set.
        if !provider
            .get("requiresKey")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        let names: Vec<String> = provider
            .get("keyEnv")
            .and_then(Value::as_array)
            .map(|values| {
                values
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default();
        if !names.is_empty() {
            mapping.insert(id.to_string(), names);
        }
    }
    mapping
}

/// Set every stored credential the catalog knows a variable for, on `command`.
///
/// Returns the provider ids that were injected, for logging. A provider with no
/// stored key is skipped silently: that is the normal state, and reporting it
/// would bury the ones that matter.
pub fn inject_credentials(
    command: &mut Command,
    catalog: &Value,
    store: &dyn CredentialStore,
) -> Vec<String> {
    let mut injected = Vec::new();
    for (provider_id, names) in key_env_by_provider(catalog) {
        let secret = match store.secret_for_worker(&provider_id) {
            Ok(secret) => secret,
            // No key stored, or the store is unavailable. Either way `agent/.env`
            // remains the source, which is a supported configuration.
            Err(_) => continue,
        };
        if secret.trim().is_empty() {
            continue;
        }
        for name in names {
            command.env(name, secret.trim());
        }
        injected.push(provider_id);
    }
    injected
}

#[cfg(test)]
mod tests {
    use super::*;

    fn catalog_with(keys: Value, requires_key: bool) -> Value {
        serde_json::json!({
            "providers": [
                { "id": "cartesia", "requiresKey": requires_key, "keyEnv": keys }
            ]
        })
    }

    #[test]
    fn the_mapping_comes_from_the_catalog() {
        let catalog = catalog_with(serde_json::json!(["CARTESIA_API_KEY"]), true);
        let mapping = key_env_by_provider(&catalog);
        assert_eq!(
            mapping.get("cartesia"),
            Some(&vec!["CARTESIA_API_KEY".to_string()])
        );
    }

    #[test]
    fn a_local_provider_gets_no_variable() {
        // Silero needs no credential; setting a variable for it would be noise.
        let catalog = catalog_with(serde_json::json!([]), false);
        assert!(key_env_by_provider(&catalog).is_empty());
    }

    #[test]
    fn a_provider_with_no_named_variable_is_skipped() {
        let catalog = catalog_with(serde_json::json!([]), true);
        assert!(key_env_by_provider(&catalog).is_empty());
    }

    #[test]
    fn a_malformed_catalog_yields_nothing_rather_than_panicking() {
        // The worker must still start if the catalog cannot be read; the
        // credentials simply stay in `agent/.env`.
        for bad in [
            serde_json::json!({}),
            serde_json::json!({ "providers": "nope" }),
            serde_json::json!({ "providers": [{}] }),
            serde_json::json!({ "providers": [{ "id": 7 }] }),
        ] {
            assert!(key_env_by_provider(&bad).is_empty(), "{bad}");
        }
    }

    #[test]
    fn every_variable_named_for_a_provider_receives_the_same_secret() {
        // A provider may accept a primary and a fallback variable; both have to
        // be set or the plugin picks whichever it finds first.
        let catalog = catalog_with(serde_json::json!(["PRIMARY_KEY", "FALLBACK_KEY"]), true);
        let mapping = key_env_by_provider(&catalog);
        assert_eq!(mapping.get("cartesia").map(Vec::len), Some(2));
    }

    /// A store that answers from memory, so the injection path can be exercised
    /// without touching the real OS keyring.
    struct FakeStore {
        secrets: BTreeMap<String, String>,
    }

    impl FakeStore {
        fn with(provider: &str, secret: &str) -> Self {
            let mut secrets = BTreeMap::new();
            secrets.insert(provider.to_string(), secret.to_string());
            Self { secrets }
        }

        fn empty() -> Self {
            Self {
                secrets: BTreeMap::new(),
            }
        }
    }

    impl CredentialStore for FakeStore {
        fn set(&self, _: &str, _: &str) -> Result<(), crate::credentials::CredentialError> {
            unimplemented!("the injection path never writes")
        }
        fn delete(&self, _: &str) -> Result<(), crate::credentials::CredentialError> {
            unimplemented!("the injection path never deletes")
        }
        fn status(
            &self,
            _provider: &str,
        ) -> Result<crate::credentials::CredentialStatus, crate::credentials::CredentialError> {
            unimplemented!("the injection path never asks for status")
        }
        fn secret_for_worker(
            &self,
            provider: &str,
        ) -> Result<String, crate::credentials::CredentialError> {
            self.secrets
                .get(provider)
                .cloned()
                .ok_or(crate::credentials::CredentialError::NotFound)
        }
    }

    /// The environment overrides a `Command` will hand to the child.
    ///
    /// `Command` exposes exactly what it will set, which is the thing that
    /// matters: whether the provider plugin will see the key once the worker is
    /// running. No process is spawned, so a key never reaches a test output.
    fn child_env(command: &Command, names: &[&str]) -> BTreeMap<String, String> {
        let mut resolved = BTreeMap::new();
        for name in names {
            let wanted = std::ffi::OsStr::new(name);
            let found = command.get_envs().find(|(key, _)| *key == wanted);
            if let Some((_, Some(value))) = found {
                resolved.insert((*name).to_string(), value.to_string_lossy().into_owned());
            }
        }
        resolved
    }

    #[test]
    fn a_stored_key_lands_on_the_child_process() {
        let catalog = catalog_with(serde_json::json!(["CARTESIA_API_KEY"]), true);
        let mut command = Command::new("never-executed");
        let injected = inject_credentials(&mut command, &catalog, &FakeStore::with("cartesia", "sk-secret"));

        assert_eq!(injected, vec!["cartesia".to_string()]);
        let env = child_env(&command, &["CARTESIA_API_KEY"]);
        assert_eq!(env.get("CARTESIA_API_KEY").map(String::as_str), Some("sk-secret"));
    }

    #[test]
    fn a_provider_with_no_stored_key_is_left_to_the_env_file() {
        let catalog = catalog_with(serde_json::json!(["CARTESIA_API_KEY"]), true);
        let mut command = Command::new("never-executed");
        let injected = inject_credentials(&mut command, &catalog, &FakeStore::empty());

        assert!(injected.is_empty());
        assert!(child_env(&command, &["CARTESIA_API_KEY"]).is_empty());
    }

    #[test]
    fn a_blank_secret_is_not_injected() {
        // A stored but empty value would blank out a working `agent/.env` entry.
        let catalog = catalog_with(serde_json::json!(["CARTESIA_API_KEY"]), true);
        let mut command = Command::new("never-executed");
        let injected = inject_credentials(&mut command, &catalog, &FakeStore::with("cartesia", "   "));
        assert!(injected.is_empty());
        assert!(child_env(&command, &["CARTESIA_API_KEY"]).is_empty());
    }

    #[test]
    fn a_secret_is_trimmed_before_it_is_passed_on() {
        // A pasted key usually carries a trailing newline, which providers reject.
        let catalog = catalog_with(serde_json::json!(["CARTESIA_API_KEY"]), true);
        let mut command = Command::new("never-executed");
        inject_credentials(&mut command, &catalog, &FakeStore::with("cartesia", "sk-secret\n"));
        let env = child_env(&command, &["CARTESIA_API_KEY"]);
        assert_eq!(env.get("CARTESIA_API_KEY").map(String::as_str), Some("sk-secret"));
    }

    #[test]
    fn an_unreadable_store_does_not_stop_the_worker() {
        // A headless Linux box with no Secret Service must still get a worker;
        // its credentials simply stay in `agent/.env`.
        struct BrokenStore;
        impl CredentialStore for BrokenStore {
            fn set(&self, _: &str, _: &str) -> Result<(), crate::credentials::CredentialError> {
                Err(crate::credentials::CredentialError::Failed("no secret service".into()))
            }
            fn delete(&self, _: &str) -> Result<(), crate::credentials::CredentialError> {
                Err(crate::credentials::CredentialError::Failed("no secret service".into()))
            }
            fn status(
                &self,
                _: &str,
            ) -> Result<crate::credentials::CredentialStatus, crate::credentials::CredentialError> {
                Err(crate::credentials::CredentialError::Failed("no secret service".into()))
            }
            fn secret_for_worker(
                &self,
                _: &str,
            ) -> Result<String, crate::credentials::CredentialError> {
                Err(crate::credentials::CredentialError::Failed("no secret service".into()))
            }
        }

        let catalog = catalog_with(serde_json::json!(["CARTESIA_API_KEY"]), true);
        let mut command = Command::new("never-executed");
        let injected = inject_credentials(&mut command, &catalog, &BrokenStore);
        assert!(injected.is_empty());
    }
}
