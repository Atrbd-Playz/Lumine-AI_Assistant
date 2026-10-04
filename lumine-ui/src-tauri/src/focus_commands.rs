//! The Focus Workspace's four Tauri commands.
//!
//! ## Why this is a module and not four functions in `lib.rs`
//!
//! `lib.rs` already holds the twenty-odd commands that predate the pattern of
//! keeping a concern in its own module, and the three that came later
//! (`setup_status`, `credential_probe`, `voice_preview`) are registered as
//! `module::command`. This follows the newer shape: the Focus page owns its
//! commands, and `lib.rs` only registers them.
//!
//! ## The three rules every command here obeys
//!
//! 1. **`async`, always.** A Tauri command declared without `async` runs on the
//!    main thread. `get_tool_catalog` was synchronous once and froze the whole
//!    window for six seconds. These four touch the filesystem, so they go through
//!    [`crate::run_off_thread`] without exception.
//! 2. **Validate on the way in.** The document arrives from a webview as an
//!    untyped `Value`. [`focus_store::validate`] is the gate, and it runs *before*
//!    the write rather than after.
//! 3. **Read back, then report success.** `save_focus` writes and then loads. A
//!    save that reports success and cannot be read is worse than an error toast,
//!    because the person believes their todos are safe.

use serde_json::Value;
use tauri::Manager;

use crate::focus_store::{self, FocusError};
use crate::run_off_thread;

/// Render a storage error as the sentence a person reads.
///
/// A closure rather than `FocusError::to_string`, because that resolves to
/// `<FocusError as Display>::to_string` and takes `&self`, which does not satisfy
/// `FnOnce(FocusError)`. Three call sites wanted this and none of them should be
/// able to drift onto a different message, so it is named once.
fn say(err: FocusError) -> String {
    err.to_string()
}

/// Where the focus document lives, resolved through the shared override.
fn focus_file_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    let dir = app
        .path()
        .app_local_data_dir()
        .map_err(|err| format!("Could not resolve the app data directory: {err}"))?;
    Ok(focus_store::focus_path(&dir))
}

/// Read the focus document, or a valid default when there is not one yet.
///
/// ## What happens to a file that will not parse
///
/// It is **reported**, not repaired. `load` returns `Err`, the frontend shows the
/// message, and the bytes stay where they are — the `.bak` beside them, if a
/// previous save made one, is still the person's last good copy. Overwriting a
/// corrupt file with a default is how somebody loses a term's worth of study
/// history and is never told, so this command refuses rather than recovers. The
/// recovery is a deliberate human action: restore the backup, or delete the file
/// knowingly.
///
/// A *migratable* older version is the exception, and it is handled inside
/// `focus_store::load` rather than here. That asymmetry is intentional: a version
/// bump is a known, forward-only move, whereas "we could not read this" is not
/// something to resolve by overwriting.
#[tauri::command]
pub async fn load_focus(app: tauri::AppHandle) -> Result<Value, String> {
    run_off_thread("Reading your focus data", move || {
        let path = focus_file_path(&app)?;
        Ok(focus_store::load(&path).map_err(say)?.unwrap_or_else(focus_store::default_document))
    })
    .await
}

/// Persist the focus document, then prove it can be read back.
///
/// The read-back is the same deliberate step `save_config` performs, for the same
/// reason: a write can succeed and still leave a file `load` refuses.
///
/// Two passes happen before the write, both in `focus_store::save`: sessions are
/// pruned to the retention window and intentions clamped to three per day. Both
/// are *retention*, not validation, and they run on the way in so the file on disk
/// is always already trimmed — otherwise the pruning would only take effect on the
/// next launch, and a document could grow for as long as nobody reopened the page.
#[tauri::command]
pub async fn save_focus(app: tauri::AppHandle, document: Value) -> Result<(), String> {
    run_off_thread("Saving your focus data", move || {
        let path = focus_file_path(&app)?;
        focus_store::save(&path, &document).map_err(say)?;
        // Proof of write. Also the only thing that would catch a full disk that
        // filled between the serialise and the rename.
        focus_store::load(&path)
            .map(|_| ())
            .map_err(|err| format!("Your focus data was written but could not be read back: {err}"))
    })
    .await
}

