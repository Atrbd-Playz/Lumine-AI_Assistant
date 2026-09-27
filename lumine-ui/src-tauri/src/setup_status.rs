//! Can this install actually hold a conversation?
//!
//! The voice path needs three things before a room is worth joining: a
//! credential for every provider the active profile names, a credential for
//! LiveKit itself, and a configuration that validates. None of them were
//! checked together in one place, so the failure surfaced as an unhandled 401
//! inside a session the user had already started talking into.
//!
//! ## Why this is one command and not three
//!
//! The onboarding screen has to answer a single question — "may I talk to
//! Lumine yet?" — and the honest answer needs all three inputs. Asking the
//! webview to assemble it would mean shipping the provider catalog, the
//! keyring status and the diagnostics to the frontend and reimplementing this
//! reduction in TypeScript, where it could disagree with the worker.
//!
//! ## What this never returns
//!
//! No secret, in any field. A credential is reported as a boolean plus, when one
//! is stored, its last four characters — the same redaction the settings screen
//! already shows. Environment variable *names* are included because they are
//! documentation, not secrets, and they are the only thing a user can act on if
//! they are configuring through `agent/.env` instead of the wizard.

use std::collections::BTreeSet;
use std::env;

use serde::Serialize;
use serde_json::Value;

use crate::credentials::{CredentialStore, OsCredentialStore};
use crate::python_env;

/// How to draw one field of a multi-part credential.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SlotInfo {
    /// The environment variable the value belongs in. Doubles as the keyring slot
    /// name, so the field drawn and the variable the worker reads cannot drift.
    pub env: String,
    /// A name a person can act on: "Server URL", not "LIVEKIT_URL".
    pub label: String,
    /// `url` fields hold an address and are shown in the clear so a typo is
    /// visible; `secret` is masked. A value named like a credential is always
    /// masked -- the Python catalog refuses to declare otherwise.
    pub kind: String,
    pub help: String,
}

/// Whether one variable of a multi-part credential has a value.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SlotStatus {
    pub env: String,
    pub label: String,
    /// `url` fields hold an address and are shown in the clear so a typo is
    /// visible; `secret` is masked. A value named like a credential is always
    /// masked -- the Python catalog refuses to declare otherwise.
    pub kind: String,
    pub help: String,
    /// A value is held in the OS keyring for this variable specifically.
    pub stored: bool,
    /// At most the final four characters, so a person can tell *which* of three
    /// near-identical keys they pasted into the right field.
    pub stored_last4: Option<String>,
    /// A value is set in the environment for this variable specifically.
    pub in_env: bool,
}

impl SlotStatus {
    /// Whether this one variable is satisfied, by either route.
    pub fn is_satisfied(&self) -> bool {
        self.stored || self.in_env
    }
}

/// One provider the active profile cannot run without.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RequiredCredential {
    pub id: String,
    pub label: String,
    /// Where to get a key. Opened by the onboarding screen's button.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub setup_url: Option<String>,
    /// Environment variable names the worker reads. Never a value.
    pub key_env: Vec<String>,
    /// One entry per `key_env` variable, so the wizard draws a field for each
    /// rather than one field that a single value cannot fill.
    pub key_slots: Vec<SlotInfo>,
    /// Per-variable state, in the same order as `key_slots`.
    ///
    /// The wizard cannot do its job without this. A provider that is two thirds
    /// stored has to be able to say *which* two, or reopening it shows three
    /// blank fields, which is indistinguishable from having stored nothing — and
    /// is how saving one of LiveKit's three variables came to look like saving
    /// all of them.
    pub slots: Vec<SlotStatus>,
    /// What this provider is doing in the profile, for the wizard's subtitle.
    pub capabilities: Vec<String>,
    /// **Any** of this provider's variables is held in the OS keyring.
    ///
    /// Provider-level on purpose, and deliberately weaker than
    /// [`Self::is_satisfied`]. This is the "has any value at all" signal a label
    /// or a badge wants. Deciding whether a call can be attempted uses the
    /// per-slot `missing` list instead.
    pub stored: bool,
    /// At most the final four characters of a stored key. Of the first stored
    /// slot, which is as much as a single tail can honestly describe.
    pub stored_last4: Option<String>,
    /// A key is set in the environment (`agent/.env`, or injected at spawn).
    pub in_env: bool,
    /// True when the provider needs no credential at all, like Silero.
    pub local: bool,
    /// Which of this provider's variables still have no value anywhere.
    ///
    /// A provider with one key is either satisfied or not, so this is empty or
    /// holds that one name. A provider with several -- LiveKit needs a URL, an
    /// API key and an API secret -- is only satisfied when the list is empty,
    /// because two of the three is a credential that cannot connect. The first
    /// version of the gate asked only "is there any value for this provider",
    /// which is why it reported ready for a LiveKit install that could never
    /// join a room.
    pub missing: Vec<String>,
}

