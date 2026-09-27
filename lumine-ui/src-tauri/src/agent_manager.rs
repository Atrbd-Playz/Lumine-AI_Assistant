use std::env;
use std::io::{BufRead, BufReader, Read};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{AppHandle, Emitter};

use crate::credential_injection;
use crate::credentials;
use crate::python_env;

const RUNTIME_EVENT_PREFIX: &str = "LUMINE_EVENT ";

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum AgentState {
    Booting,
    Ready,
    Sleeping,
    Listening,
    Thinking,
    Speaking,
    Error,
    ShuttingDown,
    Stopped,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AgentStatus {
    pub state: AgentState,
    pub running: bool,
    pub connected: bool,
    pub pid: Option<u32>,
    pub error: Option<String>,
}

pub struct AgentManager {
    child: Option<Child>,
    status: AgentStatus,
    readiness: Arc<(Mutex<Readiness>, Condvar)>,
}

/// The worker-registration state, shared with the stdout reader.
///
/// Cloned out of the manager before any waiting begins, so the readiness gate
/// never holds the manager's own lock. That lock is what `get_agent_status`,
/// `stop_agent` and `restart_agent` need, and a worker that takes twenty
/// seconds to register would otherwise make all four of them look hung.
#[derive(Default, Debug)]
pub struct Readiness {
    pub registered: bool,
    pub error: Option<String>,
}

/// How a readiness wait ended.
#[derive(Debug, PartialEq, Eq)]
pub enum ReadinessOutcome {
    Registered,
    /// The worker reported an error instead of registering.
    Failed(String),
    /// Nothing arrived in time. Not necessarily a failure: a worker started
    /// outside Tauri is invisible here and may still be serving the room.
    TimedOut,
}

impl ReadinessOutcome {
    pub fn message(&self) -> String {
        match self {
            ReadinessOutcome::Failed(message) => message.clone(),
            ReadinessOutcome::TimedOut => {
                "Lumine worker did not register with LiveKit within 20 seconds.".to_string()
            }
            ReadinessOutcome::Registered => "Worker is ready.".to_string(),
        }
    }
}

/// Block until the worker registers, reports an error, or `timeout` elapses.
///
/// Deliberately takes only the readiness lock. The caller applies the result to
/// the manager afterwards, so nothing that needs the manager can be blocked by a
/// slow worker boot.
pub fn await_readiness(
    readiness: &Arc<(Mutex<Readiness>, Condvar)>,
    timeout: Duration,
) -> ReadinessOutcome {
    let deadline = Instant::now() + timeout;
    let (lock, signal) = &**readiness;
    let Ok(mut state) = lock.lock() else {
        return ReadinessOutcome::Failed("Worker readiness lock poisoned.".to_string());
    };

    while !state.registered && state.error.is_none() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return ReadinessOutcome::TimedOut;
        }
        match signal.wait_timeout(state, remaining) {
            Ok(result) => state = result.0,
            Err(_) => return ReadinessOutcome::Failed("Worker readiness wait failed.".to_string()),
        }
    }

    match state.error.clone() {
        Some(error) => ReadinessOutcome::Failed(error),
        None => ReadinessOutcome::Registered,
    }
}

impl Default for AgentManager {
    fn default() -> Self {
        Self {
            child: None,
            status: AgentStatus {
                state: AgentState::Sleeping,
                running: false,
                connected: false,
                pid: None,
                error: None,
            },
            readiness: Arc::new((Mutex::new(Readiness::default()), Condvar::new())),
        }
    }
}

impl AgentManager {
    /// A handle to the readiness gate, cloned so the caller can wait without
    /// holding the manager's lock.
    pub fn readiness_handle(&self) -> Arc<(Mutex<Readiness>, Condvar)> {
        Arc::clone(&self.readiness)
    }

