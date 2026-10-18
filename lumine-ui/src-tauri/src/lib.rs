mod agent_manager;
// Proves a stored provider key works, without the key entering the webview.
mod credential_injection;
mod credential_probe;
mod credentials;
// The native folder picker for the music library, and what is inside it.
mod media;
mod python_env;
mod settings_store;
mod setup_status;
// Renders a candidate voice so the settings screen can play it back.
mod voice_preview;

use std::collections::HashMap;
use std::env;
use std::process::{Command, Stdio};
use std::sync::{Mutex, OnceLock};

use agent_manager::{AgentManager, AgentStatus};
use credentials::CredentialStore;
use serde_json::Value;
use tauri::Manager;

/// Run a blocking task on the async pool and return what it produced.
///
/// ## Why every helper-reading command goes through here
///
/// A Tauri command declared without `async` **runs on the main thread**. That is
/// documented, and it is invisible until something slow runs inside one: a cold
/// `python agent/tool_catalog.py` spends about six seconds importing
/// `livekit-agents` before it prints a byte, so the Tools page froze the whole
/// window for six seconds, every repaint stopped, and navigating away during it
/// produced a Windows "not responding" dialog. A spinner cannot fix that, because
/// the thread that would draw the spinner is the thread that is blocked.
///
/// So the blocking half goes to `spawn_blocking` and the command stays `async`.
/// `wait_for_agent_worker` and `voice_preview` were already written this way; the
/// catalog and configuration reads were simply missed, and they are the ones on
/// the launch path.
///
/// A task that panicked arrives as `Err` here rather than as a hang, which is
/// also what keeps one failing helper from taking the app with it.
pub(crate) async fn run_off_thread<T, F>(action: &str, task: F) -> Result<T, String>
where
    T: Send + 'static,
    F: FnOnce() -> Result<T, String> + Send + 'static,
{
    match tauri::async_runtime::spawn_blocking(task).await {
        Ok(result) => result,
        Err(err) => Err(format!("{action} did not finish: {err}")),
    }
}

/// Run a one-shot helper from `agent/` and return its stdout.
///
/// Every command that shells out to a Python helper goes through here, so there
/// is one place that knows the interpreter, the working directory and the error
/// shape. It also fixes the three commands that used to discard the child's
/// stderr: a script that fails because of a missing credential printed the
/// reason to a pipe nobody read, and the UI showed "check the agent
/// environment" for what was actually a missing `LIVEKIT_API_SECRET`.
pub(crate) fn run_helper(script: &str, args: &[&str], action: &str) -> Result<Vec<u8>, String> {
    let python = python_env::python_bin()?;
    let path = python_env::agent_dir()?.join(script);

    let output = Command::new(python)
        .arg(&path)
        .args(args)
        .current_dir(python_env::project_root()?)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|err| format!("Could not {action}: {err}"))?;

    if !output.status.success() {
        // The last line of stderr is the traceback's conclusion. Trimmed to
        // something a toast can hold, because this string goes to the UI.
        let stderr = String::from_utf8_lossy(&output.stderr);
        let detail = stderr
            .lines()
            .rev()
            .map(str::trim)
            .find(|line| !line.is_empty())
            .unwrap_or("no error output");
        return Err(format!("Could not {action}: {detail}"));
    }

    Ok(output.stdout)
}

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

/// Wait for the worker to register with LiveKit.
///
/// The wait itself happens *without* the manager's lock. A worker can take
/// twenty seconds to come up, and holding the lock across that made
/// `get_agent_status`, `stop_agent` and `restart_agent` block for the same
/// twenty seconds -- so a slow worker boot looked like a frozen app, and the
/// only way to recover was to kill the process.
#[tauri::command]
async fn wait_for_agent_worker(
    app: tauri::AppHandle,
    manager: tauri::State<'_, Mutex<AgentManager>>,
) -> Result<AgentStatus, String> {
    let readiness = {
        let guard = manager
            .lock()
            .map_err(|err| format!("Agent manager lock poisoned: {err}"))?;
        guard.readiness_handle()
    };

    // Off the async runtime's worker thread: this blocks a thread for up to
    // twenty seconds, and `spawn_blocking` exists for exactly that.
    let outcome = tauri::async_runtime::spawn_blocking(move || {
        agent_manager::await_readiness(&readiness, std::time::Duration::from_secs(20))
    })
    .await
    .map_err(|err| format!("Worker readiness wait failed: {err}"))?;

    let mut manager = manager
        .lock()
        .map_err(|err| format!("Agent manager lock poisoned: {err}"))?;
    manager.apply_readiness(&app, outcome)
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

    let output = run_helper("livekit_token.py", &[&room, &identity], "generate a LiveKit token")?;

    String::from_utf8(output)
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

    let mut args: Vec<&str> = vec![&room, "lumine"];
    if let Some(value) = metadata.as_deref() {
        args.push(value);
    }
    let output = run_helper("livekit_dispatch.py", &args, "dispatch Lumine to the session room")?;

    String::from_utf8(output)
        .map(|dispatch_id| dispatch_id.trim().to_string())
        .map_err(|_| "LiveKit returned an invalid dispatch id.".to_string())
}

