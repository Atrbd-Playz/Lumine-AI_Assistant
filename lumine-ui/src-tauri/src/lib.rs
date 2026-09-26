mod agent_manager;
// Hands the worker the keys held in the OS keyring, so a key stored in Settings
// actually reaches the runtime instead of only being reported as stored.
mod credential_injection;
mod credentials;
mod python_env;
mod settings_store;

use std::env;
use std::process::{Command, Stdio};
use std::sync::Mutex;

use agent_manager::{AgentManager, AgentStatus};
use credentials::CredentialStore;
use serde_json::Value;
use tauri::Manager;

#[tauri::command]
fn start_agent(app: tauri::AppHandle, manager: tauri::State<'_, Mutex<AgentManager>>) -> Result<AgentStatus, String> {
    let mut manager = manager.lock().map_err(|err| format!("Agent manager lock poisoned: {err}"))?;
    manager.start(&app)
}

#[tauri::command]
fn stop_agent(app: tauri::AppHandle, manager: tauri::State<'_, Mutex<AgentManager>>) -> Result<AgentStatus, String> {
    let mut manager = manager.lock().map_err(|err| format!("Agent manager lock poisoned: {err}"))?;
    manager.stop(&app)
}

#[tauri::command]
fn restart_agent(app: tauri::AppHandle, manager: tauri::State<'_, Mutex<AgentManager>>) -> Result<AgentStatus, String> {
    let mut manager = manager.lock().map_err(|err| format!("Agent manager lock poisoned: {err}"))?;
    manager.restart(&app)
}

#[tauri::command]
fn get_agent_status(app: tauri::AppHandle, manager: tauri::State<'_, Mutex<AgentManager>>) -> Result<AgentStatus, String> {
    let mut manager = manager.lock().map_err(|err| format!("Agent manager lock poisoned: {err}"))?;
    Ok(manager.get_status(&app))
}

#[tauri::command]
fn get_agent_worker_status(app: tauri::AppHandle, manager: tauri::State<'_, Mutex<AgentManager>>) -> Result<AgentStatus, String> {
    get_agent_status(app, manager)
}

#[tauri::command]
fn wait_for_agent_worker(app: tauri::AppHandle, manager: tauri::State<'_, Mutex<AgentManager>>) -> Result<AgentStatus, String> {
    let mut manager = manager.lock().map_err(|err| format!("Agent manager lock poisoned: {err}"))?;
    manager.wait_until_ready(&app)
}

#[tauri::command]
fn get_livekit_token(room: String, identity: String) -> Result<String, String> {
    if room.is_empty() || identity.is_empty() || room.len() > 128 || identity.len() > 128 {
        return Err("Invalid LiveKit session identity.".to_string());
    }
    if !room.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"-_".contains(&byte))
        || !identity.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"-_".contains(&byte))
    {
        return Err("Invalid LiveKit session identity.".to_string());
    }

    let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
    let python = env::var("LUMINE_PYTHON_BIN").ok().filter(|value| !value.trim().is_empty()).or_else(|| {
        env::var("VIRTUAL_ENV").ok().map(|venv| {
            if cfg!(windows) { format!("{venv}\\Scripts\\python.exe") } else { format!("{venv}/bin/python") }
        })
    }).unwrap_or_else(|| {
        let bundled = if cfg!(windows) { root.join(".venv").join("Scripts").join("python.exe") } else { root.join(".venv").join("bin").join("python") };
        if bundled.exists() { bundled.to_string_lossy().to_string() } else { "python".to_string() }
    });
    let script = root.join("agent").join("livekit_token.py");
    let output = Command::new(python)
        .arg(script)
        .arg(room)
        .arg(identity)
        .current_dir(&root)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|error| format!("Could not generate a LiveKit token: {error}"))?;

    if !output.status.success() {
        return Err("Could not generate a LiveKit token. Check the agent environment.".to_string());
    }

    String::from_utf8(output.stdout)
        .map(|token| token.trim().to_string())
        .map_err(|_| "LiveKit returned an invalid token.".to_string())
}

