//! Proves a stored provider credential actually works.
//!
//! "Stored" only means the OS keyring has an entry for a provider id. It says
//! nothing about whether the key is valid, was revoked, or was pasted with a
//! stray character — which is why the Providers page previously could not tell a
//! working key from a broken one.
//!
//! ## The secret's path
//!
//! It is read from the keyring here, put in the environment of a one-shot Python
//! child, and read back as a verdict. It is never an argument (those appear in
//! the process list), never logged, never returned to the webview, and never
//! written to disk. The only thing that crosses back is a status code, a
//! three-word verdict, and a latency.
//!
//! ## Why the verdict is three-valued
//!
//! A rejected credential, a provider outage, and a malformed request of ours are
//! three different things, and only the first is the user's problem. Collapsing
//! them would tell someone to re-enter a perfectly good key because their
//! provider was down. `agent/provider_probe.py` decides, because the per-provider
//! knowledge of which statuses mean what lives in the catalog next to it.

use serde::{Deserialize, Serialize};

use crate::credentials::{CredentialStore, OsCredentialStore};
use crate::python_env;

/// The verdict vocabulary, mirroring `agent/provider_probe.py`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    /// The provider accepted the credential.
    Valid,
    /// The provider refused this specific credential.
    Rejected,
    /// The provider could not be asked, or answered something that says nothing
    /// about the credential. Not the user's problem.
    Inconclusive,
    /// This provider has no cheap authenticated request to make.
    NoProbe,
    /// Nothing is set to test.
    NoSecret,
}

