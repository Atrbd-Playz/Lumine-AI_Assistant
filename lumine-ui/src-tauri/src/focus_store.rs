//! Versioned storage for the Focus Workspace document.
//!
//! ## Why a second store rather than a configurable `settings_store`
//!
//! The pattern here is deliberately `settings_store.rs`, down to the
//! temp-file/backup/rename sequence and the error enum shape. What it
//! deliberately is *not* is the same store with a bigger constant, for two
//! reasons that are about correctness rather than taste:
//!
//! - **The size ceiling is a security bound, not a tuning knob.** 512 KB is a
//!   deliberate limit on a document that a person edits by hand. A focus
//!   document holds session *history* — one row per completed pomodoro, growing
//!   forever — so it legitimately needs megabytes. Raising the shared constant to
//!   accommodate it would weaken the bound on the settings file, which is the one
//!   document where a pathological size actually matters.
//! - **The error sentences are about configuration.** `ConfigError::Malformed`
//!   renders as "The configuration is not valid JSON", and
//!   `UnsupportedVersion` as "Configuration version 7 is not supported by this
//!   build." A corrupt focus file reported either of those would send somebody to
//!   the AI settings screen for a file that screen has never heard of.
//!
//! ## What this file must never do
//!
//! It must never *repair* a document by overwriting it. A corrupt focus file is
//! reported to the caller, which surfaces it, and the user's bytes stay on disk.
//! The one exception is a version this build knows how to migrate, which is a
//! forward move rather than a repair — see [`migrate`].

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;

/// Overrides the resolved location, for a packaged build, a test, or a developer.
///
/// Mirrors `settings_store::CONFIG_PATH_ENV` rather than inventing a name, and
/// `agent/focus_store.py` searches the same variable so both halves agree.
pub const FOCUS_PATH_ENV: &str = "LUMINE_FOCUS_PATH";

pub const FOCUS_FILENAME: &str = "focus.json";
pub const FOCUS_VERSION: i64 = 1;

/// How long a session record is kept.
///
/// Pruning is a *retention policy*, stated here rather than implied by a delete.
/// Past a year the charts aggregate identically and the file would otherwise grow
/// without bound. See [`prune_sessions`] for the rule that makes this safe.
pub const SESSION_RETENTION_DAYS: i64 = 365;

/// 8 MB. A year of pomodoros at four per working day is roughly 1,000 rows of
/// well under a kilobyte, so this is generous rather than tight — and it is still
/// a ceiling, because the frontend is an untrusted source of this document and a
/// runaway append loop must not be able to fill a disk.
const MAX_DOCUMENT_BYTES: u64 = 8 * 1024 * 1024;

/// How many daily intentions a day may hold.
///
/// Enforced here as well as in the UI because the UI is not the only writer, and
/// "up to three" that silently becomes "up to three hundred" is how a widget
/// becomes a table nobody can scan.
pub const MAX_INTENTIONS_PER_DAY: usize = 3;

#[derive(Debug)]
pub enum FocusError {
    Io(String),
    Malformed(String),
    TooLarge,
    UnsupportedVersion(i64),
}

impl std::fmt::Display for FocusError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FocusError::Io(detail) => write!(f, "Could not access your focus data: {detail}"),
            FocusError::Malformed(detail) => {
                write!(f, "Your focus data is not valid: {detail}")
            }
            FocusError::TooLarge => {
                write!(f, "Your focus data file is unexpectedly large.")
            }
            FocusError::UnsupportedVersion(version) => write!(
                f,
                "This focus data was written by a newer version of Lumine (v{version}). Update the app to read it."
            ),
        }
    }
}

impl std::error::Error for FocusError {}

/// Where the focus document lives.
pub fn focus_path(app_data_dir: &Path) -> PathBuf {
    resolve_focus_path(app_data_dir, std::env::var(FOCUS_PATH_ENV).ok().as_deref())
}

/// The pure form of [`focus_path`].
///
/// Split out for the same reason `settings_store::resolve_config_path` is: a test
/// that mutates a process-global environment variable races every other test in
/// the binary under `cargo test`'s threads.
pub fn resolve_focus_path(app_data_dir: &Path, override_value: Option<&str>) -> PathBuf {
    match override_value.map(str::trim) {
        Some(non_empty) if !non_empty.is_empty() => PathBuf::from(non_empty),
        _ => app_data_dir.join(FOCUS_FILENAME),
    }
}

