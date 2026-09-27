//! Renders a candidate voice configuration so the settings screen can play it.
//!
//! ## Why this needs its own command
//!
//! A talking-speed slider is unusable if you cannot hear what it does. Every other
//! model setting is judged by looking at the field; this one is judged by
//! listening. So the voice controls offer a preview, and this is the bridge.
//!
//! ## What it does and does not do
//!
//! It runs `agent/voice_preview.py` as a one-shot child, which synthesizes a short
//! phrase with the stage fragment the screen currently has -- *including* changes
//! that have not been saved. It never dispatches an agent, never opens a room, and
//! never touches the running worker, so a preview cannot disturb a live session.
//!
//! ## The credential's path
//!
//! Same rule as the worker and the credential probe: the secret is read from the
//! keyring, put in the child's environment, and goes nowhere else. It is not an
//! argument (arguments are visible in the process list), not logged, not returned,
//! and not written to disk. The stage fragment on stdin carries only ids, numbers
//! and enums.
//!
//! ## Why there is a timeout
//!
//! This is the only command in the app that blocks on a third-party network call
//! with no way to report progress. A provider that stalls would leave a button
//! spinning forever and a child process alive holding a socket. So the child is
//! killed at the deadline and the UI is told the preview timed out, which is both
//! true and actionable in a way "still loading" never is.

use std::io::Write;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::credential_injection::key_env_by_provider;
use crate::credentials::{CredentialStore, OsCredentialStore};
use crate::python_env;

/// How long to wait for a phrase before giving up.
///
/// Generous enough for a cold websocket plus a synthesis round trip, short enough
/// that a wedged provider reads as a failed button press rather than a hang. The
/// button also refuses to re-fire while a preview is in flight, so this ceiling is
/// per attempt and not cumulative.
const PREVIEW_TIMEOUT: Duration = Duration::from_secs(20);

/// Longest phrase accepted. The preview exists to judge pace and emotion, and a
/// long passage stops being a preview; the cap also bounds the bill for a request
/// someone triggers by accident.
const MAX_TEXT_CHARS: usize = 240;

/// The audio the settings screen plays.
///
/// `audio_base64` is a WAV, not raw PCM, because the webview gets a data URI it can
/// hand straight to an `<audio>` element. Encoding it here means the browser never
/// has to know the format the provider happened to return.
#[derive(Debug, Clone, Serialize)]
pub struct VoicePreview {
    pub mime_type: String,
    pub sample_rate: u32,
    pub channels: u32,
    pub duration_seconds: f64,
    /// A `data:` URI, ready for `HTMLAudioElement.src`.
    pub data_uri: String,
}

/// The script's own envelope, trusted only as far as its shape.
///
/// `rename_all` is load-bearing. `agent/voice_preview.py` emits camelCase
/// (`audioBase64`, `mimeType`, `sampleRate`, `durationSeconds`) because it is
/// consumed as JSON by a webview, and without this every one of those fields
/// deserialises as `None`. The failure was silent: the preview still returned a
/// `VoicePreview`, with an empty `data_uri`, and the settings screen showed a
/// working button that played nothing. The two envelope tests below are what
/// caught it -- a struct that is only ever exercised through a real child process
/// agrees with itself no matter what the child sends.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawPreview {
    #[serde(default)]
    ok: bool,
    #[serde(default)]
    mime_type: Option<String>,
    #[serde(default)]
    sample_rate: Option<u32>,
    #[serde(default)]
    channels: Option<u32>,
    #[serde(default)]
    duration_seconds: Option<f64>,
    #[serde(default)]
    audio_base64: Option<String>,
    /// Present when `ok` is false, and the only thing shown to the user.
    #[serde(default)]
    error: Option<String>,
}