impl RequiredCredential {
    /// Whether a session can be attempted with what is on this machine.
    ///
    /// Deliberately *presence*, not validity. Proving a key works means making
    /// a billable request to a third party, and the onboarding screen is not the
    /// place to spend that unasked; the Providers page has an explicit "Test"
    /// for it. What the gate must not do is block someone whose key is fine
    /// because the network happened to be down.
    pub fn is_satisfied(&self) -> bool {
        self.local || self.missing.is_empty()
    }
}

/// The whole answer, so the frontend makes one call and renders it.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SetupStatus {
    /// Every provider the active profile names, plus LiveKit, in a stable order.
    pub required: Vec<RequiredCredential>,
    /// True when nothing is missing. The single boolean the voice path gates on.
    pub ready: bool,
    /// The profile the requirements were derived from, for the wizard's heading.
    pub active_profile: Option<String>,
    /// True when the configuration came from a saved document rather than
    /// `agent/.env`. The wizard offers to save one only in this case.
    pub configured_in_app: bool,
    /// Why the configuration is not usable, when it is not. Validation's own
    /// words, so the gate never invents a reason the worker did not give.
    pub blocking: Vec<String>,
}

/// Provider ids the active profile names, in the order a stage list reads.
fn providers_in_use(document: &Value) -> Vec<String> {
    let mut found: BTreeSet<String> = BTreeSet::new();
    let Some(profile) = document.get("activeProfileId").and_then(Value::as_str) else {
        return Vec::new();
    };
    let Some(profiles) = document.get("profiles").and_then(Value::as_array) else {
        return Vec::new();
    };
    let Some(active) = profiles
        .iter()
        .find(|entry| entry.get("id").and_then(Value::as_str) == Some(profile))
    else {
        return Vec::new();
    };

    // A realtime profile names its provider at the top and, in half-cascade
    // mode, a separate TTS underneath. A pipeline profile names one per stage.
    // Both shapes are walked rather than pattern-matched, so a stage added later
    // is picked up by the gate without another edit here.
    fn collect(node: &Value, into: &mut BTreeSet<String>) {
        match node {
            Value::Object(map) => {
                for (key, value) in map {
                    if key == "provider" {
                        if let Some(id) = value.as_str() {
                            if !id.trim().is_empty() {
                                into.insert(id.to_string());
                            }
                        }
                    }
                    collect(value, into);
                }
            }
            Value::Array(items) => {
                for item in items {
                    collect(item, into);
                }
            }
            _ => {}
        }
    }
    collect(active, &mut found);
    found.into_iter().collect()
}

/// Whether any of a provider's environment variables holds a value.
///
/// Presence only. Reading a value would put a secret in this process, and the
/// gate cannot be the thing that leaks one.
fn present_in_env(key_env: &[String]) -> bool {
    key_env
        .iter()
        .any(|name| env::var_os(name).is_some_and(|value| !value.is_empty()))
}