/// Pre-aggregated focus statistics.
///
/// ## Why the aggregation lives here and not only in the frontend
///
/// The frontend draws charts from `document.sessions` directly, and
/// `src/pages/focus/selectors.ts` does that aggregation in TypeScript. This command
/// exists for the *other* consumer: the Python reader in `agent/focus_stats.py`,
/// which must produce the same numbers without a webview.
///
/// One definition per language is already one too many. The rule that keeps them
/// from disagreeing is `src/pages/focus/focusStatsContract.ts`, which names every
/// field and its unit, plus `scripts/verify-focus-contract.mts`, which asserts the
/// fixture outputs agree. A contract nobody checks is a comment.
///
/// Deliberately **not cached**, unlike `get_config`. The document changes on every
/// completed pomodoro, so a cache here would need invalidation on `save_focus` for
/// a benefit no caller needs — this is not on the launch path.
#[tauri::command]
pub async fn focus_stats(
    app: tauri::AppHandle,
    // East-positive minutes east of UTC. The caller must pass
    // `-new Date().getTimezoneOffset()`, because JavaScript returns the negation.
    // `None` falls back to 0 rather than guessing, so a caller that forgets reads
    // UTC and the difference is a visible day shift rather than a silently wrong
    // number.
    utc_offset_minutes: Option<i64>,
    days: Option<i64>,
) -> Result<Value, String> {
    let offset = utc_offset_minutes.unwrap_or(0);
    let days = days.unwrap_or(7).clamp(1, 365);
    run_off_thread("Reading your focus statistics", move || {
        let path = focus_file_path(&app)?;
        let document = focus_store::load(&path)
            .map_err(say)?
            .unwrap_or_else(focus_store::default_document);
        Ok(crate::focus_stats::summarise(&document, offset, days))
    })
    .await
}

/// Authorise one music folder for in-app playback, and revoke the previous one.
///
/// ## This is a security boundary, and the ordering is the whole of it
///
/// Tauri's asset protocol is enabled with an **empty** scope, which denies
/// everything. This command is the only thing that widens it, and it widens it to
/// exactly one directory that the person picked through the native folder picker.
///
/// The sequence is **validate, deny the old root, allow the new root** — not the
/// other way round:
///
/// - *Validate first.* Granting before validating means a bad path briefly widens
///   the scope for something we were going to reject anyway.
/// - *Deny before allowing.* If the two were reversed, a failed switch would leave
///   the previous folder readable, and the failure would be silent. A half-completed
///   permission change is worse than no change.
/// - *Allow last.* Only once the canonical path is known good does anything widen.
///
/// ## Why the canonical path and not the string
///
/// `canonicalize` resolves `..` and symlinks. Allowing `C:\Music` when it is a
/// symlink to `D:\"` and then validating tracks against `C:\Music` would be two
/// different directories, and the scope would be the wider of them. Both the scope
/// and every later track check are given the same canonical value, which is why
/// they cannot disagree.
///
/// ## Why this does not authorise playback on its own
///
/// It grants a *scope*, nothing more. Which file inside it may be played is still
/// decided per track by [`resolve_focus_track`], and the extension check there is
/// what stops a `.exe` or an `.html` sitting in a music folder from being handed
/// to an `<audio>` element. A scope says where; it does not say what.
#[tauri::command]
pub async fn allow_music_root(app: tauri::AppHandle, path: String) -> Result<bool, String> {
    run_off_thread("Authorising your music folder", move || {
        // Canonical first: everything below works on the resolved path, so there is
        // exactly one directory in play and it is the real one.
        let root = crate::media::canonical_library_root(&path)?;

        let scope = app.asset_protocol_scope();
        // Deny whatever was allowed before. `forbid_*` takes precedence over
        // `allow_*` in Tauri's scope, so this is safe to do before the allow and
        // is what makes a *failed* switch leave nothing behind.
        if let Some(previous) = granted_root() {
            let _ = scope.forbid_directory(&previous, true);
        }
        scope
            .allow_directory(&root, true)
            .map_err(|err| format!("That folder could not be authorised for playback: {err}"))?;
        set_granted_root(Some(root));
        Ok(true)
    })
    .await
}

/// Revoke the music folder, if one is authorised.
///
/// Separate from `allow_music_root` rather than folded into it, because "stop
/// letting Lumine read my music folder" is a thing a person should be able to do
/// without picking a replacement first — and a command that only ever adds
/// permission has no way to remove it.
#[tauri::command]
pub async fn revoke_music_root(app: tauri::AppHandle) -> Result<bool, String> {
    run_off_thread("Revoking your music folder", move || {
        let Some(previous) = granted_root() else {
            return Ok(false);
        };
        app.asset_protocol_scope()
            .forbid_directory(&previous, true)
            .map_err(|err| format!("That folder could not be revoked: {err}"))?;
        set_granted_root(None);
        Ok(true)
    })
    .await
}