    /// Record the result of a readiness wait on the manager's status.
    pub fn apply_readiness(
        &mut self,
        app: &AppHandle,
        outcome: ReadinessOutcome,
    ) -> Result<AgentStatus, String> {
        match outcome {
            ReadinessOutcome::Registered => {
                self.status.state = AgentState::Ready;
                self.status.connected = true;
                self.status.error = None;
                app.emit("agent_state_changed", &self.status).ok();
                Ok(self.status.clone())
            }
            ReadinessOutcome::Failed(_) | ReadinessOutcome::TimedOut => {
                self.status.state = AgentState::Error;
                self.status.connected = false;
                self.status.error = Some(outcome.message());
                app.emit("agent_state_changed", &self.status).ok();
                Err(outcome.message())
            }
        }
    }

    /// Whether a child is still alive.
    ///
    /// A `try_wait` error counts as *not* alive. Folding the error into "still
    /// running" would make `start` believe it found a healthy worker and return
    /// without doing anything, while in fact the handle is unusable and nothing
    /// is coming back.
    fn is_alive(&mut self) -> bool {
        self.child
            .as_mut()
            .is_some_and(|child| matches!(child.try_wait(), Ok(None)))
    }

    pub fn start(&mut self, app: &AppHandle) -> Result<AgentStatus, String> {
        self.sync_if_exited(app);

        if self.is_alive() {
            return Ok(self.status.clone());
        }

        let python_bin = resolve_python_bin()?;
        let agent_script = resolve_agent_script()?;
        let project_root = resolve_project_root()?;
        let agent_args = resolve_agent_args();
        {
            let (state, _) = &*self.readiness;
            let mut state = state.lock().map_err(|err| format!("Worker readiness lock poisoned: {err}"))?;
            state.registered = false;
            state.error = None;
        }

        let mut command = Command::new(&python_bin);
        command
            .arg(&agent_script)
            .args(agent_args)
            .current_dir(&project_root)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        // Point the worker at the same configuration file this app writes.
        // Without this the two runtimes would silently read different files: Rust
        // defaults to the app data directory, the worker to the agent directory.
        if let Some(config_path) = resolve_config_path(app) {
            command.env("LUMINE_CONFIG_PATH", &config_path);
        }

        // `agent.py dev` is a development command, and LiveKit's dev mode turns the
        // root logger up to DEBUG. That is a lot of records for a desktop app to
        // consume over a pipe: the child's stdout is a fixed-size OS buffer, and a
        // producer that outruns the reader either blocks or -- once the read end is
        // gone -- fails every write with `OSError: [Errno 22] Invalid argument`.
        //
        // Set through the environment rather than the argument list, so it composes
        // with a caller who has set `LUMINE_AGENT_ARGS`. An explicit setting always
        // wins, because someone who asked for DEBUG wants DEBUG.
        if std::env::var_os("LIVEKIT_LOG_LEVEL").is_none() {
            command.env("LIVEKIT_LOG_LEVEL", "info");
        }

        // Hand over the keys held in the OS keyring. A provider with no stored
        // key is left to `agent/.env`, which remains a supported way to
        // configure the worker.
        if let Some(catalog) = read_provider_catalog() {
            let store = credentials::OsCredentialStore;
            let injected = credential_injection::inject_credentials(&mut command, &catalog, &store);
            if !injected.is_empty() {
                // Provider ids only. A key value must never reach a log.
                println!("[LUMINE][WORKER] injected stored credentials for: {}", injected.join(", "));
            }
        }

        let child = command.spawn().map_err(|err| {
            let message = format!("Failed to launch the Lumine Python agent: {err}");
            self.status = AgentStatus {
                state: AgentState::Error,
                running: false,
                connected: false,
                pid: None,
                error: Some(message.clone()),
            };
            app.emit("agent_error", &self.status)
                .ok();
            app.emit("agent_state_changed", &self.status)
                .ok();
            message
        })?;

        let pid = child.id();
        println!("[LUMINE][WORKER] spawn pid={pid}");
        self.child = Some(child);
        // Not `expect`. The value was just stored, so this branch is unreachable
        // in practice -- but a panic in a Tauri command aborts the process, and a
        // bookkeeping slip is not a reason a desktop app can disappear. Report it
        // the way every other failure here is reported.
        let Some(child) = self.child.as_mut() else {
            let message = "The agent process was spawned but could not be tracked.".to_string();
            self.status = AgentStatus {
                state: AgentState::Error,
                running: false,
                connected: false,
                pid: None,
                error: Some(message.clone()),
            };
            app.emit("agent_error", &self.status).ok();
            app.emit("agent_state_changed", &self.status).ok();
            return Err(message);
        };
        let readiness = Arc::clone(&self.readiness);
        if let Some(stdout) = child.stdout.take() {
            spawn_output_reader(app.clone(), stdout, "stdout", Some(readiness));
        }
        if let Some(stderr) = child.stderr.take() {
            spawn_output_reader(app.clone(), stderr, "stderr", None);
        }
        self.status = AgentStatus {
            state: AgentState::Booting,
            running: true,
            connected: false,
            pid: Some(pid),
            error: None,
        };

        app.emit("agent_started", &self.status)
            .map_err(|err| format!("Failed to emit agent_started: {err}"))?;
        app.emit("agent_state_changed", &self.status)
            .map_err(|err| format!("Failed to emit agent_state_changed: {err}"))?;

        Ok(self.status.clone())
    }

