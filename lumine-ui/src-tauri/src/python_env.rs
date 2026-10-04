//! Shared resolution of the Python interpreter and the `agent/` directory.
//!
//! `lib.rs` already resolves Python inline in three places and `agent_manager.rs`
//! in a fourth. Rather than add a fifth copy for the new commands, this module is
//! the single place new code should call.
//!
//! Consolidating the existing three call sites onto this helper is deliberately
//! left as a follow-up: those commands are on the working voice path, and
//! Phase 1 must not put them at risk.

use std::io::Read;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// Repository root, derived from this crate's location.
///
/// This is a development layout. A packaged build ships the Python worker as a
/// sidecar and must resolve it from the bundle instead; that is the packaging
/// work item, not this one.
pub fn project_root() -> Result<PathBuf, String> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .canonicalize()
        .map_err(|err| format!("Could not resolve the project root: {err}"))?;
    Ok(root)
}

/// The `agent/` directory, where the Python helpers and the worker live.
pub fn agent_dir() -> Result<PathBuf, String> {
    Ok(project_root()?.join("agent"))
}

/// The environment values interpreter resolution consults.
///
/// Grouped into a struct so the resolution order can be tested without mutating
/// process-global environment variables, which race under parallel `cargo test`.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct PythonEnv {
    pub explicit: Option<String>,
    pub venv: Option<String>,
}

impl PythonEnv {
    fn from_process() -> Self {
        let read = |name: &str| std::env::var(name).ok().filter(|value| !value.trim().is_empty());
        Self {
            explicit: read("LUMINE_PYTHON_BIN"),
            venv: read("VIRTUAL_ENV"),
        }
    }
}

fn venv_interpreter(venv: &str) -> PathBuf {
    if cfg!(windows) {
        PathBuf::from(venv).join("Scripts").join("python.exe")
    } else {
        PathBuf::from(venv).join("bin").join("python")
    }
}

fn bundled_interpreter(root: &std::path::Path) -> PathBuf {
    if cfg!(windows) {
        root.join(".venv").join("Scripts").join("python.exe")
    } else {
        root.join(".venv").join("bin").join("python")
    }
}

/// Resolve the Python interpreter, in the documented order.
pub fn python_bin() -> Result<String, String> {
    resolve_python(&PythonEnv::from_process())
}

fn resolve_python(env: &PythonEnv) -> Result<String, String> {
    if let Some(explicit) = &env.explicit {
        return Ok(explicit.clone());
    }

    if let Some(venv) = &env.venv {
        let candidate = venv_interpreter(venv);
        if candidate.exists() {
            return Ok(candidate.to_string_lossy().to_string());
        }
    }

    let root = project_root()?;
    let bundled = bundled_interpreter(&root);
    if bundled.exists() {
        return Ok(bundled.to_string_lossy().to_string());
    }

    for candidate in ["python", "python3"] {
        if Command::new(candidate)
            .arg("--version")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
        {
            return Ok(candidate.to_string());
        }
    }

    Err("Could not resolve a Python interpreter. Set LUMINE_PYTHON_BIN or activate a venv.".to_string())
}

/// Run a script from `agent/` and return its stdout.
///
/// Used by the settings commands to ask the worker side for data it owns, such as
/// the provider catalog, instead of duplicating that knowledge in Rust.
pub fn run_agent_script(script: &str, args: &[&str]) -> Result<String, String> {
    run_agent_script_with_env(script, args, &[])
}

/// How long a helper script may run before it is killed.
///
/// The helpers are all short, but "short" is a property of the machine and the
/// library it is importing, not of the code. A cold `python agent/tool_catalog.py`
/// pays for importing `livekit-agents` before it prints anything, which is about
/// six seconds on an ordinary install and rather longer on a cold spinning disk
/// or a first run with antivirus scanning every `.pyd`.
///
/// Without a deadline a helper that never exits is indistinguishable from one
/// that is slow, and the difference matters: a slow helper eventually answers,
/// while a hung one takes the window's "not responding" state with it and offers
/// no way back except killing the app. So the wait is bounded, and the timeout is
/// reported as a timeout rather than as a failure the user has to decode.
///
/// Well above the worst legitimate case, because the failure this guards against
/// is a hang and the cost of being generous is only that a genuinely dead
/// interpreter takes this long to be noticed.
const HELPER_TIMEOUT: Duration = Duration::from_secs(45);

/// The poll interval while waiting on a child.
///
/// Short enough that a script finishing between two polls costs nothing visible,
/// long enough that a six-second import is not 300 wakeups of a thread that is
/// otherwise idle on a blocking pool.
const POLL_INTERVAL: Duration = Duration::from_millis(20);