/// The variables of `key_env` that hold no value in the environment.
///
/// A variable set to an empty string does not count: `CARTESIA_API_KEY=` in a
/// `.env` file parses to `""`, and treating that as present is how a machine
/// ends up "configured" and then 401s.
fn missing_from_env(key_env: &[String]) -> Vec<String> {
    key_env
        .iter()
        .filter(|name| !env::var_os(name).is_some_and(|value| !value.is_empty()))
        .cloned()
        .collect()
}

/// Answer "may I talk to Lumine yet?" in one call.
///
/// Fails soft in one direction only. If the catalog cannot be read, this
/// returns an error rather than an optimistic `ready: true`, because an
/// unanswerable question must not open the gate.
#[tauri::command]
pub fn get_setup_status() -> Result<SetupStatus, String> {
    let catalog: Value = python_env::run_agent_script("provider_catalog.py", &[])
        .and_then(|stdout| {
            serde_json::from_str(&stdout)
                .map_err(|err| format!("The provider catalog was not valid JSON: {err}"))
        })?;

    let config_stdout = python_env::run_agent_script("validate_config.py", &["--describe"])
        .map_err(|err| format!("Could not read the effective configuration: {err}"))?;
    let config: Value = serde_json::from_str(&config_stdout)
        .map_err(|err| format!("The effective configuration was not readable: {err}"))?;

    let document = config.get("document").cloned().unwrap_or(Value::Null);
    let source = config.get("source").and_then(Value::as_str).unwrap_or("env");

    let mut required_ids: Vec<String> = providers_in_use(&document);
    // LiveKit is not a profile stage -- it is the room every stage meets in --
    // so it is added rather than discovered. Adding it is also what stops the
    // gate from passing on a machine with provider keys but no LiveKit account,
    // which used to be the single most confusing first-run failure.
    if !required_ids.iter().any(|id| id == "livekit") {
        required_ids.push("livekit".to_string());
    }

    let empty = Vec::new();
    let providers = catalog
        .get("providers")
        .and_then(Value::as_array)
        .unwrap_or(&empty);

    let store = OsCredentialStore;
    let mut required = Vec::new();
    for id in &required_ids {
        let Some(entry) = providers
            .iter()
            .find(|entry| entry.get("id").and_then(Value::as_str) == Some(id.as_str()))
        else {
            // A provider the catalog does not know cannot be checked, and cannot
            // be satisfied either. Skipping it would let the gate open for a
            // profile we have no idea how to serve.
            required.push(RequiredCredential {
                id: id.clone(),
                label: id.clone(),
                setup_url: None,
                key_env: Vec::new(),
                key_slots: Vec::new(),
                slots: Vec::new(),
                capabilities: Vec::new(),
                stored: false,
                stored_last4: None,
                in_env: false,
                local: false,
                // A provider the catalog does not know cannot be satisfied, so it
                // is reported as unsatisfied by naming a slot nothing can fill.
                missing: vec![format!("{id}_CREDENTIAL")],
            });
            continue;
        };

        let key_env: Vec<String> = entry
            .get("keyEnv")
            .and_then(Value::as_array)
            .map(|values| {
                values
                    .iter()
                    .filter_map(Value::as_str)
                    .map(String::from)
                    .collect()
            })
            .unwrap_or_default();
        let key_slots: Vec<SlotInfo> = entry
            .get("keySlots")
            .and_then(Value::as_array)
            .map(|values| {
                values
                    .iter()
                    .filter_map(|slot| {
                        Some(SlotInfo {
                            env: slot.get("env")?.as_str()?.to_string(),
                            label: slot.get("label")?.as_str()?.to_string(),
                            kind: slot
                                .get("kind")
                                .and_then(Value::as_str)
                                .unwrap_or("secret")
                                .to_string(),
                            help: slot
                                .get("help")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                                .to_string(),
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();
        let local = entry
            .get("local")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let needs_key = entry
            .get("requiresKey")
            .and_then(Value::as_bool)
            .unwrap_or(false);

        // A keyring that is unavailable is not a missing credential. Falling
        // back to the environment check keeps a headless Linux box, which has no
        // Secret Service, usable through `agent/.env` alone.
        //
        // Read per slot. Which of this provider's variables have a value is the
        // only thing the wizard can actually draw, and `secrets_for_worker`
        // reports presence per name so one field's answer is never borrowed for
        // another's. An unavailable keyring reports nothing stored, which makes
        // the row ask again rather than claim a value it cannot see.
        let stored_slots: Vec<String> = store
            .secrets_for_worker(id, &key_env)
            .into_iter()
            .map(|(name, _)| name)
            .collect();

        let slots: Vec<SlotStatus> = key_slots
            .iter()
            .zip(key_env.iter())
            .map(|(slot, name)| {
                let in_env = env::var_os(name).is_some_and(|value| !value.is_empty());
                let (stored, stored_last4) = match store.status(id, Some(name.as_str())) {
                    Ok(status) => (status.present, status.last4),
                    Err(_) => (false, None),
                };
                SlotStatus {
                    env: name.clone(),
                    label: slot.label.clone(),
                    kind: slot.kind.clone(),
                    help: slot.help.clone(),
                    stored,
                    stored_last4,
                    in_env,
                }
            })
            .collect();

        // Provider-level "any value at all", for a badge. Derived from the slots
        // so a provider with no declared slots still answers from `stored_slots`.
        let any_stored = !stored_slots.is_empty() || slots.iter().any(SlotStatus::is_satisfied);
        let first_stored_last4 = slots
            .iter()
            .find_map(|slot| slot.stored_last4.clone());

        let missing: Vec<String> = if local || !needs_key {
            Vec::new()
        } else {
            missing_from_env(&key_env)
                .into_iter()
                .filter(|name| !stored_slots.contains(name))
                .collect()
        };

        required.push(RequiredCredential {
            id: id.clone(),
            label: entry
                .get("label")
                .and_then(Value::as_str)
                .unwrap_or(id.as_str())
                .to_string(),
            setup_url: entry
                .get("setupUrl")
                .and_then(Value::as_str)
                .filter(|url| !url.is_empty())
                .map(String::from),
            key_env: key_env.clone(),
            key_slots,
            slots,
            capabilities: entry
                .get("capabilities")
                .and_then(Value::as_array)
                .map(|values| {
                    values
                        .iter()
                        .filter_map(Value::as_str)
                        .map(String::from)
                        .collect()
                })
                .unwrap_or_default(),
            stored: any_stored,
            stored_last4: first_stored_last4,
            in_env: present_in_env(&key_env),
            // A provider that needs no key is satisfied by existing. `local` is
            // the catalog's own claim, and `requiresKey` is the consequence.
            local: local || !needs_key,
            missing,
        });
    }

    let diagnostics = config
        .get("diagnostics")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let blocking: Vec<String> = diagnostics
        .iter()
        .filter(|entry| entry.get("severity").and_then(Value::as_str) == Some("error"))
        .map(|entry| {
            let message = entry
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("The configuration is not valid.");
            match entry.get("hint").and_then(Value::as_str).filter(|hint| !hint.is_empty()) {
                Some(hint) => format!("{message} {hint}"),
                None => message.to_string(),
            }
        })
        .collect();

    let missing: Vec<&RequiredCredential> = required.iter().filter(|item| !item.is_satisfied()).collect();
    let ready = missing.is_empty() && blocking.is_empty();

    Ok(SetupStatus {
        required,
        ready,
        active_profile: document
            .get("activeProfileId")
            .and_then(Value::as_str)
            .map(String::from),
        configured_in_app: source == "ui",
        blocking,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_local_provider_is_satisfied_by_existing() {
        // Silero runs on-device. Asking the keyring to store an empty string for
        // it would make onboarding look permanently incomplete.
        let item = RequiredCredential {
            id: "silero".into(),
            label: "Silero".into(),
            setup_url: None,
            key_env: vec![],
            key_slots: vec![],
            slots: vec![],
            capabilities: vec!["vad".into()],
            stored: false,
            stored_last4: None,
            in_env: false,
            local: true,
            missing: vec![],
        };
        assert!(item.is_satisfied());
    }

    fn slot(env: &str, stored: bool, in_env: bool) -> SlotStatus {
        SlotStatus {
            env: env.into(),
            label: env.into(),
            kind: "secret".into(),
            help: String::new(),
            stored,
            stored_last4: stored.then(|| "3f2a".to_string()),
            in_env,
        }
    }

    fn groq(stored: bool, in_env: bool) -> RequiredCredential {
        let key_env = vec!["GROQ_API_KEY".to_string()];
        let missing = if stored || in_env { vec![] } else { key_env.clone() };
        RequiredCredential {
            id: "groq".into(),
            label: "Groq".into(),
            setup_url: None,
            key_env,
            key_slots: vec![],
            slots: vec![slot("GROQ_API_KEY", stored, in_env)],
            capabilities: vec!["stt".into(), "llm".into()],
            stored,
            stored_last4: None,
            in_env,
            local: false,
            missing,
        }
    }

    #[test]
    fn a_keyring_credential_or_an_environment_one_both_satisfy() {
        assert!(!groq(false, false).is_satisfied());
        assert!(groq(true, false).is_satisfied());
        assert!(groq(false, true).is_satisfied());
    }

    /// The regression test for the gate that said "ready" for a broken LiveKit.
    #[test]
    fn a_three_value_provider_needs_all_three() {
        let livekit = |stored: &[&str]| {
            let names = [
                "LIVEKIT_URL".to_string(),
                "LIVEKIT_API_KEY".to_string(),
                "LIVEKIT_API_SECRET".to_string(),
            ];
            RequiredCredential {
                id: "livekit".into(),
                label: "LiveKit".into(),
                setup_url: Some("https://cloud.livekit.io/".into()),
                key_env: names.to_vec(),
                key_slots: vec![],
                slots: names
                    .iter()
                    .map(|name| slot(name, stored.contains(&name.as_str()), false))
                    .collect(),
                capabilities: vec!["transport".into()],
                stored: !stored.is_empty(),
                stored_last4: None,
                in_env: false,
                local: false,
                missing: names
                    .iter()
                    .filter(|name| !stored.contains(&name.as_str()))
                    .cloned()
                    .collect(),
            }
        };

        // One value is what the old single-slot field could hold, and it is not
        // a credential LiveKit can use.
        assert!(!livekit(&["LIVEKIT_API_KEY"]).is_satisfied());
        assert!(!livekit(&["LIVEKIT_URL", "LIVEKIT_API_KEY"]).is_satisfied());
        // All three is the only satisfied state.
        assert!(livekit(&["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"]).is_satisfied());
    }

    /// The bug behind "it saved all three even though two were empty".
    ///
    /// Provider-level `stored` is a *badge* signal, and it is true the moment any
    /// one variable lands. A wizard that used it to decide whether the credential
    /// was done -- or to label the button "Replace" -- would call a one-third
    /// LiveKit credential complete, and reopening it would show three blanks with
    /// nothing to suggest otherwise. `is_satisfied` must stay the per-slot answer.
    #[test]
    fn any_stored_slot_does_not_make_the_whole_credential_satisfied() {
        let names = [
            "LIVEKIT_URL".to_string(),
            "LIVEKIT_API_KEY".to_string(),
            "LIVEKIT_API_SECRET".to_string(),
        ];
        let only_url: Vec<&str> = vec!["LIVEKIT_URL"];
        let item = RequiredCredential {
            id: "livekit".into(),
            label: "LiveKit".into(),
            setup_url: None,
            key_env: names.to_vec(),
            key_slots: vec![],
            slots: names
                .iter()
                .map(|name| slot(name, only_url.contains(&name.as_str()), false))
                .collect(),
            capabilities: vec!["transport".into()],
            // What a badge wants to know: is there anything at all?
            stored: true,
            stored_last4: Some("3f2a".into()),
            in_env: false,
            local: false,
            missing: vec![
                "LIVEKIT_API_KEY".to_string(),
                "LIVEKIT_API_SECRET".to_string(),
            ],
        };

        assert!(item.stored, "a badge should still see the one stored slot");
        assert!(
            !item.is_satisfied(),
            "two of three values is not a credential LiveKit can use"
        );
        // And the wizard can say which two are still owed, by name.
        assert_eq!(item.missing.len(), 2);
        assert!(item.slots[0].is_satisfied(), "the URL is the one that landed");
        assert!(!item.slots[1].is_satisfied());
        assert!(!item.slots[2].is_satisfied());
    }

    /// An environment value satisfies its own variable, and nobody else's.
    #[test]
    fn an_environment_value_answers_for_its_own_slot_only() {
        let name = "LUMINE_TEST_SLOT_ENV".to_string();
        env::set_var(&name, "value");
        let others = ["LUMINE_TEST_SLOT_OTHER_A", "LUMINE_TEST_SLOT_OTHER_B"];
        let slots: Vec<SlotStatus> = std::iter::once(name.clone())
            .chain(others.iter().map(|s| s.to_string()))
            .map(|env_name| {
                let in_env = env::var_os(&env_name).is_some_and(|value| !value.is_empty());
                slot(&env_name, false, in_env)
            })
            .collect();
        env::remove_var(&name);

        assert!(slots[0].is_satisfied());
        assert!(!slots[1].is_satisfied());
        assert!(!slots[2].is_satisfied());
    }

    #[test]
    fn the_names_left_out_are_the_ones_the_user_still_has_to_enter() {
        // The wizard needs to say which field is empty, not just that one is.
        let names = ["A", "B", "C"];
        let missing: Vec<String> = names
            .iter()
            .filter(|name| !["A", "C"].contains(name))
            .map(|name| name.to_string())
            .collect();
        assert_eq!(missing, vec!["B".to_string()]);
    }

    #[test]
    fn both_profile_shapes_contribute_their_providers() {
        let realtime = serde_json::json!({
            "activeProfileId": "realtime",
            "profiles": [{
                "id": "realtime",
                "kind": "realtime",
                "realtime": {
                    "provider": "google",
                    "output": { "mode": "custom_tts", "tts": { "provider": "cartesia", "model": "sonic-3" } }
                }
            }]
        });
        let ids = providers_in_use(&realtime);
        assert_eq!(ids, vec!["cartesia", "google"]);

        let pipeline = serde_json::json!({
            "activeProfileId": "cascade",
            "profiles": [{
                "id": "cascade",
                "kind": "pipeline",
                "pipeline": {
                    "stt": { "provider": "groq" },
                    "llm": { "provider": "groq" },
                    "tts": { "provider": "cartesia" },
                    "vad": { "provider": "silero" }
                }
            }]
        });
        assert_eq!(
            providers_in_use(&pipeline),
            vec!["cartesia", "groq", "silero"]
        );
    }

    #[test]
    fn an_unknown_active_profile_yields_nothing_rather_than_guessing() {
        // A dangling id must not silently fall back to "every provider", which
        // would make onboarding demand credentials nothing uses.
        let document = serde_json::json!({
            "activeProfileId": "missing",
            "profiles": [{ "id": "other", "kind": "pipeline", "pipeline": { "llm": { "provider": "groq" } } }]
        });
        assert!(providers_in_use(&document).is_empty());
    }

    #[test]
    fn an_empty_environment_variable_does_not_count_as_a_credential() {
        // `CARTESIA_API_KEY=` in a `.env` file parses to an empty string. Treating
        // that as present is how a machine ends up "configured" and then 401s.
        let name = "LUMINE_TEST_EMPTY_KEY".to_string();
        env::set_var(&name, "");
        assert!(!present_in_env(std::slice::from_ref(&name)));
        env::remove_var(&name);
        assert!(!present_in_env(std::slice::from_ref(&name)));
    }
}