#[tauri::command]
fn delete_livekit_room(room: String) -> Result<(), String> {
    if room.is_empty() || room.len() > 128 || !room.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"-_".contains(&byte)) {
        return Err("Invalid LiveKit room name.".to_string());
    }

    run_helper("livekit_room.py", &[&room], "delete the LiveKit room")?;
    Ok(())
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
///
/// ## Why this is `async` when it used not to be
///
/// A Tauri command without the `async` keyword runs **on the main thread**, and
/// this one spawns a Python interpreter. Every read of the catalog therefore
/// froze the entire window for as long as the interpreter took, and any attempt
/// to navigate during that window produced a Windows "not responding" state. The
/// same was true of the tool catalog and the effective configuration, which is
/// why all three go through [`run_off_thread`] now.
#[tauri::command]
async fn get_provider_catalog() -> Result<Value, String> {
    if let Some(hit) = cached(CATALOG_PROVIDER) {
        return Ok(hit);
    }
    let resolved = run_off_thread("Reading the provider catalog", read_provider_catalog).await?;
    write_catalog_cache(CATALOG_PROVIDER, resolved.clone())?;
    Ok(resolved)
}

pub(crate) fn read_provider_catalog() -> Result<Value, String> {
    let stdout = python_env::run_agent_script("provider_catalog.py", &[])?;
    serde_json::from_str(&stdout).map_err(|err| format!("The provider catalog was not valid JSON: {err}"))
}

/// The tool registry, for the same reason: one list of what Lumine can do.
///
/// Generated from `agent/tools/tools_registry.py` rather than written in the
/// frontend, because a hardcoded list here would drift the first time a tool was
/// added and the Tools page would then advertise something the worker cannot
/// call. Each tool carries whether it is enabled under the current environment
/// toggles, so a card cannot show a green tick for something `LUMINE_DISABLED_TOOLS`
/// has switched off.
/// The tool catalog, resolved once per app run.
///
/// ## Why this is cached, and why that is not a staleness bug
///
/// The first version ran the helper on every call, and the Tools page read it on
/// every mount. A cold `python agent/tool_catalog.py` takes about six seconds on
/// this machine — most of it the interpreter, not the script — so the page showed
/// five grey placeholder cards for six seconds and people read that as "there are
/// no tools".
///
/// The answer is genuinely static for the lifetime of the desktop process. What
/// it depends on is the `TOOL_CARDS` table in `agent/tools/tool_registry.py` and
/// the `LUMINE_DISABLED_TOOLS` / `LUMINE_ENABLE_APP_LAUNCH` toggles. The first is
/// compiled into the file on disk, and the second two are read from the
/// environment when the tool module loads — and this process's environment does not
/// change under it, because Tauri is the only thing that sets it, at spawn, and
/// nothing rewrites it afterwards.
///
/// A failure is cached too, and that is deliberate for the same reason: if the
/// interpreter is missing, every retry spends the same several seconds proving it
/// again, on a page somebody is trying to read. `clear_tool_catalog` is the
/// deliberate way to force a re-read, and it exists for the case where somebody
/// adds a tool while the app is open and wants to see it without a restart.

