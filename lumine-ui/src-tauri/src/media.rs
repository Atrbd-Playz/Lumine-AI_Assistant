//! Choosing the music folder, and saying what is in it.
//!
//! ## Why the picker is a command and not a control
//!
//! The Music screen needs a real folder path. An `<input webkitdirectory>` hands
//! the webview `File` objects with relative paths, which is no use at all: the
//! worker runs in a different process and needs the path the OS knows the folder
//! by. So this is the one control in the app that has to cross the IPC boundary,
//! and putting it behind a command means the screen never names a plugin.
//!
//! ## Why it also counts the tracks
//!
//! Picking the wrong folder is silent. `D:\Music` when the music is in
//! `D:\Music\Library` produces a settings screen that looks configured and a
//! Lumine who answers "I couldn't find that" to everything. The count is the only
//! place that mistake can be caught, so the picker returns one rather than making
//! the user ask for a song to find out.
//!
//! The count comes from `agent/media_probe.py`, which walks with the *same*
//! scanner the tool plays from. A count computed here in Rust would be a count of
//! a different thing, and the screen would say 40 when Lumine can find 3.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri_plugin_dialog::DialogExt;

/// What the settings screen shows about a chosen folder.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MusicFolder {
    /// The full path, stored in the document and never spoken.
    pub path: String,
    /// The folder's own name — what the screen leads with.
    pub name: String,
    pub track_count: usize,
    pub folder_count: usize,
    /// Subfolders with their track counts. A folder is a playlist, and a playlist
    /// with a number on it is a thing a person can choose.
    pub folders: Vec<MusicPlaylist>,
    /// Extension to count, so the screen can say "mp3 and m4a" rather than "3
    /// formats" — and so an unexpected one is visible.
    pub formats: Vec<(String, usize)>,
    pub allowed: Vec<String>,
    /// False when the folder was read but holds nothing playable. Still a
    /// success: the answer is zero, and that is worth saying plainly rather than
    /// reporting as a failure.
    pub ok: bool,
    /// Set only when `ok` is false, and the only thing to show.
    pub error: Option<String>,
}

/// One subfolder, as a playlist.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MusicPlaylist {
    pub name: String,
    pub tracks: usize,
}

/// What a queue actually contains, and where it went.
///
/// **The Music widget's answer to "play my music".** No track list crosses this
/// boundary any more: the widget asks for a folder to be queued, and gets a count
/// and a sentence. A widget that rendered the library would be a second music
/// browser with none of the care of the first, and the complaint it was answering
/// was that the library screen took too long to appear.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MusicQueue {
    pub ok: bool,
    /// How many tracks are in the queue. The one number worth having.
    pub queued: usize,
    /// Which folder it was built from — the requested subfolder, or the library's
    /// own name.
    pub folder: String,
    /// The first track, as a name. Never the whole queue: a list is not a summary.
    pub first: String,
    /// Where the queue went, in words the user can act on. Skip and stop live in
    /// that window, so the widget has to say so rather than implying it controls
    /// playback.
    pub note: String,
    pub error: Option<String>,
}

/// `agent/media_probe.py`'s envelope, trusted only as far as its shape.
///
/// `rename_all = "camelCase"` is load-bearing for the same reason it is in
/// `voice_preview`: the script emits camelCase for a webview, and without this
/// every field deserialises to its default and the screen shows a library of
/// zero tracks in a folder that has hundreds.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawInspection {
    #[serde(default)]
    ok: bool,
    #[serde(default)]
    path: Option<String>,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    track_count: usize,
    #[serde(default)]
    folder_count: usize,
    /// Either a list of names or a list of `{name, tracks}`; both are read, because
    /// the probe grew the second shape and an older file may still be the first.
    #[serde(default)]
    folders: Vec<RawFolder>,
    #[serde(default)]
    formats: std::collections::BTreeMap<String, usize>,
    #[serde(default)]
    allowed: Vec<String>,
    #[serde(default)]
    error: Option<String>,
}

/// One folder from the probe, in either shape.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum RawFolder {
    Named { name: String, tracks: usize },
    Plain(String),
}