#[tauri::command]
fn dispatch_agent(room: String, metadata: Option<String>) -> Result<String, String> {
    if room.is_empty() || room.len() > 128 || !room.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"-_".contains(&byte)) {
        return Err("Invalid LiveKit room name.".to_string());
    }
    if let Some(value) = metadata.as_deref() {
        if value.is_empty() || value.len() > 4096 {
            return Err("Invalid agent session metadata.".to_string());
        }
        let parsed: serde_json::Value = serde_json::from_str(value)
            .map_err(|_| "Invalid agent session metadata.".to_string())?;
        let mode = parsed.get("interruption_mode").and_then(serde_json::Value::as_str);
        if !matches!(mode, Some("finish_response" | "barge_in")) {
            return Err("Invalid interruption mode.".to_string());
        }
    }

    let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
    let python = env::var("LUMINE_PYTHON_BIN").ok().filter(|value| !value.trim().is_empty()).or_else(|| {
        env::var("VIRTUAL_ENV").ok().map(|venv| {
            if cfg!(windows) { format!("{venv}\\Scripts\\python.exe") } else { format!("{venv}/bin/python") }
        })
    }).unwrap_or_else(|| {
        let bundled = if cfg!(windows) { root.join(".venv").join("Scripts").join("python.exe") } else { root.join(".venv").join("bin").join("python") };
        if bundled.exists() { bundled.to_string_lossy().to_string() } else { "python".to_string() }
    });
    let script = root.join("agent").join("livekit_dispatch.py");
    let mut command = Command::new(python);
    command
        .arg(script)
        .arg(room)
        .arg("lumine");
    if let Some(value) = metadata {
        command.arg(value);
    }
    let output = command
        .current_dir(&root)
        .output()
        .map_err(|error| format!("Could not dispatch Lumine: {error}"))?;

    if !output.status.success() {
        return Err("Could not dispatch Lumine to the session room. Check the agent environment.".to_string());
    }

    String::from_utf8(output.stdout)
        .map(|dispatch_id| dispatch_id.trim().to_string())
        .map_err(|_| "LiveKit returned an invalid dispatch id.".to_string())
}

#[tauri::command]
fn delete_livekit_room(room: String) -> Result<(), String> {
    if room.is_empty() || room.len() > 128 || !room.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"-_".contains(&byte)) {
        return Err("Invalid LiveKit room name.".to_string());
    }

    let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
    let python = env::var("LUMINE_PYTHON_BIN").ok().filter(|value| !value.trim().is_empty()).or_else(|| {
        env::var("VIRTUAL_ENV").ok().map(|venv| {
            if cfg!(windows) { format!("{venv}\\Scripts\\python.exe") } else { format!("{venv}/bin/python") }
        })
    }).unwrap_or_else(|| {
        let bundled = if cfg!(windows) { root.join(".venv").join("Scripts").join("python.exe") } else { root.join(".venv").join("bin").join("python") };
        if bundled.exists() { bundled.to_string_lossy().to_string() } else { "python".to_string() }
    });
    let script = root.join("agent").join("livekit_room.py");
    let output = Command::new(python)
        .arg(script)
        .arg(room)
        .current_dir(&root)
        .output()
        .map_err(|error| format!("Could not delete the LiveKit room: {error}"))?;

    if output.status.success() { Ok(()) } else { Err("Could not delete the LiveKit room.".to_string()) }
}

// ---------------------------------------------------------------------------
// AI Control Center (Phase 1)
//
// The command surface is deliberately write-only for credentials: there is no
// command that returns key material to the webview. Configuration and catalog
// reads are non-secret and may be returned freely.
//
// None of these commands touch the running worker yet. `agent/.env` remains
// authoritative until the worker-wiring step, so a user's voice session behaves
// exactly as it does today.
// ---------------------------------------------------------------------------

fn config_file_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    let dir = app
        .path()
        .app_local_data_dir()
        .map_err(|err| format!("Could not resolve the app data directory: {err}"))?;
    Ok(settings_store::config_path(&dir))
}