    pub fn stop(&mut self, app: &AppHandle) -> Result<AgentStatus, String> {
        if let Some(mut child) = self.child.take() {
            self.status = AgentStatus {
                state: AgentState::ShuttingDown,
                running: false,
                connected: false,
                pid: Some(child.id()),
                error: None,
            };

            app.emit("agent_state_changed", &self.status)
                .map_err(|err| format!("Failed to emit agent_state_changed: {err}"))?;

            let _ = child.kill();
            let _ = child.wait();
        }

        self.child = None;
        self.status = AgentStatus {
            state: AgentState::Sleeping,
            running: false,
            connected: false,
            pid: None,
            error: None,
        };

        app.emit("agent_stopped", &self.status)
            .map_err(|err| format!("Failed to emit agent_stopped: {err}"))?;
        app.emit("agent_state_changed", &self.status)
            .map_err(|err| format!("Failed to emit agent_state_changed: {err}"))?;

        Ok(self.status.clone())
    }

    pub fn restart(&mut self, app: &AppHandle) -> Result<AgentStatus, String> {
        let _ = self.stop(app);
        self.start(app)
    }

    pub fn get_status(&mut self, app: &AppHandle) -> AgentStatus {
        self.sync_if_exited(app);
        self.status.clone()
    }

    fn sync_if_exited(&mut self, app: &AppHandle) {
        let Some(child) = self.child.as_mut() else {
            if self.status.running {
                self.status = AgentStatus {
                    state: AgentState::Sleeping,
                    running: false,
                    connected: false,
                    pid: None,
                    error: Some("Agent process is not running.".to_string()),
                };
            }
            return;
        };

        match child.try_wait() {
            Ok(Some(_)) => {
                self.child = None;
                self.status = AgentStatus {
                    state: AgentState::Error,
                    running: false,
                    connected: false,
                    pid: None,
                    error: Some("Agent process exited unexpectedly.".to_string()),
                };

                let _ = app.emit("agent_error", &self.status);
                let _ = app.emit("agent_state_changed", &self.status);
            }
            Ok(None) => {}
            Err(err) => {
                self.child = None;
                self.status = AgentStatus {
                    state: AgentState::Error,
                    running: false,
                    connected: false,
                    pid: None,
                    error: Some(format!("Failed to inspect the agent process: {err}")),
                };
                let _ = app.emit("agent_error", &self.status);
                let _ = app.emit("agent_state_changed", &self.status);
            }
        }
    }
}

/// Where the app's configuration file lives, if it can be resolved.
///
/// Best-effort: a failure here is not worth refusing to start the worker, because
/// the worker then falls back to `agent/.env`, which is the pre-existing path.
fn resolve_config_path(app: &AppHandle) -> Option<PathBuf> {
    use tauri::Manager;
    let dir = app.path().app_local_data_dir().ok()?;
    Some(crate::settings_store::config_path(&dir))
}