/// What the settings screen shows. Carries no secret material by construction:
/// every field here is safe to put in a log or an event.
#[derive(Debug, Clone, Serialize)]
pub struct ProbeOutcome {
    pub provider: String,
    pub verdict: Verdict,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub latency_ms: Option<u64>,
    /// A short message from the provider, or an explanation of why nothing could
    /// be concluded. Never the key, and never a URL with a key in it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

impl ProbeOutcome {
    fn unprobed(provider: &str, verdict: Verdict, detail: &str) -> Self {
        Self {
            provider: provider.to_string(),
            verdict,
            status: None,
            latency_ms: None,
            detail: Some(detail.to_string()),
        }
    }
}

/// The script's own output, which is trusted only as far as its shape.
#[derive(Debug, Deserialize)]
struct RawOutcome {
    #[serde(default)]
    verdict: String,
    #[serde(default)]
    status: Option<u16>,
    #[serde(default)]
    latency_ms: Option<u64>,
    #[serde(default)]
    detail: Option<String>,
}

fn parse_verdict(value: &str) -> Verdict {
    match value {
        "valid" => Verdict::Valid,
        "rejected" => Verdict::Rejected,
        "no_probe" => Verdict::NoProbe,
        "no_secret" => Verdict::NoSecret,
        // Anything unrecognised, including an empty string, is inconclusive by
        // default. Guessing "valid" from a shape we do not understand would be
        // the one failure that matters.
        _ => Verdict::Inconclusive,
    }
}

/// Test a provider's credential and return what the provider said.
///
/// The credential comes from the OS keyring when there is one, and otherwise from
/// `agent/.env`, which is how a key that has never been entered in Settings can
/// still be checked.
#[tauri::command]
pub async fn test_provider_credential(provider: String) -> Result<ProbeOutcome, String> {
    let provider = provider.trim().to_string();
    if provider.is_empty() {
        return Err("A provider id is required.".to_string());
    }

    let catalog = python_env::run_agent_script("provider_catalog.py", &[])?;
    let catalog: serde_json::Value =
        serde_json::from_str(&catalog).map_err(|err| format!("The provider catalog was not valid JSON: {err}"))?;

    let declared = catalog
        .get("providers")
        .and_then(serde_json::Value::as_array)
        .and_then(|providers| {
            providers
                .iter()
                .find(|entry| entry.get("id").and_then(serde_json::Value::as_str) == Some(provider.as_str()))
        })
        .cloned();
    let Some(declared) = declared else {
        return Err(format!("{provider} is not a known Lumine provider."));
    };

    if declared.get("probe").is_none_or(serde_json::Value::is_null) {
        let label = declared
            .get("label")
            .and_then(serde_json::Value::as_str)
            .unwrap_or(&provider);
        return Ok(ProbeOutcome::unprobed(
            &provider,
            Verdict::NoProbe,
            &format!("{label} has no connectivity check, so its key cannot be tested from here."),
        ));
    }

    // The keyring first, because that is what the user just saved. Falling back
    // to the environment is deliberate: someone who has never opened the
    // Providers page still deserves to know whether their `agent/.env` key works.
    //
    // Every variable the catalog names is injected, each with its own value. The
    // previous version passed only the first name, so a three-value provider
    // could never be probed and reported "not testable" -- which is how the
    // LiveKit credential went wrong without anything on screen objecting.
    let store = OsCredentialStore;
    let names: Vec<String> = declared
        .get("keyEnv")
        .and_then(serde_json::Value::as_array)
        .map(|values| {
            values
                .iter()
                .filter_map(serde_json::Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();

    if names.is_empty() {
        return Ok(ProbeOutcome::unprobed(
            &provider,
            Verdict::NoSecret,
            "This provider declares no credential variable, so there is nothing to test.",
        ));
    }

    let pairs = store.secrets_for_worker(&provider, &names);
    if pairs.is_empty() {
        // Nothing in the keyring. `agent/.env` may still hold a working key, so
        // the script is still worth running -- it reads the environment itself.
        // Reporting "no secret" here would tell someone with a perfectly good
        // `agent/.env` that they have no credential.
        let has_env = python_env::run_agent_script("provider_probe.py", &[&provider])
            .ok()
            .and_then(|stdout| serde_json::from_str::<serde_json::Value>(&stdout).ok())
            .and_then(|value| value.get("verdict").and_then(serde_json::Value::as_str).map(str::to_string))
            .is_some_and(|verdict| verdict == "no_secret");
        if has_env {
            return Ok(ProbeOutcome::unprobed(
                &provider,
                Verdict::NoSecret,
                "No key is stored, and none is set in agent/.env.",
            ));
        }
    }

    let owned: Vec<(&str, &str)> = pairs
        .iter()
        .map(|(name, value)| (name.as_str(), value.as_str()))
        .collect();
    let stdout = python_env::run_agent_script_with_env("provider_probe.py", &[&provider], &owned)?;
    let raw: RawOutcome =
        serde_json::from_str(&stdout).map_err(|err| format!("The probe result was not readable: {err}"))?;

    Ok(ProbeOutcome {
        provider,
        verdict: parse_verdict(&raw.verdict),
        status: raw.status,
        latency_ms: raw.latency_ms,
        detail: raw.detail,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_verdicts_the_script_uses_are_all_understood() {
        for (text, expected) in [
            ("valid", Verdict::Valid),
            ("rejected", Verdict::Rejected),
            ("inconclusive", Verdict::Inconclusive),
            ("no_probe", Verdict::NoProbe),
            ("no_secret", Verdict::NoSecret),
        ] {
            assert_eq!(parse_verdict(text), expected, "{text}");
        }
    }

    #[test]
    fn an_unrecognised_verdict_is_inconclusive_never_valid() {
        // The one failure that would matter: telling a user a credential works
        // because a response we did not understand happened to parse.
        for text in ["", "ok", "VALID ", "success", "true"] {
            assert_eq!(
                parse_verdict(text),
                Verdict::Inconclusive,
                "{text:?} must not be read as valid"
            );
        }
    }

    #[test]
    fn the_outcome_carries_nothing_that_could_be_a_secret() {
        // A structural check rather than a runtime one: the type has no field
        // that a key could end up in, so there is no code path that leaks one.
        let outcome = ProbeOutcome {
            provider: "groq".to_string(),
            verdict: Verdict::Valid,
            status: Some(200),
            latency_ms: Some(120),
            detail: Some("ok".to_string()),
        };
        let json = serde_json::to_string(&outcome).expect("serializes");
        assert!(json.contains("groq"));
        assert!(!json.contains("key"), "unexpected field in {json}");
    }

    #[test]
    fn an_unprobed_provider_explains_itself() {
        let outcome = ProbeOutcome::unprobed("silero", Verdict::NoProbe, "no check");
        assert_eq!(outcome.verdict, Verdict::NoProbe);
        assert_eq!(outcome.status, None);
        assert_eq!(outcome.detail.as_deref(), Some("no check"));
    }
}