/// A complete, valid, empty document.
///
/// This is the shape every reader agrees on, which is why it is written once here
/// and mirrored — with the comment saying it is mirrored — in
/// `agent/focus_store.py` and `src/pages/focus/types.ts`. Three hand-written
/// copies of one schema is a real risk, and the mitigation is that
/// `scripts/verify-focus-contract.mts` compares them rather than trusting them.
pub fn default_document() -> Value {
    serde_json::json!({
        "version": FOCUS_VERSION,
        "settings": default_settings(),
        "widgets": default_widgets(),
        "todos": [],
        "notes": [],
        "sessions": [],
        "customLists": [],
        "music": {
            "youtube": { "current": null, "queue": [], "volume": 0.6 },
            "local": { "queue": [], "index": -1 }
        },
        "dayIntents": []
    })
}

/// The timer defaults, declared apart from [`default_document`] so the settings
/// screen has one place to read the shipped values from rather than hardcoding
/// them beside its own controls.
pub fn default_settings() -> Value {
    serde_json::json!({
        "workMin": 25,
        "shortMin": 5,
        "longMin": 15,
        "longEvery": 4,
        "autoStart": false,
        "autoAdvance": true,
        "dailyGoalMin": 120,
        "ambientSound": "none",
        "notifications": true,
        "layoutPreset": "default"
    })
}

/// The default board, as grid units on a four-column grid.
///
/// `x`/`y` are cell coordinates and `w`/`h` are spans, not pixels — see
/// `src/pages/focus/board/useWidgetLayout.ts`. Writing them as numbers here
/// rather than as a named preset keeps the store free of layout policy; which
/// preset is *active* is `settings.layoutPreset`, and that is a string precisely so
/// a new preset does not need a document migration.
fn default_widgets() -> Value {
    serde_json::json!([
        { "id": "intentions", "x": 0, "y": 0, "w": 4, "h": 1, "hidden": false },
        { "id": "timer",     "x": 0, "y": 1, "w": 2, "h": 3, "hidden": false },
        { "id": "todos",     "x": 2, "y": 1, "w": 2, "h": 3, "hidden": false },
        { "id": "notes",     "x": 0, "y": 4, "w": 2, "h": 3, "hidden": false },
        { "id": "music",     "x": 2, "y": 4, "w": 2, "h": 2, "hidden": false },
        { "id": "charts",    "x": 2, "y": 6, "w": 2, "h": 2, "hidden": false }
    ])
}

/// Read and shape-check the focus document.
///
/// Returns `Ok(None)` when there is no file yet, which is the normal state on a
/// first visit to the Focus page and must not be an error.
///
/// Unlike `settings_store::load`, a *migratable* older version is not an error
/// here: this document carries a person's study history, and refusing to read it
/// because of a version number would be a worse failure than reading it and
/// moving it forward. A version from the *future* is still an error, because this
/// build cannot know what it would be dropping.
pub fn load(path: &Path) -> Result<Option<Value>, FocusError> {
    if !path.exists() {
        return Ok(None);
    }

    let metadata = fs::metadata(path).map_err(|err| FocusError::Io(err.to_string()))?;
    if metadata.len() > MAX_DOCUMENT_BYTES {
        return Err(FocusError::TooLarge);
    }

    let raw = fs::read_to_string(path).map_err(|err| FocusError::Io(err.to_string()))?;
    if raw.trim().is_empty() {
        return Ok(None);
    }

    let mut value: Value =
        serde_json::from_str(&raw).map_err(|err| FocusError::Malformed(err.to_string()))?;
    if !value.is_object() {
        return Err(FocusError::Malformed("the top level must be an object".into()));
    }

    let version = value.get("version").and_then(Value::as_i64).unwrap_or(0);
    if version > FOCUS_VERSION {
        return Err(FocusError::UnsupportedVersion(version));
    }
    if version < FOCUS_VERSION {
        migrate(version, &mut value);
    }

    // A migrated document still has to be a document. Migration adds keys; it
    // cannot vouch for the ones that were already there.
    normalise(&mut value);

    Ok(Some(value))
}