/// `agent/media_queue.py`'s envelope.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawQueue {
    #[serde(default)]
    ok: bool,
    #[serde(default)]
    queued: usize,
    #[serde(default)]
    folder: String,
    #[serde(default)]
    first: String,
    #[serde(default)]
    note: String,
    #[serde(default)]
    error: Option<String>,
}

/// Open the native folder picker and report what was chosen.
///
/// `None` means the person cancelled, which is **not** an error: a cancelled
/// picker is a decision, and reporting it as a failure would put a red toast on
/// screen every time somebody changes their mind.
#[tauri::command]
pub async fn pick_music_folder(app: tauri::AppHandle) -> Result<Option<MusicFolder>, String> {
    // `blocking_pick_folder` must not run on the main thread, and this whole
    // command is already off it. The non-blocking `pick_folder` callback form is
    // the alternative and needs a oneshot channel to come back through, which is
    // more machinery for the same dialog.
    crate::run_off_thread("Choosing a folder", move || {
        let chosen = app
            .dialog()
            .file()
            .set_title("Choose your music folder")
            .blocking_pick_folder();
        let Some(chosen) = chosen else {
            return Ok(None);
        };
        // `into_path` is the desktop spelling and the only one that does not
        // involve a `file://` URI or a content URI. Android and iOS have no
        // folder picker at all, so this is the whole supported surface.
        let path = chosen
            .into_path()
            .map_err(|err| format!("That folder could not be used: {err}"))?;
        inspect(path, true).map(Some)
    })
    .await
}

/// Count what is in a folder the document already names.
///
/// Separate from the picker because the screen re-reads on open and after a save:
/// the folder may have been filled, emptied or moved since it was chosen, and
/// reporting a stale count as current is worse than reporting none.
#[tauri::command]
pub async fn inspect_music_folder(path: String, recursive: Option<bool>) -> Result<MusicFolder, String> {
    let path = path.trim().trim_matches('"').to_string();
    if path.is_empty() {
        return Err("No folder was given.".to_string());
    }
    let recursive = recursive.unwrap_or(true);
    crate::run_off_thread("Looking inside that folder", move || {
        inspect(PathBuf::from(path), recursive)
    })
    .await
}

/// Queue a folder's tracks, in order, into the user's own media player.
///
/// **One hand-over, not one launch per track.** That is the whole point, and it
/// was a reported incident: *"play my vocal songs played all nine at once, no
/// queue or anything, I couldn't able to hear anything."* `os.startfile` takes one
/// file, so the previous shape started nine and the user got nine players on top
/// of each other. `agent/media_queue.py` writes one `.m3u8` from already-validated
/// paths and opens **that**, and from then on the player's own window owns the
/// order — which is also where skip and stop live.
///
/// It is a separate script from the tool on purpose: this button runs in the app
/// process, the `play_media` tool runs in the worker, and neither can call the
/// other. What they share is `tools.media.queue.build`, so the widget and the
/// voice path cannot disagree about what a queue is. A widget that queued
/// differently from the voice path would be the same class of bug as a search box
/// that ranked differently from the matcher.
///
/// No permission gate, on purpose: the gate's question is "may Lumine do this",
/// and the person clicking is answering a different one — "may *I* play this".
///
/// The webview only ever supplies a **subfolder name**. It cannot supply a path:
/// the script resolves the library root from the saved document itself, and
/// `tools.media.scanner` drops anything that fails the containment check before a
/// track can reach the queue.
#[tauri::command]
pub async fn queue_music(folder: Option<String>, recursive: Option<bool>) -> Result<MusicQueue, String> {
    let folder = folder.unwrap_or_default().trim().to_string();
    let recursive = recursive.unwrap_or(true);
    crate::run_off_thread("Queueing your music", move || {
        let mut args = Vec::new();
        if !folder.is_empty() {
            args.push("--folder".to_string());
            args.push(folder.clone());
        }
        if !recursive {
            args.push("--no-recursive".to_string());
        }
        queue(&args)
    })
    .await
}