/// The cache slot for the provider catalog.
pub(crate) const CATALOG_PROVIDER: &str = "provider";
/// The cache slot for the tool catalog.
const CATALOG_TOOL: &str = "tool";
/// The cache slot for the effective configuration.
///
/// A third slot rather than a special case, because the reasoning is the same —
/// except for *what invalidates it*, and that is the whole difference. A catalog
/// depends on files this process never writes; the effective configuration
/// depends on the saved document, which `save_config` does write, so it is
/// dropped there and nowhere else.
pub(crate) const CATALOG_CONFIG: &str = "config";

static CATALOG_CACHE: OnceLock<Mutex<HashMap<&'static str, Value>>> = OnceLock::new();

fn catalog_cache() -> &'static Mutex<HashMap<&'static str, Value>> {
    CATALOG_CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

/// The cached value for a slot, or `None` if this slot was never filled.
///
/// Only successes are held. The tool catalog used to cache its failures as well,
/// on the argument that a missing interpreter costs the same several seconds every
/// time; that is true and it is the wrong trade now that the read no longer
/// blocks the window — a cached failure is a page that never recovers on its own,
/// and the honest version of one is a message the reader can retry from.
pub(crate) fn cached(slot: &'static str) -> Option<Value> {
    catalog_cache().lock().ok()?.get(slot).cloned()
}

/// Store a successful read. A poisoned lock means some other command panicked
/// mid-write, which is worth reporting rather than silently ignoring.
pub(crate) fn write_catalog_cache(slot: &'static str, value: Value) -> Result<(), String> {
    let mut guard = catalog_cache()
        .lock()
        .map_err(|_| "The catalog cache was poisoned by a previous panic.".to_string())?;
    guard.insert(slot, value);
    Ok(())
}

/// Drop a cached read so the next call re-runs the helper.
///
/// Not reachable from the UI today. It exists because "restart the app" is the
/// only other way to see a tool added in this session, and a one-line command is
/// cheaper than a bug report about a missing tool.
#[tauri::command]
fn clear_tool_catalog() {
    if let Ok(mut slots) = catalog_cache().lock() {
        slots.remove(CATALOG_TOOL);
        slots.remove(CATALOG_PROVIDER);
    }
}

/// What Lumine can do, as data.
///
/// Generated from `agent/tools/tools_registry.py` rather than written in the
/// frontend, because a hardcoded list here would drift the first time a tool was
/// added and the Tools page would then advertise something the worker cannot
/// call. Each tool carries whether it is enabled under the current environment
/// toggles, so a card cannot show a green tick for something `LUMINE_DISABLED_TOOLS`
/// has switched off.
#[tauri::command]
async fn get_tool_catalog() -> Result<Value, String> {
    if let Some(cached) = cached(CATALOG_TOOL) {
        return Ok(cached);
    }
    // The slot is checked and filled separately rather than held across the read.
    // Two concurrent Tools pages would otherwise serialise on the mutex for the
    // full six seconds, and the second one would still pay it — which is the
    // whole bug this cache was introduced to remove, one level down.
    let resolved = run_off_thread("Reading the tool catalog", read_tool_catalog).await?;
    write_catalog_cache(CATALOG_TOOL, resolved.clone())?;
    Ok(resolved)
}

fn read_tool_catalog() -> Result<Value, String> {
    let stdout = python_env::run_agent_script("tool_catalog.py", &[])?;
    serde_json::from_str(&stdout).map_err(|err| format!("The tool catalog was not valid JSON: {err}"))
}

/// Which models a local model server is serving.
///
/// The catalog is a list Lumine knows about; a server is a list that server knows
/// about, and they are not the same list. Someone with `qwen3:8b` pulled has never
/// heard of it, so a chooser that only offers the catalog is answering "no" to a
/// model that is sitting right there.
///
/// The HTTP call is Python's for the same reason the credential probe is: the
/// desktop layer has no HTTP client of its own, and adding one for a single `GET`
/// on localhost would be a dependency to keep current in exchange for six lines of
/// `httpx`. The child always exits zero and reports the verdict in `ok`, so a
/// failed lookup arrives as a sentence rather than as a discarded stdout buffer.
///
/// `base_url` is a *name*, not a credential: it is not secret, it does not belong
/// in the keyring, and it is passed as an argument because it is not.
#[tauri::command]
async fn discover_local_models(base_url: Option<String>) -> Result<Value, String> {
    // Owned up front rather than borrowed out of the argument: the closure has to
    // be `'static` to go on the blocking pool, so it cannot hold a slice of a
    // parameter that is dropped when the command returns.
    let args: Vec<String> = base_url
        .as_deref()
        .map(str::trim)
        .filter(|url| !url.is_empty())
        .map(str::to_string)
        .into_iter()
        .collect();
    // Not cached: the whole point is to ask a server that is running *now*, and
    // a cached answer is a stale list the user is about to pick a model from.
    run_off_thread("Asking the local model server", move || {
        let borrowed: Vec<&str> = args.iter().map(String::as_str).collect();
        let stdout = python_env::run_agent_script("local_models.py", &borrowed)?;
        serde_json::from_str(&stdout)
            .map_err(|err| format!("The discovery result was not valid JSON: {err}"))
    })
    .await
}

/// The effective configuration and where it came from.
///
/// `source` is `"ui"` when a saved document governs and `"env"` when the profile
/// is derived from `agent/.env` and read-only. The settings screen needs that
/// distinction to know whether it is editing a real configuration or looking at
/// a report of the current one.
///
/// Cached, and invalidated by `save_config` — the only thing in this process that
/// writes the file this reads.
#[tauri::command]
async fn get_config() -> Result<Value, String> {
    if let Some(cached) = cached(CATALOG_CONFIG) {
        return Ok(cached);
    }
    let resolved = run_off_thread("Reading the configuration", read_config).await?;
    write_catalog_cache(CATALOG_CONFIG, resolved.clone())?;
    Ok(resolved)
}

pub(crate) fn read_config() -> Result<Value, String> {
    let stdout = python_env::run_agent_script("validate_config.py", &["--describe"])?;
    serde_json::from_str(&stdout)
        .map_err(|err| format!("The configuration could not be read: {err}"))
}

/// Persist the configuration atomically, then read it back to prove it is usable.
///
/// The read-back is not ceremony: `save` can succeed and still leave something
/// `load` refuses (a truncated file, an unexpected encoding). Verifying here means
/// the app never reports a successful save it cannot read on the next launch.
///
/// The cached effective configuration is dropped on the way out, including on
/// failure. A save that was refused must not leave the app reporting the document
/// it tried to write — the next reader would then get the old answer from the
/// cache and the settings screen would appear to have reverted silently.
#[tauri::command]
fn save_config(app: tauri::AppHandle, document: Value) -> Result<(), String> {
    let path = config_file_path(&app)?;
    let outcome = settings_store::save(&path, &document)
        .map_err(|err| err.to_string())
        .and_then(|()| {
            settings_store::load(&path)
                .map_err(|err| format!("The configuration was written but could not be read back: {err}"))
        });
    if let Ok(mut slots) = catalog_cache().lock() {
        slots.remove(CATALOG_CONFIG);
        // **The tool catalog too**, and this was a real bug: every card's
        // `enabled` / `disabledReason` is computed by the worker from this same
        // document, so a tool switch written by the Tools page invalidated the
        // config cache and left the catalog alone. The write succeeded, and the
        // next read handed back the catalog from *before* the write -- so
        // flipping a switch appeared to revert on its own, and a page reload
        // brought the stale answer back. A user toggling a capability and
        // watching it switch itself off has no way to tell that from the
        // feature being broken.
        slots.remove(CATALOG_TOOL);
    }
    outcome.map(|_| ())
}

/// Store a credential, or one slot of a multi-value provider's credential.
///
/// `slot` is the environment variable the value belongs in -- `LIVEKIT_URL`,
/// `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` for LiveKit. It is `None` for a
/// provider with a single key, which is what keeps every credential stored
/// before slots existed readable under the name it already has.
///
/// The slot is a variable *name*, never a value, so nothing secret crosses the
/// IPC boundary in the other direction: this command takes one and returns only
/// a redacted status.
#[tauri::command]
fn set_credential(
    provider: String,
    secret: String,
    slot: Option<String>,
) -> Result<credentials::CredentialStatus, String> {
    let slot = slot.as_deref();
    credentials::OsCredentialStore
        .set(&provider, slot, &secret)
        .and_then(|()| credentials::OsCredentialStore.status(&provider, slot))
        .map_err(|err| err.to_string())
}

/// Remove one credential slot.
///
/// A provider with several slots is removed slot by slot, so clearing the API
/// key does not silently discard a URL the user just spent time entering. The
/// caller passes the same `slot` it passed to `set_credential`.
#[tauri::command]
fn delete_credential(provider: String, slot: Option<String>) -> Result<(), String> {
    credentials::OsCredentialStore
        .delete(&provider, slot.as_deref())
        .map_err(|err| err.to_string())
}

/// Redacted status only. There is deliberately no command that returns the value.
#[tauri::command]
fn get_credential_status(
    provider: String,
    slot: Option<String>,
) -> Result<credentials::CredentialStatus, String> {
    credentials::OsCredentialStore
        .status(&provider, slot.as_deref())
        .map_err(|err| err.to_string())
}

/// Validate a configuration document without saving it.
///
/// The rules live in the worker so the settings screen and the runtime cannot
/// disagree. Takes the document as a JSON string because it arrives from the
/// webview as an untyped value and may legitimately be malformed.
///
/// `async` for the same reason the catalog reads are: this runs on every edit
/// keystroke in the settings screens, and as a synchronous command it blocked
/// the main thread on an interpreter start *per keystroke*.
#[tauri::command]
async fn validate_config(document: String) -> Result<Value, String> {
    run_off_thread("Validation", move || validate_config_blocking(document)).await
}

fn validate_config_blocking(document: String) -> Result<Value, String> {
    use std::io::Write;

    let mut child = Command::new(python_env::python_bin()?)
        .arg(python_env::agent_dir()?.join("validate_config.py"))
        .arg("-")
        .current_dir(python_env::project_root()?)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("Could not start validation: {err}"))?;

    // `take` and then drop, rather than `as_mut`. The script reads stdin to EOF
    // before it answers, so the pipe has to be closed for the read to finish.
    // Holding it open meant a configuration larger than the OS pipe buffer
    // blocked this write while the child blocked waiting for more input: a
    // deadlock that only appeared for big documents, and only on the settings
    // screen.
    {
        let mut stdin = child
            .stdin
            .take()
            .ok_or("Validation stdin was unavailable.")?;
        stdin
            .write_all(document.as_bytes())
            .map_err(|err| format!("Could not send the configuration for validation: {err}"))?;
    }

    let output = child
        .wait_with_output()
        .map_err(|err| format!("Validation did not finish: {err}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let detail = stderr
            .lines()
            .rev()
            .map(str::trim)
            .find(|line| !line.is_empty())
            .unwrap_or("no error output");
        return Err(format!("Validation failed to run: {detail}"));
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|err| format!("Validation returned unreadable output: {err}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        // The native folder picker. Registered from Rust rather than reached from
        // the webview, so the Music screen asks for a folder through an ordinary
        // command and no capability has to be granted to the frontend.
        .plugin(tauri_plugin_dialog::init())
        // Approval questions as OS toasts. Registered from the webview side only
        // for sending and listening; there is no Rust command here, because the
        // request id already travels in the payload and the answer goes back the
        // way every other permission answer does.
        .plugin(tauri_plugin_notification::init())
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
            get_tool_catalog,
            clear_tool_catalog,
            discover_local_models,
            get_config,
            save_config,
            validate_config,
            set_credential,
            delete_credential,
            get_credential_status,
            setup_status::get_setup_status,
            credential_probe::test_provider_credential,
            voice_preview::preview_voice,
            media::pick_music_folder,
            media::inspect_music_folder,
            media::queue_music
        ])
        .setup(|app| {
            // The identifier moved from `com.art.lumine-ui` to `com.art.lumine`,
            // which moved `app_local_data_dir()` and with it the saved settings.
            // Converge on the new location once, so the user's profiles do not
            // read as a fresh install. Reads already fall back to the old path,
            // so a failure here is logged rather than fatal.
            use tauri::Manager;
            if let Ok(dir) = app.path().app_local_data_dir() {
                match settings_store::migrate_legacy_config(&dir) {
                    Ok(true) => eprintln!(
                        "[LUMINE][CONFIG] moved saved settings into {}",
                        dir.display()
                    ),
                    Ok(false) => {}
                    Err(err) => eprintln!("[LUMINE][CONFIG] settings migration skipped: {err}"),
                }
            }
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
