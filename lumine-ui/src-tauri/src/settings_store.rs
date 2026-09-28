//! Versioned, non-secret configuration storage for the AI Control Center.
//!
//! This is the writer half of the contract that `agent/config_store.py` reads.
//! The file holds profiles, provider enablement, and agent toggles. It never
//! holds credential material — `credentials.rs` owns that, in the OS keyring.
//!
//! Why Rust owns this file rather than the JavaScript store plugin: the worker
//! has to read it, it needs a version and a migration hook, and a crash
//! mid-write must not lose the active profile. Writing it from Rust makes the
//! authoritative copy explicit and lets the write be atomic.
//!
//! Path resolution is shared with the Python side: `LUMINE_CONFIG_PATH` wins,
//! otherwise each runtime uses its own default (Rust: the app data directory;
//! Python: the agent directory). When Rust starts the worker it must pass
//! `LUMINE_CONFIG_PATH` in the child environment so both agree, which is the
//! worker-wiring step rather than this one.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;

pub const CONFIG_PATH_ENV: &str = "LUMINE_CONFIG_PATH";
pub const CONFIG_FILENAME: &str = "lumine.config.json";
pub const CONFIG_VERSION: i64 = 1;

/// The app data directory name used before Lumine took its own name.
///
/// `app_local_data_dir()` is named after the Tauri identifier, so moving the
/// identifier from `com.art.lumine-ui` to `com.art.lumine` moved this file
/// too. Without handling that, every saved profile silently reverts to
/// defaults and the app looks like a fresh install.
///
/// The legacy directory is a *sibling* of the current one, so locating it this
/// way is correct on Windows, macOS and Linux without naming any of them.
const LEGACY_DIR_NAME: &str = "com.art.lumine-ui";

const MAX_DOCUMENT_BYTES: u64 = 512 * 1024;

#[derive(Debug)]
pub enum ConfigError {
    Io(String),
    Malformed(String),
    TooLarge,
    UnsupportedVersion(i64),
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConfigError::Io(detail) => write!(f, "Could not access the configuration: {detail}"),
            ConfigError::Malformed(detail) => write!(f, "The configuration is not valid JSON: {detail}"),
            ConfigError::TooLarge => write!(f, "The configuration file is unexpectedly large."),
            ConfigError::UnsupportedVersion(version) => {
                write!(f, "Configuration version {version} is not supported by this build.")
            }
        }
    }
}

impl std::error::Error for ConfigError {}

/// Where the configuration file lives.
pub fn config_path(app_data_dir: &Path) -> PathBuf {
    resolve_config_path(app_data_dir, std::env::var(CONFIG_PATH_ENV).ok().as_deref())
}

/// The pure form of [`config_path`].
///
/// Split out so the precedence rule can be tested without mutating a
/// process-global environment variable, which races under parallel `cargo test`.
///
/// A file that exists under the pre-rename directory still wins over an absent
/// file in the current one. That is deliberate: a path with nothing at it would
/// report a fresh install, and the user's saved profiles would read as deleted.
/// [`migrate_legacy_config`] moves the file, after which this arm is inert.
pub fn resolve_config_path(app_data_dir: &Path, override_value: Option<&str>) -> PathBuf {
    match override_value.map(str::trim) {
        Some(non_empty) if !non_empty.is_empty() => PathBuf::from(non_empty),
        _ => {
            let current = app_data_dir.join(CONFIG_FILENAME);
            if current.exists() {
                return current;
            }
            legacy_config_path(app_data_dir).unwrap_or(current)
        }
    }
}

/// The pre-rename config file, if it is still there.
fn legacy_config_path(app_data_dir: &Path) -> Option<PathBuf> {
    let candidate = app_data_dir
        .parent()?
        .join(LEGACY_DIR_NAME)
        .join(CONFIG_FILENAME);
    candidate.exists().then_some(candidate)
}

