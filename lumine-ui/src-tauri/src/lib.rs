mod agent_manager;

use std::env;
use std::process::{Command, Stdio};
use std::sync::Mutex;

use agent_manager::{AgentManager, AgentStatus};
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
fn dispatch_agent(room: String) -> Result<String, String> {
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
    let script = root.join("agent").join("livekit_dispatch.py");
    let output = Command::new(python)
        .arg(script)
        .arg(room)
        .arg("lumine")
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(Mutex::new(AgentManager::default()))
        .invoke_handler(tauri::generate_handler![start_agent, stop_agent, restart_agent, get_agent_status, get_agent_worker_status, wait_for_agent_worker, get_livekit_token, dispatch_agent, delete_livekit_room])
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
