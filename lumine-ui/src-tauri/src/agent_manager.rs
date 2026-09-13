use std::env;
use std::io::{BufRead, BufReader, Read};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter};

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

#[derive(Default)]
struct Readiness {
    registered: bool,
    error: Option<String>,
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
    pub fn start(&mut self, app: &AppHandle) -> Result<AgentStatus, String> {
        self.sync_if_exited(app);

        if self.child.as_mut().is_some_and(|child| child.try_wait().ok().flatten().is_none()) {
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
        let child = self.child.as_mut().expect("agent child was just stored");
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

    pub fn wait_until_ready(&mut self, app: &AppHandle) -> Result<AgentStatus, String> {
        let deadline = Instant::now() + Duration::from_secs(20);
        let (readiness_lock, readiness_signal) = &*self.readiness;
        let mut readiness = readiness_lock.lock().map_err(|err| format!("Worker readiness lock poisoned: {err}"))?;
        while !readiness.registered && readiness.error.is_none() {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                self.status.state = AgentState::Error;
                self.status.connected = false;
                self.status.error = Some("Lumine worker did not register with LiveKit within 20 seconds.".to_string());
                return Err(self.status.error.clone().unwrap_or_else(|| "Lumine worker readiness timed out.".to_string()));
            }
            let result = readiness_signal.wait_timeout(readiness, remaining).map_err(|err| format!("Worker readiness wait failed: {err}"))?;
            readiness = result.0;
        }
        if let Some(error) = readiness.error.clone() {
            self.status.state = AgentState::Error;
            self.status.connected = false;
            self.status.error = Some(error.clone());
            return Err(error);
        }
        self.status.state = AgentState::Ready;
        self.status.connected = true;
        app.emit("agent_state_changed", &self.status).ok();
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

fn resolve_project_root() -> Result<PathBuf, String> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .canonicalize()
        .map_err(|err| format!("Could not resolve project root: {err}"))?;

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

fn resolve_python_bin() -> Result<String, String> {
    if let Ok(path) = env::var("LUMINE_PYTHON_BIN") {
        if !path.trim().is_empty() {
            return Ok(path);
        }
    }

    if let Ok(venv) = env::var("VIRTUAL_ENV") {
        let candidate = if cfg!(windows) {
            PathBuf::from(&venv).join("Scripts").join("python.exe")
        } else {
            PathBuf::from(&venv).join("bin").join("python")
        };

        if candidate.exists() {
            return Ok(candidate.to_string_lossy().to_string());
        }
    }

    let root = resolve_project_root()?;
    let bundled = if cfg!(windows) {
        root.join(".venv").join("Scripts").join("python.exe")
    } else {
        root.join(".venv").join("bin").join("python")
    };
    if bundled.exists() {
        return Ok(bundled.to_string_lossy().to_string());
    }

    for candidate in ["python", "python3"] {
        if Command::new(candidate)
            .arg("--version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok()
        {
            return Ok(candidate.to_string());
        }
    }

    Err("Could not resolve a Python interpreter. Set LUMINE_PYTHON_BIN or activate a venv before starting the agent.".to_string())
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
            println!("[agent:{stream}] {line}");
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