/// Run `agent/media_queue.py` and judge its answer.
fn queue(args: &[String]) -> Result<MusicQueue, String> {
    let borrowed: Vec<&str> = args.iter().map(String::as_str).collect();
    let stdout = crate::python_env::run_agent_script("media_queue.py", &borrowed)
        .map_err(|err| format!("I could not reach your music folder: {err}"))?;
    judge_queue(stdout.as_bytes())
}

/// `agent/media_queue.py`'s answer, in the shape the widget draws.
fn judge_queue(stdout: &[u8]) -> Result<MusicQueue, String> {
    let raw: RawQueue = serde_json::from_slice(stdout)
        .map_err(|err| format!("Your music folder could not be read: {err}"))?;
    Ok(MusicQueue {
        ok: raw.ok,
        queued: raw.queued,
        folder: raw.folder,
        first: raw.first,
        note: raw.note,
        error: error_of(raw.ok, raw.error),
    })
}

/// Resolve a configured library root to a canonical path.
///
/// Split out of [`validate_against_library`] because the Focus Workspace's
/// in-app player needs the *root* on its own, not a track checked against one: it
/// grants the Tauri asset protocol exactly this directory and nothing else, and
/// granting an uncanonicalised string is how a scope ends up one directory wider
/// than the person chose.
///
/// The refusals are deliberately the same two the track path uses, so switching
/// folder cannot report a problem the track path would have accepted.
pub(crate) fn canonical_library_root(library: &str) -> Result<PathBuf, String> {
    let root = library.trim().trim_matches('"');
    if root.is_empty() {
        return Err("Choose a music folder in Settings first.".to_string());
    }
    let lowered = root.to_ascii_lowercase();
    for scheme in ["http://", "https://", "ftp://", "file://"] {
        if lowered.starts_with(scheme) {
            return Err("A music folder is a place on this computer, not a web address.".to_string());
        }
    }
    std::fs::canonicalize(root).map_err(|_| "That music folder could not be read.".to_string())
}

/// Audio extensions the in-app player will hand to an `<audio>` element.
///
/// **A narrower list than `agent/tools/media/library.py::ALLOWED_SUFFIXES`, and
/// the narrowing is the point.** That constant also admits video, because
/// `play_media` may open a video in the system player where a container can be
/// anything. A webview's `<audio>` cannot decode `.mkv` or `.wmv`, so offering
/// them produces a player that silently does nothing — and a player that fails
/// quietly is worse than one that refuses. `.mp4`/`.m4a` are kept because the
/// webview decodes AAC/MP4 audio without trouble.
///
/// Duplicated rather than shelled out for the same reason
/// [`validate_against_library`] is: a 400 ms interpreter start between a click
/// and a sound. The Python list stays the authority for the *voice* path; this one
/// is the authority for *in-app* playback, and the two answers are allowed to
/// differ because they are answering different questions.
pub(crate) const IN_APP_AUDIO_SUFFIXES: [&str; 8] = [
    "mp3", "m4a", "aac", "flac", "wav", "ogg", "opus", "weba",
];

/// Whether a path is something the in-app player can actually decode.
///
/// Case-insensitive on purpose: `SONG.MP3` is the same file as `song.mp3` on
/// Windows and macOS, and refusing it because of its capital letters would be a
/// bug that only shows up on somebody's own library.
pub(crate) fn is_in_app_audio(path: &Path) -> bool {
    path.extension()
        .and_then(|ext| ext.to_str())
        .map(|ext| {
            IN_APP_AUDIO_SUFFIXES
                .iter()
                .any(|allowed| allowed.eq_ignore_ascii_case(ext))
        })
        .unwrap_or(false)
}