/// The provider catalog, straight from the worker side so there is exactly one
/// list of providers and models in the product.
#[tauri::command]
fn get_provider_catalog() -> Result<Value, String> {
    let stdout = python_env::run_agent_script("provider_catalog.py", &[])?;
    serde_json::from_str(&stdout).map_err(|err| format!("The provider catalog was not valid JSON: {err}"))
}

/// The effective configuration and where it came from.
///
/// `source` is `"ui"` when a saved document governs and `"env"` when the profile
/// is derived from `agent/.env` and read-only. The settings screen needs that
/// distinction to know whether it is editing a real configuration or looking at
/// a report of the current one.
#[tauri::command]
fn get_config() -> Result<Value, String> {
    let stdout = python_env::run_agent_script("validate_config.py", &["--describe"])?;
    serde_json::from_str(&stdout)
        .map_err(|err| format!("The configuration could not be read: {err}"))
}

/// Persist the configuration atomically, then read it back to prove it is usable.
///
/// The read-back is not ceremony: `save` can succeed and still leave something
/// `load` refuses (a truncated file, an unexpected encoding). Verifying here means
/// the app never reports a successful save it cannot read on the next launch.
#[tauri::command]
fn save_config(app: tauri::AppHandle, document: Value) -> Result<(), String> {
    let path = config_file_path(&app)?;
    settings_store::save(&path, &document).map_err(|err| err.to_string())?;
    settings_store::load(&path)
        .map_err(|err| format!("The configuration was written but could not be read back: {err}"))?;
    Ok(())
}

#[tauri::command]
fn set_credential(provider: String, secret: String) -> Result<credentials::CredentialStatus, String> {
    credentials::OsCredentialStore
        .set(&provider, &secret)
        .and_then(|()| credentials::OsCredentialStore.status(&provider))
        .map_err(|err| err.to_string())
}

#[tauri::command]
fn delete_credential(provider: String) -> Result<(), String> {
    credentials::OsCredentialStore.delete(&provider).map_err(|err| err.to_string())
}

/// Redacted status only. There is deliberately no command that returns the value.
#[tauri::command]
fn get_credential_status(provider: String) -> Result<credentials::CredentialStatus, String> {
    credentials::OsCredentialStore.status(&provider).map_err(|err| err.to_string())
}

/// Validate a configuration document without saving it.
///
/// The rules live in the worker so the settings screen and the runtime cannot
/// disagree. Takes the document as a JSON string because it arrives from the
/// webview as an untyped value and may legitimately be malformed.
#[tauri::command]
fn validate_config(document: String) -> Result<Value, String> {
    use std::io::Write;
    use std::process::{Command, Stdio};

    let mut child = Command::new(python_env::python_bin()?)
        .arg(python_env::agent_dir()?.join("validate_config.py"))
        .arg("-")
        .current_dir(python_env::project_root()?)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("Could not start validation: {err}"))?;

    child
        .stdin
        .as_mut()
        .ok_or("Validation stdin was unavailable.")?
        .write_all(document.as_bytes())
        .map_err(|err| format!("Could not send the configuration for validation: {err}"))?;

    let output = child
        .wait_with_output()
        .map_err(|err| format!("Validation did not finish: {err}"))?;
    if !output.status.success() {
        return Err("Validation failed to run.".to_string());
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|err| format!("Validation returned unreadable output: {err}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(Mutex::new(AgentManager::default()))
        .invoke_handler(tauri::generate_handler![
            start_agent,
            stop_agent,
            restart_agent,
            get_agent_status,
            get_agent_worker_status,
            wait_for_agent_worker,
            get_livekit_token,
            dispatch_agent,
            delete_livekit_room,
            get_provider_catalog,
            get_config,
            save_config,
            validate_config,
            set_credential,
            delete_credential,
            get_credential_status
        ])
        .setup(|_app| {
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running tauri application")
            .run(|app_handle, event| {
                if matches!(event, tauri::RunEvent::ExitRequested { .. }) {
                    if let Some(manager) = app_handle.try_state::<Mutex<AgentManager>>() {
                        if let Ok(mut manager) = manager.lock() {
                            let _ = manager.stop(app_handle);
                        }
                    }
                }
            });
}