/// Move a document forward to [`FOCUS_VERSION`].
///
/// Returns whether anything changed, so a caller can decide whether the migration
/// is worth writing back — it is not, if the document was already current.
///
/// ## Why this is not `settings_store`'s situation
///
/// `settings_store::load` *rejects* an older version and falls back to
/// `agent/.env`. That is correct there: configuration is a preference, and the
/// environment is a working substitute. It would be wrong here. A focus document
/// is history — the difference between "I studied four hours yesterday" and "I
/// did not" is the user's, and a version bump that hides it is data loss wearing a
/// compatibility hat.
pub fn migrate(from_version: i64, document: &mut Value) -> bool {
    if from_version >= FOCUS_VERSION {
        return false;
    }

    // v0 -> v1: every key below was introduced in v1. Adding the missing ones with
    // their shipped defaults is the whole migration, and it is why the defaults
    // live in one function rather than being written out three times.
    let root = match document.as_object_mut() {
        Some(root) => root,
        // `load` has already rejected a non-object root, so this arm is only
        // reachable if a caller passes something odd directly. Leaving the
        // document alone is safer than inventing an object over it.
        None => return false,
    };

    let settings = default_settings();
    let mut merged = match root.remove("settings") {
        Some(Value::Object(existing)) => existing,
        _ => serde_json::Map::new(),
    };
    if let Value::Object(defaults) = &settings {
        for (key, value) in defaults {
            merged.entry(key.clone()).or_insert_with(|| value.clone());
        }
    }
    root.insert("settings".into(), Value::Object(merged));

    for key in ["todos", "notes", "sessions", "customLists", "dayIntents"] {
        if !root.get(key).map(Value::is_array).unwrap_or(false) {
            root.insert(key.into(), Value::Array(Vec::new()));
        }
    }

    let has_music = root.get("music").map(Value::is_object).unwrap_or(false);
    if !has_music {
        root.insert(
            "music".into(),
            serde_json::json!({
                "youtube": { "current": null, "queue": [], "volume": 0.6 },
                "local": { "queue": [], "index": -1 }
            }),
        );
    }

    root.insert("version".into(), serde_json::json!(FOCUS_VERSION));
    true
}

/// Fill in what a document is missing and clamp what it should not contain.
///
/// This is the reader's half of the contract, and it is deliberately forgiving:
/// a missing key takes its default, a wrong-typed key is replaced, and a
/// *present and well-formed* key is never touched. Anything else would mean the
/// reader could overwrite a person's data because a field looked unfamiliar —
/// which is the failure mode "never silently reset user data" exists to prevent.
///
/// Validation that must *refuse* rather than repair lives in [`validate`], which
/// runs on the way *in* (save). On the way out, repairing is the correct answer,
/// because refusing to open the page is worse than opening it with a default.
fn normalise(document: &mut Value) {
    let defaults = default_document();
    let Some(root) = document.as_object_mut() else {
        return;
    };

    if !root.get("version").and_then(Value::as_i64).is_some_and(|v| v == FOCUS_VERSION) {
        root.insert("version".into(), serde_json::json!(FOCUS_VERSION));
    }

    for key in ["settings", "widgets", "todos", "notes", "sessions", "customLists", "music", "dayIntents"] {
        if !root.contains_key(key) {
            if let Some(value) = defaults.get(key) {
                root.insert(key.into(), value.clone());
            }
        }
    }

    // `todos`/`notes`/`sessions` that exist but are not arrays are unusable, and
    // leaving them would make every consumer crash on `.filter`. Replacing them
    // with an empty list discards nothing a reader could have used.
    for key in ["todos", "notes", "sessions", "customLists", "dayIntents"] {
        if !root.get(key).map(Value::is_array).unwrap_or(false) {
            root.insert(key.into(), Value::Array(Vec::new()));
        }
    }

    prune_sessions(document);
    clamp_intentions(document);
}

/// Drop session rows older than [`SESSION_RETENTION_DAYS`].
///
/// ## The rule that makes this safe
///
/// A row is kept unless it is **provably** older than the cut-off. Three cases
/// are therefore *kept*, and each one is a real way to lose somebody's history by
/// accident:
///
/// - **A missing, null, non-numeric or negative `startedAt`.** It cannot be placed
///   in time, so it cannot be judged expired. Deleting it would silently discard
///   a session whose only timestamp was corrupted.
/// - **A `startedAt` in the future.** A clock change — or a document written by a
///   machine whose timezone was wrong — produces these routinely. "Older than a
///   year" is false for a future timestamp, so arithmetic alone keeps them; the
///   explicit check exists so that changing the comparison below cannot quietly
///   invert that.
/// - **A row that is not an object.** There is no timestamp to read, so no verdict.
///
/// Note what is *not* checked: whether the row looks otherwise sensible. This is a
/// retention policy, not a validator, and the two are kept apart so that "valid"
/// and "recent" never get confused for each other.
pub fn prune_sessions(document: &mut Value) {
    let Some(root) = document.as_object_mut() else {
        return;
    };
    let Some(Value::Array(sessions)) = root.get_mut("sessions") else {
        return;
    };

    let cutoff = now_millis() - (SESSION_RETENTION_DAYS as f64) * 86_400_000.0;

    sessions.retain(|session| {
        let Some(entry) = session.as_object() else {
            // Not an object: unplaceable, therefore kept.
            return true;
        };
        let Some(started) = entry.get("startedAt").and_then(Value::as_f64) else {
            return true;
        };
        if !started.is_finite() || started < 0.0 {
            return true;
        }
        if started > now_millis() {
            return true;
        }
        started >= cutoff
    });
}