/// The library boundary, in Rust.
///
/// **Deliberately duplicated from `agent/tools/media/library.py`.** The rule is
/// small — resolve both sides, then check containment — and the alternative was
/// shelling out to Python for one `starts_with`, which would put a 400 ms
/// interpreter start between a click and a sound.
///
/// `canonicalize` resolves symlinks and `..` on every platform, which is why it is
/// used rather than string surgery. The duplication is the risk, and it is bounded
/// by the tests at the bottom of this file, which pin the cases that have actually
/// caused bugs.
///
/// What this does **not** do is validate the extension — that half is Python's,
/// and the opener only ever sees a path that has been through
/// `play_media` *or* through this, so the allowlist lives in one place per process.
pub(crate) fn validate_against_library(path: &str, library: &str) -> Result<PathBuf, String> {
    let raw = path.trim().trim_matches('"');
    if raw.is_empty() {
        return Err("That track could not be found.".to_string());
    }
    let lowered = raw.to_ascii_lowercase();
    for scheme in ["http://", "https://", "ftp://", "file://"] {
        if lowered.starts_with(scheme) {
            return Err("That is a web address, not a file on this computer.".to_string());
        }
    }
    let resolved_root = canonical_library_root(library)?;
    // Both sides resolved. Comparing an unresolved root against a resolved child is
    // how every track in a good library came to be reported as outside it.
    let target = std::fs::canonicalize(raw).map_err(|_| "That track could not be found.".to_string())?;
    if !target.starts_with(&resolved_root) {
        return Err("I can only play things inside your music folder.".to_string());
    }
    Ok(target)
}

/// Run a helper and turn its envelope into the screen's shape.
fn inspect(path: PathBuf, recursive: bool) -> Result<MusicFolder, String> {
    let text = path.to_string_lossy().to_string();
    let mut args: Vec<String> = vec![text.clone()];
    if !recursive {
        args.push("--no-recursive".to_string());
    }
    let borrowed: Vec<&str> = args.iter().map(String::as_str).collect();
    let stdout = crate::run_helper("media_probe.py", &borrowed, "look inside that folder")?;
    judge(&stdout, &path)
}

/// Turn the probe's JSON into the screen's shape, or into something worth showing.
///
/// Split out of the commands above because this half is pure: it reads JSON and
/// decides what a folder looks like to a person. Both of those are checkable here
/// without a dialog, which is the only way to test the part that actually decides
/// anything.
fn judge(stdout: &[u8], path: &std::path::Path) -> Result<MusicFolder, String> {
    let raw: RawInspection =
        serde_json::from_slice(stdout).map_err(|err| format!("That folder could not be read: {err}"))?;

    let folders = raw
        .folders
        .into_iter()
        .map(|entry| match entry {
            RawFolder::Named { name, tracks } => MusicPlaylist { name, tracks },
            RawFolder::Plain(name) => MusicPlaylist { name, tracks: 0 },
        })
        .collect();

    Ok(MusicFolder {
        path: raw
            .path
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| path.to_string_lossy().to_string()),
        name: raw.name.unwrap_or_else(|| {
            path.file_name()
                .map(|name| name.to_string_lossy().to_string())
                .unwrap_or_else(|| path.to_string_lossy().to_string())
        }),
        track_count: raw.track_count,
        folder_count: raw.folder_count,
        folders,
        // BTreeMap in the script, so this is sorted by extension rather than by
        // whatever order the walk happened to produce.
        formats: raw.formats.into_iter().collect(),
        allowed: raw.allowed,
        ok: raw.ok,
        error: error_of(raw.ok, raw.error),
    })
}