fn str_field(fragment: &Value, name: &str) -> Option<String> {
    fragment
        .get(name)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

#[tauri::command]
pub async fn preview_voice(fragment: Value, text: Option<String>) -> Result<VoicePreview, String> {
    let provider = str_field(&fragment, "provider")
        .ok_or_else(|| "A voice preview needs a provider.".to_string())?;
    if str_field(&fragment, "model").is_none() {
        return Err("A voice preview needs a model.".to_string());
    }

    // The child is a blocking process with pipes and a poll loop, so it runs off
    // the async runtime's threads. Those are the ones running the IPC loop; a
    // 20-second wait on one of them would stall every other command too.
    tauri::async_runtime::spawn_blocking(move || render(&provider, fragment, text))
        .await
        .map_err(|err| format!("The preview could not be started: {err}"))?
}

/// Read the provider's keys from the keyring and name the variables they go in.
///
/// The variable names are the catalog's, not a list kept here: a provider added
/// on the Python side needs no change in this file. Each value goes into the
/// variable it was typed for -- a TTS provider with more than one credential
/// cannot be built from one value copied into both, which is what the first
/// version of this did. When the keyring has nothing, nothing is injected and
/// `agent/.env` remains the source, which is a supported configuration rather
/// than a failure.
fn credential_env(provider: &str) -> Vec<(String, String)> {
    let Ok(catalog) = python_env::run_agent_script("provider_catalog.py", &[]) else {
        return Vec::new();
    };
    let Ok(catalog) = serde_json::from_str::<Value>(&catalog) else {
        return Vec::new();
    };
    let Some(names) = key_env_by_provider(&catalog).get(provider).cloned() else {
        return Vec::new();
    };
    OsCredentialStore
        .secrets_for_worker(provider, &names)
        .into_iter()
        .map(|(name, secret)| (name, secret.trim().to_string()))
        .filter(|(_, secret)| !secret.is_empty())
        .collect()
}

/// Run the script, write the fragment to it, and wait -- but not forever.
///
/// The poll loop is the reason this does not deadlock. The reply is a few hundred
/// kilobytes of base64, far more than an OS pipe buffer holds, so the child blocks
/// until someone reads it. Draining has to happen on its own threads while the
/// deadline is watched here; a child blocked on a full pipe is a child that never
/// exits, and killing it then loses the error message.
fn render(provider: &str, fragment: Value, text: Option<String>) -> Result<VoicePreview, String> {
    let mut payload = fragment;
    if let Some(text) = text {
        let text = text.trim();
        if !text.is_empty() {
            let text: String = text.chars().take(MAX_TEXT_CHARS).collect();
            payload["phrase"] = Value::String(text);
        }
    }
    let request = serde_json::to_string(&payload).map_err(|err| format!("Could not read the voice settings: {err}"))?;

    let mut command = Command::new(python_env::python_bin()?);
    command
        .arg(python_env::agent_dir()?.join("voice_preview.py"))
        .current_dir(python_env::project_root()?)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // The keyring credential, injected into the child's environment rather than
    // passed as an argument, before the process exists.
    for (name, value) in credential_env(provider) {
        command.env(name, value);
    }
    let mut child = command
        .spawn()
        .map_err(|err| format!("Could not start the voice preview: {err}"))?;

    {
        let mut stdin = child
            .stdin
            .take()
            .ok_or("The voice preview could not be given its settings.")?;
        stdin
            .write_all(request.as_bytes())
            .map_err(|err| format!("Could not send the voice settings: {err}"))?;
    }

    let stdout = drain(child.stdout.take());
    let stderr = drain(child.stderr.take());

    let deadline = Instant::now() + PREVIEW_TIMEOUT;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                break None;
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(25)),
            // A wait error means the child is no longer ours to poll, so treat it
            // as finished and let the collected output decide what happened.
            Err(_) => break None,
        }
    };

    let out = stdout.recv().unwrap_or_default();
    let err = stderr.recv().unwrap_or_default();

    if status.is_none() && out.is_empty() && err.is_empty() {
        // Nothing at all came back, so the child was killed at the deadline rather
        // than having failed in a way it could describe. A provider that stalls is
        // a different problem from one that rejects, and only this one gets the
        // "may be slow or unreachable" wording.
        return Err(format!(
            "The preview did not finish within {} seconds. The provider may be slow or unreachable.",
            PREVIEW_TIMEOUT.as_secs()
        ));
    }

    let raw: RawPreview = match serde_json::from_slice(&out) {
        Ok(raw) => raw,
        Err(_) => {
            // Not our JSON, so the script failed before it could report properly.
            // Its last stderr line is the traceback's conclusion, which is more
            // use than "invalid output".
            return Err(last_line(&err).unwrap_or_else(|| "The voice preview did not return audio.".to_string()));
        }
    };

    judge(raw)
}