/// Enforce [`MAX_INTENTIONS_PER_DAY`] per calendar date, keeping the last three.
///
/// ## Why "the last three typed" rather than "the newest three"
///
/// An intention row is `{date, text, done}` and nothing else — there is no
/// timestamp on it. So "newest" cannot be evaluated, and a rule that *claims* to
/// keep the newest while actually keeping whichever happened to be serialised first
/// is worse than no rule at all, because it looks deliberate.
///
/// Document order is the only ordering available, and it is the right one: it is
/// the order the person typed them in, so the three that survive a fourth are the
/// three they most recently decided mattered. Dates are still grouped so the cap is
/// per day and not global — keeping two per day across a year is not "three a day".
///
/// A row with no `date` is **kept**. It cannot be grouped, and pruning on the
/// strength of a field that is not there would delete rows for no stated reason.
pub fn clamp_intentions(document: &mut Value) {
    let Some(root) = document.as_object_mut() else {
        return;
    };
    let Some(Value::Array(intents)) = root.get_mut("dayIntents") else {
        return;
    };

    let mut kept: Vec<Value> = Vec::with_capacity(intents.len());
    // Walk backwards so "the last three" is a real answer, then reverse to put the
    // surviving rows back in the order they were typed.
    let mut seen: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for intent in intents.iter().rev() {
        match intent.get("date").and_then(Value::as_str) {
            None => kept.push(intent.clone()),
            Some(date) => {
                let count = seen.entry(date.to_string()).or_insert(0);
                if *count >= MAX_INTENTIONS_PER_DAY {
                    continue;
                }
                *count += 1;
                kept.push(intent.clone());
            }
        }
    }
    kept.reverse();
    *intents = kept;
}

/// Wall-clock milliseconds since the epoch.
///
/// Split out so the retention tests can drive time without sleeping, and so the
/// one place that reads the clock is greppable.
fn now_millis() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as f64)
        .unwrap_or(0.0)
}

/// Refuse a document that should not be written.
///
/// This is the write-side counterpart to [`normalise`], and the asymmetry is the
/// point. On **save** a malformed document is refused and the error names the
/// field, because the caller is a bug or a hostile webview and repairing it would
/// hide both. On **load** the same document is repaired, because refusing there
/// means the user cannot open the page at all.
///
/// Checks, in the order a user would want them reported:
///
/// 1. root is an object, at [`FOCUS_VERSION`];
/// 2. no array is absurdly long (a runaway loop, not a person);
/// 3. every row in a row-array is an object;
/// 4. numeric fields are numbers within sane bounds;
/// 5. at most [`MAX_INTENTIONS_PER_DAY`] intentions per date.
pub fn validate(document: &Value) -> Result<(), FocusError> {
    let Some(root) = document.as_object() else {
        return Err(FocusError::Malformed("the top level must be an object".into()));
    };

    let version = root.get("version").and_then(Value::as_i64).unwrap_or(0);
    if version != FOCUS_VERSION {
        return Err(FocusError::UnsupportedVersion(version));
    }

    for key in ["todos", "notes", "sessions", "customLists", "dayIntents"] {
        let Some(value) = root.get(key) else {
            continue;
        };
        let Some(items) = value.as_array() else {
            return Err(FocusError::Malformed(format!("`{key}` must be a list")));
        };
        if items.len() > MAX_ROWS {
            return Err(FocusError::Malformed(format!(
                "`{key}` holds {} entries, which is more than this app writes. Nothing was saved.",
                items.len()
            )));
        }
        if items.iter().any(|item| !item.is_object()) {
            return Err(FocusError::Malformed(format!(
                "`{key}` must hold only records"
            )));
        }
    }

    if let Some(settings) = root.get("settings") {
        validate_settings(settings)?;
    }

    if let Some(Value::Array(sessions)) = root.get("sessions") {
        for session in sessions {
            validate_session(session)?;
        }
    }

    Ok(())
}

/// An upper bound on any one row collection.
///
/// Chosen from the retention policy: 365 days of pomodoros cannot reach it, so a
/// document over the limit is not a person who studied a great deal — it is a loop
/// that is not stopping.
const MAX_ROWS: usize = 50_000;