/// One place that decides what an unreadable folder says.
fn error_of(ok: bool, error: Option<String>) -> Option<String> {
    if ok {
        return None;
    }
    Some(
        error
            .filter(|message| !message.trim().is_empty())
            .unwrap_or_else(|| "That folder could not be read.".to_string()),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn envelope(json: &str) -> RawInspection {
        serde_json::from_str(json).expect("envelope")
    }

    #[test]
    fn a_good_report_becomes_the_counts_the_screen_shows() {
        let report = judge(
            br#"{"ok":true,"path":"C:/Music","name":"Music","trackCount":42,"folderCount":3,
                 "folders":["vocal","live","Rock"],
                 "formats":{".flac":10,".mp3":32},"allowed":[".mp3",".flac"]}"#,
            std::path::Path::new("C:/Music"),
        )
        .expect("a report");
        assert!(report.ok);
        assert_eq!(report.track_count, 42);
        assert_eq!(report.folder_count, 3);
        let names: Vec<&str> = report.folders.iter().map(|f| f.name.as_str()).collect();
        assert_eq!(names, vec!["vocal", "live", "Rock"]);
        assert!(report.error.is_none());
    }

    #[test]
    fn the_envelope_is_read_in_the_shape_the_script_writes() {
        // Asserted on the struct rather than on `judge`, so the failure names the
        // cause. Without the camelCase rename every count is 0 and the screen
        // reports an empty library for a folder full of music.
        let raw = envelope(
            r#"{"ok":true,"path":"C:/Music","name":"Music","trackCount":42,
                "folderCount":3,"folders":["vocal"],"formats":{".mp3":42},
                "allowed":[".mp3"]}"#,
        );
        assert_eq!(raw.track_count, 42);
        assert_eq!(raw.folder_count, 3);
        assert_eq!(raw.name.as_deref(), Some("Music"));
    }

    #[test]
    fn the_probe_carries_no_track_list_any_more() {
        // The library is a *permission* now, not a browser. Nothing on the music
        // path fetches filenames, and the reported symptom was loading: opening
        // the settings screen started a Python interpreter, and a track list is
        // what made a second one worth starting.
        //
        // Unknown fields are ignored rather than rejected, so a probe that still
        // emits `tracks` must not break the screen.
        let report = judge(
            br#"{"ok":true,"path":"C:/Music","name":"Music","trackCount":2,"folderCount":0,
                 "folders":[],"formats":{".mp3":2},"allowed":[".mp3"],
                 "tracks":[{"path":"C:/Music/a.mp3","name":"A","tags":["vocal"]}]}"#,
            std::path::Path::new("C:/Music"),
        )
        .expect("a report");
        assert_eq!(report.track_count, 2);
        assert_eq!(report.name, "Music");
    }

    #[test]
    fn folders_keep_their_counts_because_a_playlist_needs_one() {
        let report = judge(
            br#"{"ok":true,"path":"C:/Music","name":"Music","trackCount":9,"folderCount":2,
                 "folders":[{"name":"vocal","tracks":7},{"name":"live","tracks":2}],
                 "formats":{".mp3":9},"allowed":[".mp3"]}"#,
            std::path::Path::new("C:/Music"),
        )
        .expect("a report");
        assert_eq!(report.folders.len(), 2);
        assert_eq!(report.folders[0].name, "vocal");
        assert_eq!(report.folders[0].tracks, 7);
    }

    #[test]
    fn an_older_bare_folder_list_is_still_read() {
        // The probe grew the `{name, tracks}` shape; a document written against the
        // older one must not read as zero playlists.
        let report = judge(
            br#"{"ok":true,"path":"C:/Music","name":"Music","trackCount":3,"folderCount":1,
                 "folders":["vocal"],"formats":{},"allowed":[".mp3"]}"#,
            std::path::Path::new("C:/Music"),
        )
        .expect("a report");
        assert_eq!(report.folders[0].name, "vocal");
    }

    #[test]
    fn an_empty_folder_is_a_success_reporting_zero() {
        // The distinction that matters: read the folder, found nothing playable.
        // Reporting this as a failure is how "your music folder is empty" arrives
        // as "could not read that folder".
        let report = judge(
            br#"{"ok":true,"path":"C:/Empty","name":"Empty","trackCount":0,
                 "folderCount":0,"folders":[],"formats":{},"allowed":[".mp3"]}"#,
            std::path::Path::new("C:/Empty"),
        )
        .expect("a report");
        assert!(report.ok);
        assert_eq!(report.track_count, 0);
        assert!(report.error.is_none());
    }

    #[test]
    fn an_unreadable_folder_keeps_the_scripts_own_words() {
        let report = judge(
            br#"{"ok":false,"path":"C:/Locked","error":"That folder could not be read: denied"}"#,
            std::path::Path::new("C:/Locked"),
        )
        .expect("a report");
        assert!(!report.ok);
        assert_eq!(report.error.as_deref(), Some("That folder could not be read: denied"));
    }

    #[test]
    fn a_failure_with_no_message_still_produces_a_sentence() {
        let report = judge(br#"{"ok":false}"#, std::path::Path::new("C:/X")).expect("a report");
        let error = report.error.expect("a sentence");
        assert!(error.contains("could not be read"), "{error}");
        assert!(error.ends_with('.'), "{error}");
    }

    #[test]
    fn output_that_is_not_the_envelope_is_named_as_such() {
        // A traceback on stdout, or a stray print. The screen must say the folder
        // could not be read rather than showing an empty library, because an
        // empty library and a broken probe look identical otherwise.
        let err = judge(b"Traceback (most recent call last):", std::path::Path::new("C:/X"))
            .expect_err("a failure");
        assert!(err.contains("could not be read"), "{err}");
    }

    #[test]
    fn the_folder_name_falls_back_to_the_tail_of_the_path() {
        // A path with no final component — the root, or something the OS reported
        // unusually. An empty name renders as a blank heading.
        let report = judge(
            br#"{"ok":true,"path":"/","trackCount":0}"#,
            std::path::Path::new("/"),
        )
        .expect("a report");
        assert_eq!(report.name, "/");
    }

    #[test]
    fn a_chosen_path_beats_the_probe_when_the_probe_omits_it() {
        let report = judge(br#"{"ok":true,"path":"","trackCount":1}"#, std::path::Path::new("C:/Music"))
            .expect("a report");
        assert_eq!(report.path, "C:/Music");
    }

    /// The exact bytes `agent/media_queue.py` writes on a good queue.
    const GOOD_QUEUE: &str = r#"{"ok":true,"queued":9,"folder":"Lumine Media Folder",
        "first":"Akhon Ami এখন আমি Vocal song.mp3",
        "note":"Queued in order in your media player. Skip and stop are in that window."}"#;

    #[test]
    fn a_queue_reports_a_count_and_where_it_went() {
        // The widget's whole answer. A count and a sentence, and **no list** — the
        // queue lives in the player's window, and a widget that drew the filenames
        // would be a second library browser that knows nothing about playback.
        let queued = judge_queue(GOOD_QUEUE.as_bytes()).expect("a queue");
        assert!(queued.ok);
        assert_eq!(queued.queued, 9);
        assert_eq!(queued.folder, "Lumine Media Folder");
        assert!(queued.note.contains("media player"), "{}", queued.note);
    }

    #[test]
    fn a_queue_names_the_first_track_because_a_list_is_not_a_summary() {
        // Nine songs went somewhere. Which one is the useful half of that, and the
        // only part a widget can honestly show.
        let queued = judge_queue(GOOD_QUEUE.as_bytes()).expect("a queue");
        assert!(queued.first.contains("Akhon Ami"));
    }

    #[test]
    fn an_empty_library_is_a_failure_the_widget_can_explain() {
        // Not a silent zero: "queued 0" reads as something went wrong, and the
        // script's own sentence is the useful one.
        let queued = judge_queue(
            br#"{"ok":false,"queued":0,"folder":"vocal","error":"There's nothing in 'vocal'."}"#,
        )
        .expect("a queue");
        assert!(!queued.ok);
        assert_eq!(queued.queued, 0);
        assert_eq!(queued.error.as_deref(), Some("There's nothing in 'vocal'."));
    }

    #[test]
    fn a_broken_queue_names_itself_rather_than_reporting_zero_tracks() {
        // The failure that matters: `queued: 0` reads as "you have no music".
        let err = judge_queue(b"Traceback (most recent call last):").expect_err("a failure");
        assert!(err.contains("could not be read"), "{err}");
    }

    #[test]
    fn formats_come_back_sorted_by_extension() {
        // The script hands over a BTreeMap, so this is guaranteed there rather
        // than by a sort here — the assertion pins the guarantee rather than
        // re-implementing it.
        let report = judge(
            br#"{"ok":true,"path":"C:/M","name":"M","trackCount":3,
                 "formats":{".wav":1,".flac":1,".mp3":1},"allowed":[".mp3"]}"#,
            std::path::Path::new("C:/M"),
        )
        .expect("a report");
        let names: Vec<&str> = report.formats.iter().map(|(name, _)| name.as_str()).collect();
        assert_eq!(names, vec![".flac", ".mp3", ".wav"]);
    }
}