/// Copy a pre-rename config file into the current app data directory.
///
/// Called once during setup. Reads fall back to the old location anyway, so this
/// is about convergence rather than correctness: after it runs, the current
/// directory is authoritative and the old one is left untouched as a backup.
///
/// A failure is not fatal. The read fallback already keeps the user's settings
/// readable, so refusing to start over a copy would trade a cosmetic problem
/// for a real one.
pub fn migrate_legacy_config(app_data_dir: &Path) -> Result<bool, ConfigError> {
    let Some(legacy) = legacy_config_path(app_data_dir) else {
        return Ok(false);
    };
    let current = app_data_dir.join(CONFIG_FILENAME);
    if current.exists() {
        return Ok(false);
    }
    if let Some(parent) = app_data_dir.parent() {
        fs::create_dir_all(parent).map_err(|err| ConfigError::Io(err.to_string()))?;
    }
    fs::create_dir_all(app_data_dir).map_err(|err| ConfigError::Io(err.to_string()))?;
    fs::copy(&legacy, &current).map_err(|err| ConfigError::Io(err.to_string()))?;
    Ok(true)
}

/// Read and shape-check the configuration.
///
/// Returns `Ok(None)` when there is no file yet, which is the normal state for a
/// fresh install. A malformed or unsupported file is an error: the caller
/// surfaces it and the app falls back to `agent/.env` rather than silently
/// discarding whatever the user saved.
pub fn load(path: &Path) -> Result<Option<Value>, ConfigError> {
    if !path.exists() {
        return Ok(None);
    }

    let metadata = fs::metadata(path).map_err(|err| ConfigError::Io(err.to_string()))?;
    if metadata.len() > MAX_DOCUMENT_BYTES {
        return Err(ConfigError::TooLarge);
    }

    let raw = fs::read_to_string(path).map_err(|err| ConfigError::Io(err.to_string()))?;
    if raw.trim().is_empty() {
        return Ok(None);
    }

    let value: Value =
        serde_json::from_str(&raw).map_err(|err| ConfigError::Malformed(err.to_string()))?;
    if !value.is_object() {
        return Err(ConfigError::Malformed("the root must be an object".into()));
    }

    let version = value.get("version").and_then(Value::as_i64).unwrap_or(0);
    if version != CONFIG_VERSION {
        return Err(ConfigError::UnsupportedVersion(version));
    }

    Ok(Some(value))
}

