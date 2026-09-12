use std::env;
use std::io::{BufRead, BufReader, Read};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter};

const RUNTIME_EVENT_PREFIX: &str = "LUMINE_EVENT ";

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum AgentState {
    Booting,
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
            app.emit("agent.error", &self.status)
                .ok();
            app.emit("agent.state_changed", &self.status)
                .ok();
            message
        })?;

        let pid = child.id();
        self.child = Some(child);
        let child = self.child.as_mut().expect("agent child was just stored");
        if let Some(stdout) = child.stdout.take() {
            spawn_output_reader(app.clone(), stdout, "stdout");
        }
        if let Some(stderr) = child.stderr.take() {
            spawn_output_reader(app.clone(), stderr, "stderr");
        }
        self.status = AgentStatus {
            state: AgentState::Booting,
            running: true,
            connected: false,
            pid: Some(pid),
            error: None,
        };

        app.emit("agent.started", &self.status)
            .map_err(|err| format!("Failed to emit agent.started: {err}"))?;
        app.emit("agent.state_changed", &self.status)
            .map_err(|err| format!("Failed to emit agent.state_changed: {err}"))?;

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

            app.emit("agent.state_changed", &self.status)
                .map_err(|err| format!("Failed to emit agent.state_changed: {err}"))?;

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

        app.emit("agent.stopped", &self.status)
            .map_err(|err| format!("Failed to emit agent.stopped: {err}"))?;
        app.emit("agent.state_changed", &self.status)
            .map_err(|err| format!("Failed to emit agent.state_changed: {err}"))?;

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

                let _ = app.emit("agent.error", &self.status);
                let _ = app.emit("agent.state_changed", &self.status);
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
                let _ = app.emit("agent.error", &self.status);
                let _ = app.emit("agent.state_changed", &self.status);
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

fn spawn_output_reader<R>(app: AppHandle, reader: R, stream: &'static str)
where
    R: Read + Send + 'static,
{
    std::thread::spawn(move || {
        for line in BufReader::new(reader).lines().map_while(Result::ok) {
            println!("[agent:{stream}] {line}");
            if let Some(payload) = line.strip_prefix(RUNTIME_EVENT_PREFIX) {
                if let Ok(event) = serde_json::from_str::<serde_json::Value>(payload) {
                    let _ = app.emit("agent.runtime", event);
                }
            }
        }
    });
}