fn validate_settings(settings: &Value) -> Result<(), FocusError> {
    let Some(entries) = settings.as_object() else {
        return Err(FocusError::Malformed("`settings` must be an object".into()));
    };

    // Bounded on both sides. A zero or negative phase length would make the timer
    // divide by zero and a 10,000-minute phase would make it useless; neither is
    // a value a person sets on purpose.
    for key in ["workMin", "shortMin", "longMin"] {
        if let Some(value) = entries.get(key) {
            let minutes = value
                .as_f64()
                .ok_or_else(|| FocusError::Malformed(format!("`{key}` must be a number")))?;
            if !(1.0..=180.0).contains(&minutes) {
                return Err(FocusError::Malformed(format!(
                    "`{key}` must be between 1 and 180 minutes."
                )));
            }
        }
    }

    if let Some(value) = entries.get("longEvery") {
        let every = value
            .as_f64()
            .ok_or_else(|| FocusError::Malformed("`longEvery` must be a number".into()))?;
        if !(1.0..=12.0).contains(&every) {
            return Err(FocusError::Malformed(
                "`longEvery` must be between 1 and 12.".into(),
            ));
        }
    }

    if let Some(value) = entries.get("dailyGoalMin") {
        let goal = value
            .as_f64()
            .ok_or_else(|| FocusError::Malformed("`dailyGoalMin` must be a number".into()))?;
        if !(0.0..=1440.0).contains(&goal) {
            return Err(FocusError::Malformed(
                "`dailyGoalMin` must be between 0 and 1440.".into(),
            ));
        }
    }

    if let Some(value) = entries.get("layoutPreset") {
        let preset = value
            .as_str()
            .ok_or_else(|| FocusError::Malformed("`layoutPreset` must be text".into()))?;
        if !LAYOUT_PRESETS.contains(&preset) {
            return Err(FocusError::Malformed(format!(
                "`layoutPreset` must be one of: {}.",
                LAYOUT_PRESETS.join(", ")
            )));
        }
    }

    Ok(())
}

/// The preset names a document may name.
///
/// `custom` is here, not only in the frontend, because it is what a document
/// records the moment a person moves a widget — and a preset list that did not
/// include it would reject the save of a perfectly ordinary drag.
pub const LAYOUT_PRESETS: [&str; 5] = ["default", "study", "writing", "deepWork", "custom"];

fn validate_session(session: &Value) -> Result<(), FocusError> {
    let Some(entry) = session.as_object() else {
        return Err(FocusError::Malformed("a session must be a record".into()));
    };

    for key in ["startedAt", "endedAt"] {
        if let Some(value) = entry.get(key) {
            if value.is_null() {
                continue;
            }
            let stamp = value
                .as_f64()
                .ok_or_else(|| FocusError::Malformed(format!("`{key}` must be a timestamp")))?;
            // Negative or non-finite timestamps are what a corrupt write produces,
            // and they would silently poison every date bucket downstream — a
            // session sorting into 1970 is a chart that reads as empty forever.
            if !stamp.is_finite() || stamp < 0.0 {
                return Err(FocusError::Malformed(format!(
                    "`{key}` is not a usable timestamp."
                )));
            }
        }
    }

    if let Some(value) = entry.get("actualMin") {
        let minutes = value
            .as_f64()
            .ok_or_else(|| FocusError::Malformed("`actualMin` must be a number".into()))?;
        if !(0.0..=1440.0).contains(&minutes) {
            return Err(FocusError::Malformed(
                "`actualMin` must be between 0 and 1440.".into(),
            ));
        }
    }

    if let Some(value) = entry.get("interruptions") {
        let count = value
            .as_f64()
            .ok_or_else(|| FocusError::Malformed("`interruptions` must be a number".into()))?;
        if !(0.0..=10_000.0).contains(&count) {
            return Err(FocusError::Malformed(
                "`interruptions` must be a non-negative count.".into(),
            ));
        }
    }

    Ok(())
}