/// The provider catalog, or `None` if it cannot be read.
///
/// The catalog is the only description of which environment variable belongs to
/// which provider. Failing to read it is not worth refusing to start the worker
/// over: the credentials then stay in `agent/.env`, which is how this worked
/// before, and a later `start` can try again.
fn read_provider_catalog() -> Option<Value> {
    let stdout = python_env::run_agent_script("provider_catalog.py", &[]).ok()?;
    serde_json::from_str(&stdout).ok()
}

fn resolve_project_root() -> Result<PathBuf, String> {
    let root = python_env::project_root()?;

    if !root.join("agent").join("agent.py").exists() {
        return Err("Could not find the Python agent script in the project root.".to_string());
    }

    Ok(root)
}

fn resolve_agent_script() -> Result<PathBuf, String> {
    if let Ok(path) = env::var("LUMINE_AGENT_PATH") {
        let resolved = PathBuf::from(path);
        if resolved.exists() {
            return Ok(resolved);
        }
    }

    let root = resolve_project_root()?;
    let candidate = root.join("agent").join("agent.py");

    if candidate.exists() {
        Ok(candidate)
    } else {
        Err("Lumine agent script not found. Set LUMINE_AGENT_PATH to the agent.py file.".to_string())
    }
}

/// The interpreter to run the worker with.
///
/// Delegates to `python_env` rather than repeating the resolution. This file
/// used to carry its own copy of the `LUMINE_PYTHON_BIN` -> `VIRTUAL_ENV` ->
/// repository `.venv` -> `python` ladder, and the two copies had already drifted
/// on what counted as a usable interpreter. One ladder, one place to change it.
fn resolve_python_bin() -> Result<String, String> {
    python_env::python_bin()
}

fn resolve_agent_args() -> Vec<String> {
    env::var("LUMINE_AGENT_ARGS")
        .ok()
        .filter(|args| !args.trim().is_empty())
        .map(|args| args.split_whitespace().map(String::from).collect())
        .unwrap_or_else(|| vec!["dev".to_string()])
}

fn spawn_output_reader<R>(app: AppHandle, reader: R, stream: &'static str, readiness: Option<Arc<(Mutex<Readiness>, Condvar)>>)
where
    R: Read + Send + 'static,
{
    std::thread::spawn(move || {
        for line in BufReader::new(reader).lines().map_while(Result::ok) {
            // Only the event stream is echoed. A worker is noisy, and a `println!`
            // per line means a lock held per line on the one thread responsible for
            // keeping the child's pipe drained. Echoing to the console is also
            // redundant: Tauri's own terminal already shows the child's output in
            // development, and this reader is the second consumer, not the first.
            //
            // `LUMINE_EVENT` records are still parsed in full below -- they are
            // structured, low-volume, and the readiness gate depends on them.
            if line.starts_with(RUNTIME_EVENT_PREFIX) {
                println!("[agent:{stream}] {line}");
            }
            if let Some(payload) = line.strip_prefix(RUNTIME_EVENT_PREFIX) {
                if let Ok(event) = serde_json::from_str::<serde_json::Value>(payload) {
                    if let Some(readiness) = &readiness {
                        let event_type = event.get("type").and_then(serde_json::Value::as_str);
                        if event_type == Some("worker_registered") || event_type == Some("error") {
                            let (lock, signal) = &**readiness;
                            if let Ok(mut state) = lock.lock() {
                                if event_type == Some("worker_registered") {
                                    state.registered = true;
                                    println!("[LUMINE][WORKER] registered");
                                } else {
                                    state.error = event.get("message").and_then(serde_json::Value::as_str).map(String::from);
                                }
                                signal.notify_all();
                            }
                        }
                    }
                    let _ = app.emit("agent_runtime", event);
                }
            }
        }
    });
}
