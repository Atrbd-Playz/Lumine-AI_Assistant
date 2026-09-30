"""Speak one short phrase with a candidate TTS configuration.

The settings screen has a talking-speed slider. A slider you cannot hear is a
slider you drag once, guess at, and never revisit, so the voice controls offer a
preview: press a button, hear the settings you are looking at.

This is that button's engine. It is deliberately a *one-shot*, not a session:

* It is given the stage fragment to audition, not the saved profile, because the
  point is to hear a change that has not been committed yet.
* It builds the stage through :func:`agent.pipeline.pipeline_factory._build_tts`, the same
  path the worker uses. Building it any other way would mean previewing a voice
  the real session does not have, which is worse than not previewing at all.
* It never touches a room, an LLM, or a LiveKit session. Nothing is dispatched
  and no state outside this process changes.

Usage::

    echo '{"provider": "cartesia", "model": "sonic-3", "voice": "...",
           "options": {"speed": 1.2}}' | python agent/voice_preview.py

Output is a single JSON line on stdout::

    {"ok": true, "mimeType": "audio/wav", "sampleRate": 24000,
     "channels": 1, "durationSeconds": 3.1, "audioBase64": "..."}

On failure it writes one JSON line with ``ok: false`` and an ``error`` the caller
can show, and exits non-zero. Diagnostics go to stderr so they cannot corrupt the
payload.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import struct
import sys
from pathlib import Path
from typing import Any

# LiveKit plugins must be imported on the process's main thread. Importing them
# inside the coroutine would be after the loop exists, which on some platforms is
# too late to register the plugin's executor.
try:
    from .settings.config_store import ResolvedStage
    from .pipeline.pipeline_factory import _build_tts
except ImportError:  # running as `python agent/voice_preview.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from settings.config_store import ResolvedStage
    from pipeline.pipeline_factory import _build_tts

#: Long enough to judge pace, emotion and volume; short enough that pressing the
#: button twice in a row is not annoying. Anything past this is truncated, so a
#: pathological rate setting cannot make this hang on a long synthesis.
MAX_SECONDS = 6.0

#: Hard ceiling on the returned payload. A 6-second 24 kHz mono 16-bit clip is
#: ~288 kB of PCM, ~384 kB base64. This is under the default Tauri IPC response
#: limit with room to spare, and it fails loudly rather than truncating audio into
#: something that sounds like a dropout.
MAX_BASE64_CHARS = 2_000_000

#: Phrases chosen to exercise the settings rather than just fill time. Speed and
#: volume are obvious on anything, but emotion needs a sentence with a shape to
#: it -- a flat, one-clause line makes every emotion sound identical.
DEFAULT_PHRASE = "Hi, I'm Lumine. This is how I'll sound."


def _load_env() -> None:
    """Load ``agent/.env`` the way the worker does.

    The desktop app injects keyring credentials into the environment, and
    ``load_dotenv`` leaves an already-set variable alone, so an injected key wins
    over the file. Without this a preview could report a missing key for a
    provider the worker itself can use.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.is_file():
        load_dotenv(env_file)


def _wav_header(sample_rate: int, channels: int, data_bytes: int) -> bytes:
    """A canonical 44-byte RIFF/WAVE header.

    The browser decodes the data URI with the WAV parser, not with the sample
    rate embedded in the file, so the header only has to be well-formed.
    """
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + data_bytes),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, 16),
            b"data",
            struct.pack("<I", data_bytes),
        ]
    )


def _frame_payload(frame: Any) -> bytes:
    """The PCM bytes of a synthesized frame, or nothing if there is no frame."""
    data = getattr(frame, "data", None)
    if not data:
        return b""
    return bytes(data)