/// Check one track against the authorised folder, and hand back its path.
///
/// ## Why the frontend cannot do this itself
///
/// `convertFileSrc` is pure string encoding: it turns a path into an
/// `asset://localhost/...` URL and performs no check at all. So if the webview
/// decided for itself whether a track was inside the library, the only remaining
/// enforcement would be Tauri's scope — which grants a whole directory, not a
/// file. The check has to live here to be testable, and it does:
///
/// [`crate::media::validate_against_library`] for the boundary (resolve both
/// sides, then containment) and [`crate::media::is_in_app_audio`] for the
/// extension.
///
/// ## What is returned, and what is not
///
/// The validated absolute path — **not** a URL and not a `data:` URI. The
/// frontend calls `convertFileSrc` on it. Returning the URL from here would mean
/// two places that know Tauri's URL shape, and the frontend's copy would be the
/// one that drifts.
#[tauri::command]
pub async fn resolve_focus_track(path: String, library: String) -> Result<String, String> {
    run_off_thread("Checking that track", move || {
        let target = crate::media::validate_against_library(&path, &library)?;
        if !crate::media::is_in_app_audio(&target) {
            return Err(
                "That kind of file cannot be played inside Lumine. Settings (Music) can still open it in your usual player."
                    .to_string(),
            );
        }
        Ok(target.to_string_lossy().to_string())
    })
    .await
}

// ---------------------------------------------------------------------------
// Which folder is authorised right now
//
// A process-global, like `CATALOG_CACHE` beside it, and for the same reason: the
// asset protocol's scope is process-global, so the process is the only place that
// can honestly answer "what did we grant". Held as a `PathBuf` rather than
// re-derived from the config document because the grant and the setting are not
// the same thing — a person can have a library configured and no folder authorised,
// which is exactly the state after they switch it off.
// ---------------------------------------------------------------------------

static MUSIC_ROOT: std::sync::OnceLock<std::sync::Mutex<Option<std::path::PathBuf>>> =
    std::sync::OnceLock::new();

fn granted_root() -> Option<std::path::PathBuf> {
    MUSIC_ROOT
        .get_or_init(|| std::sync::Mutex::new(None))
        .lock()
        .ok()
        .and_then(|root| root.clone())
}

fn set_granted_root(root: Option<std::path::PathBuf>) {
    if let Ok(mut slot) = MUSIC_ROOT.get_or_init(|| std::sync::Mutex::new(None)).lock() {
        *slot = root;
    }
}

/// The folder currently authorised for in-app playback, if any.
///
/// A read, so the Focus page can say "no folder authorised" rather than
/// discovering it from a failed track.
#[tauri::command]
pub async fn granted_music_root() -> Result<Option<String>, String> {
    Ok(granted_root().map(|root| root.to_string_lossy().to_string()))
}

#[cfg(test)]
mod tests {
    // No `use super::*`: this module's assertions are all about `crate::media`
    // and `crate::focus_store`, and importing the parent would only hide which
    // half of the boundary each test is actually exercising.

    fn temp_root(tag: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("lumine-music-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("temp dir");
        dir
    }

    #[test]
    fn a_library_root_must_be_a_place_and_not_a_web_address() {
        assert!(crate::media::canonical_library_root("  ").is_err(), "blank");
        assert!(
            crate::media::canonical_library_root("https://example.com/music").is_err(),
            "a URL is not a folder"
        );
        assert!(
            crate::media::canonical_library_root("file:///music").is_err(),
            "a file URL is not a folder either"
        );
    }

    #[test]
    fn a_root_that_does_not_exist_is_refused_rather_than_created() {
        let missing = std::env::temp_dir().join("lumine-no-such-music-folder-xyz");
        assert!(crate::media::canonical_library_root(&missing.to_string_lossy()).is_err());
    }

    #[test]
    fn a_root_that_resolves_is_returned_canonically() {
        let root = temp_root("canonical");
        let via_dot = root.join(".").join("..").join(root.file_name().expect("name"));
        assert_eq!(
            crate::media::canonical_library_root(&via_dot.to_string_lossy())
                .expect("a real directory resolves"),
            std::fs::canonicalize(&root).expect("canonical"),
            "the granted root must be the resolved path, not the string typed"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn the_extension_check_is_the_narrow_in_app_list_not_the_video_list() {
        // These are the two cases that make the narrowing necessary rather than
        // cosmetic: the voice path may open them, a webview cannot decode them.
        assert!(!crate::media::is_in_app_audio(std::path::Path::new("clip.mkv")));
        assert!(!crate::media::is_in_app_audio(std::path::Path::new("clip.wmv")));
        for ok in ["song.mp3", "song.MP3", "song.flac", "song.m4a", "song.opus"] {
            assert!(
                crate::media::is_in_app_audio(std::path::Path::new(ok)),
                "{ok} should be playable in-app"
            );
        }
        assert!(!crate::media::is_in_app_audio(std::path::Path::new("run.exe")));
        assert!(!crate::media::is_in_app_audio(std::path::Path::new("page.html")));
        assert!(!crate::media::is_in_app_audio(std::path::Path::new("noextension")));
    }
}