/// Turn the script's envelope into audio, or into something worth showing.
///
/// Split out of `render` because this half is where the policy lives -- which
/// failures get their own words, which ones get a fallback, and what the webview
/// is handed -- and none of it needs a child process to decide. `render` spawns
/// Python; this only reads JSON, so it is the part that can be checked directly.
fn judge(raw: RawPreview) -> Result<VoicePreview, String> {
    if !raw.ok {
        return Err(raw
            .error
            .filter(|message| !message.trim().is_empty())
            .unwrap_or_else(|| "The voice preview could not be created.".to_string()));
    }

    let audio = raw
        .audio_base64
        .filter(|encoded| !encoded.is_empty())
        .ok_or("The voice preview returned no audio.")?;
    let mime_type = raw.mime_type.unwrap_or_else(|| "audio/wav".to_string());

    Ok(VoicePreview {
        data_uri: format!("data:{mime_type};base64,{audio}"),
        mime_type,
        sample_rate: raw.sample_rate.unwrap_or_default(),
        channels: raw.channels.unwrap_or(1),
        duration_seconds: raw.duration_seconds.unwrap_or_default(),
    })
}

/// Read a pipe to end of file on its own thread.
fn drain<R: std::io::Read + Send + 'static>(pipe: Option<R>) -> mpsc::Receiver<Vec<u8>> {
    let (tx, rx) = mpsc::channel();
    let Some(mut pipe) = pipe else {
        let _ = tx.send(Vec::new());
        return rx;
    };
    std::thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = pipe.read_to_end(&mut buffer);
        let _ = tx.send(buffer);
    });
    rx
}