/// The same, with extra environment variables for the child only.
///
/// Used by the credential test: the secret is placed in the child's environment
/// so it reaches the script that needs it without ever being passed as an
/// argument, where it would appear in the process list.
pub fn run_agent_script_with_env(
    script: &str,
    args: &[&str],
    env: &[(&str, &str)],
) -> Result<String, String> {
    let python = python_bin()?;
    let path = agent_dir()?.join(script);

    let mut command = Command::new(python);
    command
        .arg(&path)
        .args(args)
        .current_dir(project_root()?)
        // Closed rather than inherited. A helper that reads stdin it was never
        // given blocks forever, and inheriting means it can read the desktop
        // app's console instead of failing fast.
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (name, value) in env {
        command.env(name, value);
    }

    let mut child = command
        .spawn()
        .map_err(|err| format!("Could not run {script}: {err}"))?;

    // Drained on their own threads, because a pipe has a finite buffer: a helper
    // that printed more than it filled and was then waited on with nobody
    // reading would deadlock, and `wait_with_output` cannot express a deadline
    // alongside that drain.
    let stdout = child.stdout.take().map(drain);
    let stderr = child.stderr.take().map(drain);

    let deadline = Instant::now() + HELPER_TIMEOUT;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    let _ = join(stdout);
                    let _ = join(stderr);
                    return Err(format!(
                        "{script} did not finish within {} seconds. The Python interpreter may be \
                         missing a dependency, or blocked on something it has no answer for.",
                        HELPER_TIMEOUT.as_secs()
                    ));
                }
                std::thread::sleep(POLL_INTERVAL);
            }
            Err(err) => return Err(format!("Could not run {script}: {err}")),
        }
    };

    let stderr = join(stderr).unwrap_or_default();
    if !status.success() {
        let text = String::from_utf8_lossy(&stderr);
        let detail = text.lines().last().unwrap_or("no error output").trim();
        return Err(format!("{script} failed: {detail}"));
    }

    let stdout = join(stdout).unwrap_or_default();
    String::from_utf8(stdout).map_err(|_| format!("{script} produced invalid UTF-8."))
}

/// Read a child's pipe to end on its own thread, returning the handle to join.
fn drain<R: Read + Send + 'static>(mut pipe: R) -> std::thread::JoinHandle<Vec<u8>> {
    std::thread::spawn(move || {
        let mut buffer = Vec::new();
        // A read error here is reported by the status check instead: the bytes
        // that did arrive are what the caller needs to explain the failure.
        let _ = pipe.read_to_end(&mut buffer);
        buffer
    })
}

fn join(handle: Option<std::thread::JoinHandle<Vec<u8>>>) -> Option<Vec<u8>> {
    handle.and_then(|handle| handle.join().ok())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn project_root_contains_the_agent_directory() {
        let root = project_root().expect("project root");
        assert!(root.join("agent").join("agent.py").exists());
    }

    #[test]
    fn agent_dir_is_under_the_project_root() {
        assert_eq!(
            agent_dir().expect("agent dir"),
            project_root().expect("project root").join("agent")
        );
    }

    #[test]
    fn python_resolution_returns_something_runnable() {
        let python = python_bin().expect("a python interpreter");
        assert!(!python.trim().is_empty());
    }

    #[test]
    fn an_explicit_override_is_honoured() {
        let marker = if cfg!(windows) { "C:/fake/python.exe" } else { "/fake/python" };
        let env = PythonEnv {
            explicit: Some(marker.to_string()),
            venv: None,
        };
        assert_eq!(resolve_python(&env).expect("resolve"), marker);
    }

    #[test]
    fn an_explicit_override_beats_an_active_venv() {
        let env = PythonEnv {
            explicit: Some("explicit-python".to_string()),
            venv: Some("some-venv".to_string()),
        };
        assert_eq!(resolve_python(&env).expect("resolve"), "explicit-python");
    }

    #[test]
    fn a_missing_venv_interpreter_falls_through_to_the_next_candidate() {
        // A venv that does not exist must not shadow the repository .venv.
        let env = PythonEnv {
            explicit: None,
            venv: Some("definitely-not-a-real-venv-path".to_string()),
        };
        let resolved = resolve_python(&env).expect("resolve");
        assert!(!resolved.contains("definitely-not-a-real-venv-path"));
    }

    #[test]
    fn blank_environment_values_are_ignored() {
        let env = PythonEnv::from_process();
        for value in [&env.explicit, &env.venv].into_iter().flatten() {
            assert!(!value.trim().is_empty(), "blank values must be filtered out");
        }
    }

    #[test]
    fn the_bundled_path_follows_the_platform_convention() {
        let root = std::path::Path::new("root");
        let bundled = bundled_interpreter(root);
        let expected_suffix = if cfg!(windows) {
            std::path::Path::new("Scripts").join("python.exe")
        } else {
            std::path::Path::new("bin").join("python")
        };
        assert!(bundled.ends_with(&expected_suffix));
    }
}