/// Write the configuration atomically.
///
/// The sequence is write-temp, back up the current file, rename into place. A
/// crash at any point leaves either the previous file or the new one, never a
/// half-written one. This is why the JS store plugin is not used for the
/// authoritative copy.
pub fn save(path: &Path, document: &Value) -> Result<(), ConfigError> {
    if !document.is_object() {
        return Err(ConfigError::Malformed("the root must be an object".into()));
    }
    let version = document.get("version").and_then(Value::as_i64).unwrap_or(0);
    if version != CONFIG_VERSION {
        return Err(ConfigError::UnsupportedVersion(version));
    }

    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|err| ConfigError::Io(err.to_string()))?;
    }

    let serialized =
        serde_json::to_string_pretty(document).map_err(|err| ConfigError::Malformed(err.to_string()))?;

    let temp = path.with_extension("json.tmp");
    fs::write(&temp, serialized.as_bytes()).map_err(|err| ConfigError::Io(err.to_string()))?;

    if path.exists() {
        let backup = path.with_extension("json.bak");
        // A failed backup is not fatal; the rename below is what protects the
        // user from a corrupt write.
        let _ = fs::copy(path, &backup);
    }

    fs::rename(&temp, path).map_err(|err| {
        let _ = fs::remove_file(&temp);
        ConfigError::Io(err.to_string())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn document(version: i64) -> Value {
        serde_json::json!({
            "version": version,
            "activeProfileId": "gemini-live",
            "providers": { "google": { "enabled": true, "keyRef": null } },
            "profiles": [{ "id": "gemini-live", "name": "Gemini Live", "kind": "realtime" }],
        })
    }

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("lumine-settings-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("temp dir");
        dir
    }

    #[test]
    fn a_missing_file_is_not_an_error() {
        let dir = temp_dir("missing");
        let loaded = load(&dir.join(CONFIG_FILENAME)).expect("load should tolerate absence");
        assert!(loaded.is_none());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn round_trips_a_document() {
        let dir = temp_dir("roundtrip");
        let path = dir.join(CONFIG_FILENAME);
        save(&path, &document(CONFIG_VERSION)).expect("save");
        let loaded = load(&path).expect("load").expect("document should exist");
        assert_eq!(loaded["activeProfileId"], "gemini-live");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn saving_creates_missing_parent_directories() {
        let dir = temp_dir("nested");
        let path = dir.join("a").join("b").join(CONFIG_FILENAME);
        save(&path, &document(CONFIG_VERSION)).expect("save should create parents");
        assert!(path.exists());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_second_save_keeps_a_backup() {
        let dir = temp_dir("backup");
        let path = dir.join(CONFIG_FILENAME);
        save(&path, &document(CONFIG_VERSION)).expect("first save");
        let mut updated = document(CONFIG_VERSION);
        updated["activeProfileId"] = serde_json::json!("lumine-default");
        save(&path, &updated).expect("second save");

        assert!(path.with_extension("json.bak").exists(), "expected a .bak alongside the file");
        assert_eq!(load(&path).expect("load").expect("doc")["activeProfileId"], "lumine-default");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn malformed_json_is_reported_not_swallowed() {
        let dir = temp_dir("malformed");
        let path = dir.join(CONFIG_FILENAME);
        fs::write(&path, "{not json").expect("write");
        assert!(matches!(load(&path), Err(ConfigError::Malformed(_))));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_non_object_root_is_rejected() {
        let dir = temp_dir("array-root");
        let path = dir.join(CONFIG_FILENAME);
        fs::write(&path, "[]").expect("write");
        assert!(matches!(load(&path), Err(ConfigError::Malformed(_))));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_unsupported_version_is_rejected_on_both_paths() {
        let dir = temp_dir("version");
        let path = dir.join(CONFIG_FILENAME);

        fs::write(&path, document(99).to_string()).expect("write");
        assert!(matches!(load(&path), Err(ConfigError::UnsupportedVersion(99))));

        assert!(matches!(
            save(&path, &document(99)),
            Err(ConfigError::UnsupportedVersion(99))
        ));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_empty_file_reads_as_absent() {
        let dir = temp_dir("empty");
        let path = dir.join(CONFIG_FILENAME);
        fs::write(&path, "   ").expect("write");
        assert!(load(&path).expect("load").is_none());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn env_override_wins_over_the_default_directory() {
        let app_data = PathBuf::from("app-data");
        let explicit = PathBuf::from("elsewhere/custom.json");
        assert_eq!(resolve_config_path(&app_data, Some(&explicit.to_string_lossy())), explicit);
    }

    #[test]
    fn a_blank_override_falls_back_to_the_app_data_directory() {
        let app_data = PathBuf::from("app-data");
        assert_eq!(
            resolve_config_path(&app_data, Some("   ")),
            app_data.join(CONFIG_FILENAME)
        );
    }

    #[test]
    fn an_absent_override_falls_back_to_the_app_data_directory() {
        let app_data = PathBuf::from("app-data");
        assert_eq!(resolve_config_path(&app_data, None), app_data.join(CONFIG_FILENAME));
    }

    #[test]
    fn a_failed_write_leaves_no_temp_file_behind() {
        let dir = temp_dir("failedwrite");
        // A directory in place of the target makes the rename fail.
        let path = dir.join("blocked");
        fs::create_dir_all(&path).expect("dir in place of file");
        assert!(save(&path, &document(CONFIG_VERSION)).is_err());
        assert!(
            !path.with_extension("json.tmp").exists(),
            "a temp file was left behind after a failed write"
        );
        let _ = fs::remove_dir_all(&dir);
    }
}