/// The last non-empty stderr line, which is where a traceback states its cause.
fn last_line(stderr: &[u8]) -> Option<String> {
    String::from_utf8_lossy(stderr)
        .lines()
        .rev()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_preview_refuses_a_fragment_with_no_provider() {
        // Checked before any process is started, so a malformed request costs
        // nothing and names the actual problem.
        assert!(str_field(&serde_json::json!({ "model": "sonic-3" }), "provider").is_none());
    }

    #[test]
    fn a_blank_provider_counts_as_absent() {
        // The screen submits `"   "` when a select is cleared; that is not a
        // provider id and must not reach the script.
        assert!(str_field(&serde_json::json!({ "provider": "  " }), "provider").is_none());
    }

    #[test]
    fn a_provider_name_is_trimmed() {
        assert_eq!(
            str_field(&serde_json::json!({ "provider": " cartesia " }), "provider").as_deref(),
            Some("cartesia")
        );
    }

    #[test]
    fn a_typed_field_is_not_read_as_a_string() {
        // `Value::as_str` is None for a number, which is what stops
        // `{"provider": 7}` from becoming the provider "7".
        assert!(str_field(&serde_json::json!({ "provider": 7 }), "provider").is_none());
    }

    #[test]
    fn the_last_stderr_line_is_the_cause_not_the_traceback() {
        // A traceback's final line is the exception; the ones above it are frames.
        let stderr = b"Traceback (most recent call last):\n  File \"x\", line 1\nRuntimeError: no key\n";
        assert_eq!(last_line(stderr).as_deref(), Some("RuntimeError: no key"));
    }

    #[test]
    fn empty_stderr_has_no_last_line() {
        assert_eq!(last_line(b"\n  \n"), None);
    }

    /// The exact bytes `agent/voice_preview.py` writes on a good run.
    const GOOD_REPLY: &str = r#"{"ok":true,"mimeType":"audio/wav","sampleRate":24000,"channels":1,"durationSeconds":2.5,"audioBase64":"AAAA"}"#;

    fn envelope(json: &str) -> RawPreview {
        serde_json::from_str(json).expect("envelope")
    }

    #[test]
    fn a_successful_reply_becomes_a_playable_data_uri() {
        // The shape the webview depends on. If the script's envelope drifts, this
        // fails here rather than as a silent `<audio>` that never plays.
        let preview = judge(envelope(GOOD_REPLY)).expect("a preview");
        assert_eq!(preview.data_uri, "data:audio/wav;base64,AAAA");
        assert_eq!(preview.mime_type, "audio/wav");
        assert_eq!(preview.sample_rate, 24000);
        assert_eq!(preview.channels, 1);
        assert!((preview.duration_seconds - 2.5).abs() < f64::EPSILON);
    }

    #[test]
    fn the_envelope_is_read_in_the_shape_the_script_writes() {
        // `RawPreview` renames to camelCase because that is what the script emits.
        // Without the rename every field deserialises as `None`, `judge` rejects
        // the empty audio, and the settings screen shows a working "Hear it"
        // button that plays nothing. Asserted on the struct, not on `judge`, so
        // the failure names the cause instead of the symptom.
        let raw = envelope(GOOD_REPLY);
        assert_eq!(raw.audio_base64.as_deref(), Some("AAAA"));
        assert_eq!(raw.mime_type.as_deref(), Some("audio/wav"));
        assert_eq!(raw.sample_rate, Some(24000));
        assert_eq!(raw.duration_seconds, Some(2.5));
    }

    #[test]
    fn a_failed_reply_keeps_the_script_own_words() {
        // The script is the only thing that knows whether a rejected value was a
        // missing key, a bad emotion name, or a rate out of range. Replacing its
        // message with a generic one would throw away the part the user can act on.
        let err = judge(envelope(r#"{"ok":false,"error":"CARTESIA_API_KEY is not set"}"#))
            .expect_err("a failure");
        assert_eq!(err, "CARTESIA_API_KEY is not set");
    }

    #[test]
    fn a_failure_with_no_message_still_produces_a_sentence() {
        // Every error the user sees has to be a sentence they can read. A failure
        // with nothing in it is the case that used to reach the screen as an empty
        // string, which reads as a button that did nothing.
        let err = judge(envelope(r#"{"ok":false}"#)).expect_err("a failure");
        assert!(err.contains("voice preview"), "{err}");
        assert!(err.ends_with('.'), "{err}");
    }

    #[test]
    fn a_failure_with_only_whitespace_still_produces_a_sentence() {
        // `"error": "   "` is the same situation as no error at all, and would
        // otherwise pass the "is it there" check while showing a blank.
        let err = judge(envelope(r#"{"ok":false,"error":"   "}"#)).expect_err("a failure");
        assert!(err.contains("voice preview"), "{err}");
    }

    #[test]
    fn a_success_with_no_audio_is_still_a_failure() {
        // `ok: true` is the script's word, not a promise that audio came back. If
        // it is ever wrong, the honest outcome is an error the user can see rather
        // than a data URI that plays silence.
        let err = judge(envelope(r#"{"ok":true,"audioBase64":""}"#)).expect_err("a failure");
        assert!(err.contains("no audio"), "{err}");
    }

    #[test]
    fn a_wav_is_assumed_when_the_script_does_not_say() {
        // The one preview this renders is a WAV, because that is what the script
        // writes. A default here means a missing field cannot produce a data URI
        // the browser would reject on the MIME type.
        let preview = judge(envelope(r#"{"ok":true,"audioBase64":"AAAA"}"#)).expect("a preview");
        assert_eq!(preview.mime_type, "audio/wav");
    }

    #[test]
    fn the_text_cap_leaves_whole_characters() {
        // Truncating by bytes would split a multi-byte character and produce a
        // request the provider rejects for a reason nobody could see.
        let long = "é".repeat(MAX_TEXT_CHARS + 50);
        let capped: String = long.chars().take(MAX_TEXT_CHARS).collect();
        assert_eq!(capped.chars().count(), MAX_TEXT_CHARS);
        assert!(capped.is_char_boundary(capped.len()));
    }

    #[test]
    fn the_timeout_is_long_enough_to_be_useful_and_short_enough_to_not_hang() {
        assert!(PREVIEW_TIMEOUT >= Duration::from_secs(10));
        assert!(PREVIEW_TIMEOUT <= Duration::from_secs(30));
    }
}