async def _synthesize(tts: Any, phrase: str) -> tuple[bytes, int, int]:
    """Render ``phrase`` with ``tts`` and return ``(pcm, sample_rate, channels)``.

    Stops reading once the clip is long enough. The provider keeps streaming
    otherwise, and a preview is not a place to spend the user's quota to find out
    how long a sentence can get.
    """
    chunks: list[bytes] = []
    # The first frame with a payload defines the format for the whole clip. A later
    # frame that disagreed would mean the provider changed sample rate or channel
    # count mid-utterance, and the bytes would then be written under a header that
    # does not describe them -- audio at the wrong speed, or pitched into noise. So
    # the first one wins and every frame is taken as-is after that.
    sample_rate: int | None = None
    channels: int | None = None
    collected = 0

    stream = tts.synthesize(phrase)
    async for event in stream:
        frame = event.frame
        if not frame:
            continue
        payload = _frame_payload(frame)
        if not payload:
            continue
        if sample_rate is None:
            sample_rate = getattr(frame, "sample_rate", 0) or None
        if channels is None:
            channels = getattr(frame, "num_channels", 0) or None
        chunks.append(payload)
        collected += len(payload) // (2 * max(channels or 1, 1))
        if sample_rate and collected >= int(MAX_SECONDS * sample_rate):
            break

    return b"".join(chunks), sample_rate or 24_000, channels or 1


def _build_stage(payload: dict[str, Any]) -> ResolvedStage:
    provider = str(payload.get("provider") or "").strip()
    model = str(payload.get("model") or "").strip()
    if not provider or not model:
        raise ValueError("A preview needs both a provider and a model.")
    options = payload.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("Stage options must be an object.")
    voice = payload.get("voice")
    return ResolvedStage(
        provider=provider,
        model=model,
        options=options,
        voice=str(voice).strip() if isinstance(voice, str) and voice.strip() else None,
    )


async def preview(payload: dict[str, Any]) -> dict[str, Any]:
    """Synthesize the requested fragment and return a JSON-ready result."""
    phrase = str(payload.get("phrase") or "").strip() or DEFAULT_PHRASE
    stage = _build_stage(payload)
    tts = await _build_tts(stage)
    try:
        pcm, sample_rate, channels = await _synthesize(tts, phrase)
    finally:
        # The plugin holds a websocket pool. This process is short-lived, but a
        # leaked pool makes the interpreter hang on exit, which reads to the
        # desktop layer as a preview that never returned.
        closer = getattr(tts, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:  # noqa: BLE001 - closing must not mask the result
                pass

    if not pcm:
        raise RuntimeError("The provider returned no audio for that phrase.")

    wav = _wav_header(sample_rate, channels, len(pcm)) + pcm
    encoded = base64.b64encode(wav).decode("ascii")
    if len(encoded) > MAX_BASE64_CHARS:
        raise RuntimeError(
            "That preview came back too long to send. Try a shorter phrase or a smaller speed range."
        )

    return {
        "ok": True,
        "mimeType": "audio/wav",
        "sampleRate": sample_rate,
        "channels": channels,
        "durationSeconds": round(len(pcm) / (2 * channels * sample_rate), 3),
        "audioBase64": encoded,
    }


def _emit(result: dict[str, Any]) -> int:
    """Write the one JSON line the caller reads, and nothing else.

    Plugins log on import and the first request goes through OpenTelemetry; both
    write to stderr, but a library configured for stdout would corrupt the
    payload. Wrapping the write keeps a stray line from becoming a JSON parse
    error in the UI.
    """
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "fragment",
        nargs="?",
        help='JSON stage fragment, e.g. \'{"provider": "cartesia"}\'. Reads stdin if omitted.',
    )
    parser.add_argument(
        "--text",
        help="The phrase to speak. Defaults to a line that exercises emotion as well as pace.",
    )
    args = parser.parse_args(argv)

    raw = args.fragment if args.fragment is not None else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _emit({"ok": False, "error": f"That was not valid JSON: {exc}"})
    if not isinstance(payload, dict):
        return _emit({"ok": False, "error": "Expected a JSON object describing the voice stage."})
    if args.text:
        payload["phrase"] = args.text

    _load_env()
    try:
        result = asyncio.run(preview(payload))
    except Exception as exc:  # noqa: BLE001 - every failure is reported to the UI
        return _emit({"ok": False, "error": str(exc) or exc.__class__.__name__})
    return _emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