/// Write the focus document, then make sure it can be read back.
///
/// The sequence is validate, serialise, write-temp, back up the current file,
/// rename into place — copied from `settings_store::save` so that a reader who
/// knows one store knows this one. The backup is best-effort and its failure is
/// not fatal, because the rename is what actually protects the previous file.
///
/// ## On the "atomic" claim
///
/// `fs::rename` is atomic within a filesystem on POSIX and on Windows for a
/// same-volume replace, which is the only case here (the temp file is a sibling of
/// the target by construction). It is **not** durability: nothing calls `fsync`,
/// so a power loss can leave a renamed-but-empty file on some hardware. That is
/// why the backup exists and why [`load`] treats an unreadable file as an error to
/// report rather than as an empty document to overwrite. A `sync_all` would
/// narrow the window and cost a full flush on every autosave, which on a 250 ms
/// debounce is the wrong trade for a file whose loss is recoverable from `.bak`.
///
/// Recovery from an interrupted write is therefore: the target is either the
/// previous document or the new one, and if a caller finds the target unreadable,
/// `focus.json.bak` still holds the last good copy. Nothing else is assumed.
pub fn save(path: &Path, document: &Value) -> Result<(), FocusError> {
    validate(document)?;

    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|err| FocusError::Io(err.to_string()))?;
    }

    let serialized = serde_json::to_string_pretty(document)
        .map_err(|err| FocusError::Malformed(err.to_string()))?;

    // Checked before the write, not only on read. A document this size is either a
    // runaway loop or a mistake, and discovering it by filling a disk is the worse
    // of the two orders.
    if serialized.len() as u64 > MAX_DOCUMENT_BYTES {
        return Err(FocusError::TooLarge);
    }

    let temp = path.with_extension("json.tmp");
    fs::write(&temp, serialized.as_bytes()).map_err(|err| FocusError::Io(err.to_string()))?;

    if path.exists() {
        let backup = path.with_extension("json.bak");
        let _ = fs::copy(path, &backup);
    }

    fs::rename(&temp, path).map_err(|err| {
        let _ = fs::remove_file(&temp);
        FocusError::Io(err.to_string())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("lumine-focus-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("temp dir");
        dir
    }

    fn day_ago(days: f64) -> Value {
        serde_json::json!(now_millis() - days * 86_400_000.0)
    }

    fn session(started: Value) -> Value {
        serde_json::json!({
            "id": "s1",
            "startedAt": started,
            "endedAt": started,
            "plannedMin": 25,
            "actualMin": 25,
            "phase": "work",
            "subject": "Maths",
            "todoId": Value::Null,
            "noteId": Value::Null,
            "completed": true,
            "interruptions": 0
        })
    }

    fn doc_with_sessions(sessions: Vec<Value>) -> Value {
        let mut document = default_document();
        document["sessions"] = Value::Array(sessions);
        document
    }

    /* -- the basics, mirroring settings_store -- */

    #[test]
    fn a_missing_file_is_not_an_error() {
        let dir = temp_dir("missing");
        let loaded = load(&dir.join(FOCUS_FILENAME)).expect("absence is normal");
        assert!(loaded.is_none());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn round_trips_a_document() {
        let dir = temp_dir("roundtrip");
        let path = dir.join(FOCUS_FILENAME);
        save(&path, &default_document()).expect("save");
        let loaded = load(&path).expect("load").expect("a document");
        assert_eq!(loaded["version"], FOCUS_VERSION);
        assert_eq!(loaded["settings"]["workMin"], 25);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn saving_creates_missing_parent_directories() {
        let dir = temp_dir("nested");
        let path = dir.join("a").join("b").join(FOCUS_FILENAME);
        save(&path, &default_document()).expect("save should create parents");
        assert!(path.exists());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_second_save_keeps_a_backup() {
        let dir = temp_dir("backup");
        let path = dir.join(FOCUS_FILENAME);
        save(&path, &default_document()).expect("first save");

        let mut updated = default_document();
        updated["settings"]["workMin"] = serde_json::json!(50);
        save(&path, &updated).expect("second save");

        assert!(path.with_extension("json.bak").exists(), "expected a .bak");
        assert_eq!(load(&path).expect("load").expect("doc")["settings"]["workMin"], 50);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_failed_write_leaves_no_temp_file_behind() {
        let dir = temp_dir("failedwrite");
        let path = dir.join("blocked");
        fs::create_dir_all(&path).expect("a directory where the file should be");
        assert!(save(&path, &default_document()).is_err());
        assert!(
            !path.with_extension("json.tmp").exists(),
            "a temp file was left behind after a failed write"
        );
        let _ = fs::remove_dir_all(&dir);
    }

    /* -- corrupt data is reported, never repaired away -- */

    #[test]
    fn malformed_json_is_reported_and_the_file_is_left_alone() {
        let dir = temp_dir("malformed");
        let path = dir.join(FOCUS_FILENAME);
        fs::write(&path, "{not json").expect("write");
        assert!(matches!(load(&path), Err(FocusError::Malformed(_))));
        // The load must not have touched it. A reader that "fixed" a corrupt file
        // by writing a default over it is how a person's history disappears and
        // they are never told.
        assert_eq!(fs::read_to_string(&path).expect("reread"), "{not json");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_non_object_root_is_rejected() {
        let dir = temp_dir("arrayroot");
        let path = dir.join(FOCUS_FILENAME);
        fs::write(&path, "[]").expect("write");
        assert!(matches!(load(&path), Err(FocusError::Malformed(_))));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_newer_version_is_refused_because_this_build_cannot_know_what_it_would_drop() {
        const FROM_THE_FUTURE: i64 = FOCUS_VERSION + 1;
        let dir = temp_dir("toonew");
        let path = dir.join(FOCUS_FILENAME);
        let mut document = default_document();
        document["version"] = serde_json::json!(FROM_THE_FUTURE);
        fs::write(&path, document.to_string()).expect("write");
        assert!(matches!(
            load(&path),
            Err(FocusError::UnsupportedVersion(FROM_THE_FUTURE))
        ));
        let _ = fs::remove_dir_all(&dir);
    }

    /* -- migration: forward, and refusing to lose history -- */

    #[test]
    fn an_older_document_is_migrated_forward_and_gains_its_keys() {
        let mut legacy = serde_json::json!({
            "version": 0,
            "settings": { "workMin": 50 },
            "todos": [{ "id": "t1", "title": "Revise", "done": false }],
            "sessions": [session(day_ago(1.0))]
        });

        assert!(migrate(0, &mut legacy), "a v0 document should be migrated");
        assert_eq!(legacy["version"], FOCUS_VERSION);

        // The one thing migration must never do: overwrite a value the person set.
        assert_eq!(legacy["settings"]["workMin"], 50);
        // And the one it must: supply a key that did not exist.
        assert_eq!(legacy["settings"]["longEvery"], 4);

        assert_eq!(legacy["todos"].as_array().expect("todos").len(), 1);
        assert_eq!(legacy["sessions"].as_array().expect("sessions").len(), 1);
        assert!(legacy["notes"].is_array());
        assert!(legacy["music"]["youtube"]["volume"].is_number());
    }

    #[test]
    fn an_older_document_on_disk_is_read_through_the_migration() {
        let dir = temp_dir("migrateload");
        let path = dir.join(FOCUS_FILENAME);
        fs::write(
            &path,
            serde_json::json!({ "version": 0, "settings": { "shortMin": 3 } }).to_string(),
        )
        .expect("write");

        let loaded = load(&path).expect("an old document is readable").expect("a document");
        assert_eq!(loaded["version"], FOCUS_VERSION);
        assert_eq!(loaded["settings"]["shortMin"], 3, "a set value survives");
        assert_eq!(loaded["settings"]["workMin"], 25, "a missing value is filled in");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn migrating_a_current_document_changes_nothing() {
        let mut document = default_document();
        let before = document.clone();
        assert!(!migrate(FOCUS_VERSION, &mut document));
        assert_eq!(document, before, "an already-current document was rewritten");
    }

    /* -- retention: the three cases that must not delete anybody's history -- */

    #[test]
    fn retention_drops_only_provably_old_sessions() {
        let mut document = doc_with_sessions(vec![
            session(day_ago(2.0)),   // kept
            session(day_ago(364.0)), // kept, just inside the year
            session(day_ago(400.0)), // dropped, provably outside it
        ]);

        prune_sessions(&mut document);

        assert_eq!(
            document["sessions"].as_array().expect("sessions").len(),
            2,
            "only the 400-day-old row may go"
        );
    }

    #[test]
    fn a_session_with_an_unusable_timestamp_is_kept_because_it_cannot_be_judged() {
        let mut no_stamp = session(day_ago(900.0));
        no_stamp["startedAt"] = serde_json::Value::Null;
        let mut negative = session(day_ago(900.0));
        negative["id"] = serde_json::json!("s2");
        negative["startedAt"] = serde_json::json!(-1.0);
        let mut not_a_number = session(day_ago(900.0));
        not_a_number["id"] = serde_json::json!("s3");
        not_a_number["startedAt"] = serde_json::json!("yesterday");

        let mut document = doc_with_sessions(vec![
            session(day_ago(1.0)),
            no_stamp,
            negative,
            not_a_number,
        ]);
        prune_sessions(&mut document);

        assert_eq!(
            document["sessions"].as_array().expect("sessions").len(),
            4,
            "a missing, negative or non-numeric timestamp cannot be placed in time, so \
             those rows must survive -- deleting them would silently discard sessions \
             whose only timestamp was corrupted"
        );
    }

    #[test]
    fn a_future_dated_session_is_kept_because_a_clock_change_is_not_an_expiry() {
        let future = serde_json::json!(now_millis() + 86_400_000.0);
        let mut document = doc_with_sessions(vec![session(future)]);
        prune_sessions(&mut document);
        assert_eq!(
            document["sessions"].as_array().expect("sessions").len(),
            1,
            "a future timestamp must never read as 'older than a year'"
        );
    }

    /* -- validation refuses what it should, on the way in -- */

    #[test]
    fn an_absurd_row_count_is_refused_before_it_can_fill_a_disk() {
        let mut document = default_document();
        document["todos"] = Value::Array(
            (0..MAX_ROWS + 1)
                .map(|i| serde_json::json!({ "id": format!("t{i}") }))
                .collect(),
        );
        assert!(matches!(validate(&document), Err(FocusError::Malformed(_))));
    }

    #[test]
    fn a_nonsensical_phase_length_is_refused() {
        for bad in [serde_json::json!(0), serde_json::json!(-5), serde_json::json!(9999)] {
            let mut document = default_document();
            document["settings"]["workMin"] = bad.clone();
            assert!(
                validate(&document).is_err(),
                "{bad} minutes should not be saveable"
            );
        }
    }

    #[test]
    fn an_unknown_layout_preset_is_refused_and_the_list_names_the_valid_ones() {
        let mut document = default_document();
        document["settings"]["layoutPreset"] = serde_json::json!("spiral");
        let error = validate(&document).expect_err("an unknown preset");
        assert!(error.to_string().contains("default"), "{error}");
    }

    #[test]
    fn a_negative_timestamp_is_refused_because_it_poisons_every_date_bucket() {
        let mut document = default_document();
        document["sessions"] = Value::Array(vec![session(serde_json::json!(-1.0))]);
        let error = validate(&document).expect_err("a negative timestamp");
        assert!(error.to_string().contains("timestamp"), "{error}");
    }

    #[test]
    fn a_row_that_is_not_a_record_is_refused() {
        let mut document = default_document();
        document["notes"] = serde_json::json!(["just a string"]);
        assert!(validate(&document).is_err());
    }

    /* -- daily intentions -- */

    #[test]
    fn only_the_last_three_intentions_of_a_day_are_kept() {
        let document = serde_json::json!({
            "version": FOCUS_VERSION,
            "settings": default_settings(),
            "widgets": [],
            "todos": [], "notes": [], "sessions": [], "customLists": [],
            "music": { "youtube": { "current": null, "queue": [], "volume": 0.6 },
                       "local": { "queue": [], "index": -1 } },
            "dayIntents": [
                { "date": "2026-10-04", "text": "one",     "done": false },
                { "date": "2026-10-04", "text": "two",     "done": false },
                { "date": "2026-10-04", "text": "three",   "done": false },
                { "date": "2026-10-04", "text": "four",    "done": false },
                { "date": "2026-10-03", "text": "yesterday", "done": true }
            ]
        });

        let mut mutable = document;
        clamp_intentions(&mut mutable);

        let today: Vec<&str> = mutable["dayIntents"]
            .as_array()
            .expect("intents")
            .iter()
            .filter(|i| i["date"] == "2026-10-04")
            .map(|i| i["text"].as_str().unwrap_or_default())
            .collect();
        assert_eq!(
            today,
            vec!["two", "three", "four"],
            "the first typed is the one dropped -- there is no timestamp to judge \
             'newest' by, so document order is the rule and it is stated"
        );

        // A different day is a different budget, so yesterday's is untouched.
        assert!(mutable["dayIntents"]
            .as_array()
            .expect("intents")
            .iter()
            .any(|i| i["text"] == "yesterday"));

        // And the survivors are still in the order they were typed. Walking backwards
        // and reversing must land back on the original sequence minus the dropped row;
        // forgetting the reverse is how a list of intentions silently reorders itself
        // under the person editing it.
        let order: Vec<&str> = mutable["dayIntents"]
            .as_array()
            .expect("intents")
            .iter()
            .filter_map(|i| i["text"].as_str())
            .collect();
        assert_eq!(
            order, vec!["two", "three", "four", "yesterday"],
            "the surviving rows keep their input order"
        );
    }

    #[test]
    fn an_intention_with_no_date_is_kept_because_it_cannot_be_grouped() {
        let mut document = serde_json::json!({
            "version": FOCUS_VERSION,
            "dayIntents": [{ "text": "undated" }]
        });
        clamp_intentions(&mut document);
        assert_eq!(
            document["dayIntents"].as_array().expect("intents").len(),
            1,
            "pruning on the strength of a field that is not there is how rows disappear \
             for no stated reason"
        );
    }

    /* -- path resolution -- */

    #[test]
    fn the_env_override_wins() {
        let app_data = PathBuf::from("app-data");
        let explicit = PathBuf::from("elsewhere/focus.json");
        assert_eq!(resolve_focus_path(&app_data, Some(&explicit.to_string_lossy())), explicit);
    }

    #[test]
    fn a_blank_override_falls_back_to_the_app_data_directory() {
        let app_data = PathBuf::from("app-data");
        assert_eq!(
            resolve_focus_path(&app_data, Some("   ")),
            app_data.join(FOCUS_FILENAME)
        );
        assert_eq!(resolve_focus_path(&app_data, None), app_data.join(FOCUS_FILENAME));
    }

    /* -- the document a reader agrees on -- */

    #[test]
    fn the_default_document_is_itself_valid() {
        // A default that `validate` rejects would make a first save impossible.
        validate(&default_document()).expect("the shipped defaults must be saveable");
    }

    #[test]
    fn the_default_board_uses_only_known_presets_and_legal_spans() {
        let document = default_document();
        assert_eq!(document["settings"]["layoutPreset"], "default");
        let widgets = document["widgets"].as_array().expect("widgets");
        assert!(!widgets.is_empty(), "the page would open with nothing on it");
        for widget in widgets {
            let width = widget["w"].as_i64().expect("w");
            let height = widget["h"].as_i64().expect("h");
            assert!((1..=4).contains(&width), "{width} is outside the 4-column grid");
            assert!((1..=6).contains(&height), "{height} is outside the allowed span");
        }
    }
}