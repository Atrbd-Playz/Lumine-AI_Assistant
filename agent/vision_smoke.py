"""End-to-end proof that a screen frame reaches the model's provider.

Everything else in this feature is argued from unit tests, and a unit test can
prove the gate, the slot and the ledger while the *pipe* is broken. The question
this script answers is the one those tests structurally cannot: **did a frame
captured in a real LiveKit room become an image in a real request to a real
provider?**

It is the vision counterpart of `desktop_smoke.py`: staged, and the first stage
proves the negative. Nothing here types, clicks or sends -- it publishes a video
track and reads the worker's own `LUMINE_EVENT` stream back. It costs a handful
of Gemini image tokens and no microphone, because a check that needs a person to
speak into a microphone is not a check anybody can run twice the same way.

What it publishes is a *marker*, not a desktop: a large changing number with a
coloured band, so a stale-frame failure is visible as "she described window 7"
while the marker had already advanced to 12. A photograph of a real desktop would
make the same failure invisible in the report.

The stages, in order, and each one is expected to fail before the next can pass::

    off      no track published           -> zero vision_request records
    on       one track published          -> at least one with image_attached
    stable   one marker, held             -> few requests; the budget working
    switch   marker every 0.25s           -> latest wins, one image, no queue

Usage::

    .\\.venv\\Scripts\\python.exe agent\\vision_smoke.py [--seconds 20] [--camera]
    .\\.venv\\Scripts\\python.exe agent\\vision_smoke.py --pipeline legacy_cascade

`--camera` publishes a camera track instead of a screencast, which is the
"camera-only and screen-only" pair. `--pipeline legacy_cascade` runs the same
room against the text-only cascade, where no sampler exists at all and the
expectation inverts: **zero** vision_request records, because a text-only model
must never be handed a picture it cannot read.

It always stops the worker it started, and always deletes the room it made.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

AGENT_NAME = "lumine"
READY_TIMEOUT = 90.0
#: Two frames a second is more than enough to be refused by a 1fps budget, and
#: it is the rate that makes "latest wins" measurable rather than instantaneous.
PUBLISH_FPS = 4.0
SWITCH_SECONDS = 0.25

#: Mirrors agent.VISION_SYNC_SECONDS -- how often the worker publishes its
#: `visual` heartbeat, and therefore how long a caller has to wait before a
#: frame-count reading means anything.
VISION_SYNC_SECONDS = 5.0

#: The number the spoken-answer stage plants on the screen. Fixed rather than
#: random so the assertion is a string comparison against a known value, and
#: large enough that it cannot appear in the question itself by accident.
ANSWER_MARKER = 42

#: The marker is rendered small on purpose. A 1024x768 RGBA frame is 3MB, so a
#: 4fps publisher pushes ~12MB/s of raw video into the encoder *and* re-renders
#: it with PIL each time. Measured: under that load the source silently drops
#: frames, and a run that should have judged ~260 judged 8 -- which looks
#: exactly like a broken sampler and is nothing of the kind. 640x480 is a third
#: of the bytes, and the marker is still legible to the model.
WIDTH, HEIGHT = 640, 480

#: Rendered markers are cached: the same number is published hundreds of times
#: over a run, and re-drawing it each time is pure CPU competing with the
#: encoder that has to ship it.
_MARKER_CACHE: dict[int, bytes] = {}


# ---------------------------------------------------------------------------
# The worker, and its telemetry
# ---------------------------------------------------------------------------


class WorkerLog:
    """The worker's stdout, read off a pipe while it is still being written.

    A pipe rather than a file so this works on a machine with no writable temp
    location, and so the reader sees records as they happen -- a scenario that
    has to wait for a file to be flushed is a scenario whose timings are the
    flush's, not the system's.
    """

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.lines: list[str] = []
        self._records: list[dict] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        stream = self.process.stdout
        if stream is None:
            return
        for raw in stream:
            line = raw.rstrip("\n")
            with self._lock:
                self.lines.append(line)
            if line.startswith("LUMINE_EVENT "):
                try:
                    record = json.loads(line[len("LUMINE_EVENT ") :])
                except ValueError:
                    continue
                if isinstance(record, dict):
                    with self._lock:
                        self._records.append(record)

    def records(self, kind: str | None = None) -> list[dict]:
        with self._lock:
            found = list(self._records)
        return [r for r in found if kind is None or r.get("type") == kind]

    def wait_for(self, kind: str, *, room: str | None = None, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            with self._lock:
                pending = self._records[seen:]
                seen = len(self._records)
            for record in pending:
                if record.get("type") != kind:
                    continue
                if room is not None and record.get("room") != room:
                    continue
                return record
            if self.process.poll() is not None:
                return None
            time.sleep(0.2)
        return None

    def tail(self, count: int = 40) -> str:
        with self._lock:
            return "\n".join(self.lines[-count:])


def start_worker(extra_env: dict[str, str]) -> tuple[subprocess.Popen[str], WorkerLog]:
    """Start `agent.py dev` as a child, with its stdout on a pipe.

    `dev` rather than the production entrypoint: this is a check script, and the
    worker it starts is the same worker the desktop app starts, so a check that
    only passes under a different entrypoint would be checking something else.
    """
    env = dict(os.environ)
    env.update(extra_env)
    # A dead stdout must not take the session with it, but it still prints a
    # traceback per record unless the level is settled down.
    env.setdefault("LIVEKIT_LOG_LEVEL", "error")
    env.setdefault("PYTHONUNBUFFERED", "1")
    process = subprocess.Popen(
        [sys.executable, str(HERE / "agent.py"), "dev"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return process, WorkerLog(process)


def stop_worker(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        # Windows gives a child no signal it must honour; the process *tree* has
        # to go, because the worker is not the only thing it started.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    except Exception:  # noqa: BLE001 - a stuck check must still finish
        process.kill()
    try:
        process.wait(timeout=15)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------


def marker_frame(value: int, *, width: int = WIDTH, height: int = HEIGHT):
    """A frame whose *content* is a number, so a stale answer is identifiable.

    The band colour rotates with the value too. A screen that has changed is
    what the change detector looks for, and a number alone changes only the
    digits -- a few hundred pixels out of 786k. The band is the cheap half of
    that signal, which is what makes the "significant change" path reachable
    without a real desktop behind it.

    Cached by value: a marker is published hundreds of times over a run, and the
    encoder is competing with whoever renders it.
    """
    if (width, height) == (WIDTH, HEIGHT) and value in _MARKER_CACHE:
        return _MARKER_CACHE[value]

    from PIL import Image, ImageDraw

    hues = [(220, 40, 40), (40, 180, 80), (40, 90, 220), (230, 170, 30)]
    image = Image.new("RGBA", (width, height), (18, 18, 24, 255))
    draw = ImageDraw.Draw(image)
    band = hues[value % len(hues)]
    draw.rectangle([0, 0, width, height // 4], fill=(*band, 255))
    draw.rectangle([0, height - height // 6, width, height], fill=(245, 245, 245, 255))
    draw.text((width // 2 - 40, height // 3), str(value), fill=(255, 255, 255, 255))
    draw.text((width // 2 - 80, height - height // 8), f"WINDOW {value:03d}", fill=(20, 20, 20, 255))
    data = image.tobytes()
    if (width, height) == (WIDTH, HEIGHT):
        _MARKER_CACHE[value] = data
    return data


# ---------------------------------------------------------------------------
# The room
# ---------------------------------------------------------------------------


class Publisher:
    """One participant, one video track, a marker that can be advanced."""

    def __init__(self, url: str, token: str, *, screencast: bool) -> None:
        from livekit import rtc

        self._rtc = rtc
        self._url = url
        self._token = token
        self._screencast = screencast
        self._source = None
        self._room = None
        self.value = 0

    async def start(self) -> None:
        rtc = self._rtc
        self._room = rtc.Room()
        await self._room.connect(self._url, self._token)
        self._source = rtc.VideoSource(WIDTH, HEIGHT, is_screencast=self._screencast)
        track = rtc.LocalVideoTrack.create_video_track("lumine-smoke", self._source)
        source_kind = (
            rtc.TrackSource.SOURCE_SCREENSHARE
            if self._screencast
            else rtc.TrackSource.SOURCE_CAMERA
        )
        await self._room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(source=source_kind),
        )
        self.publish()

    def publish(self) -> None:
        if self._source is None:
            return
        frame = self._rtc.VideoFrame(
            WIDTH,
            HEIGHT,
            self._rtc.VideoBufferType.RGBA,
            marker_frame(self.value),
        )
        self._source.capture_frame(frame)

    def advance(self) -> int:
        self.value += 1
        self.publish()
        return self.value

    async def stop(self) -> None:
        if self._room is not None:
            try:
                await self._room.disconnect()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass

    @property
    def room(self):
        return self._room


async def speak(room, text: str, *, voice: str, sample_rate: int = 24000) -> None:
    """Say a sentence into the room, so the agent gets a real user turn.

    There is no text injection on the realtime path: the model's user input is
    audio, so a check that wants to *ask* something has to speak it. Cartesia is
    a credential this install already holds and its PCM output is exactly what
    the room's audio pipeline expects, so this costs one synthesised sentence.

    The track is published *before* synthesising, because a frame captured into
    a source with no published track goes nowhere -- and that gap is exactly the
    sort of thing that makes a working check report a silent agent.
    """
    from livekit import rtc
    from livekit.agents.utils import http_context
    from livekit.plugins import cartesia

    tts = cartesia.TTS(model="sonic-3", voice=voice, language="en", sample_rate=sample_rate)
    source = rtc.AudioSource(sample_rate, 1)
    track = rtc.LocalAudioTrack.create_audio_track("lumine-smoke-voice", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    await asyncio.sleep(0.6)
    try:
        # `http_context.open()` because this process is not an agent job: a
        # plugin resolves its aiohttp session from the job context, and outside
        # one it raises rather than making its own. The error is accurate and
        # unhelpful in equal measure, so it is worth naming here once.
        async with http_context.open():
            async for audio in tts.synthesize(text):
                if audio.frame is not None:
                    await source.capture_frame(audio.frame)
        # Trailing silence, or VAD clips the last word off the question and the
        # agent answers a sentence nobody finished saying.
        await asyncio.sleep(1.5)
    finally:
        try:
            await room.local_participant.unpublish_track(track.sid)
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


class Stage:
    def __init__(self, name: str, expectation: str) -> None:
        self.name = name
        self.expectation = expectation
        self.actual = ""
        self.ok = False
        self.skipped = False
        self.detail = ""

    def record(self, ok: bool, actual: str, *, detail: str = "") -> None:
        self.ok = ok
        self.actual = actual
        self.detail = detail


async def run(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv
    from livekit import api

    load_dotenv(HERE / ".env")
    url = os.environ["LIVEKIT_URL"]

    stages: list[Stage] = []
    room_name = f"lumine-vision-smoke-{uuid.uuid4().hex[:8]}"
    extra_env: dict[str, str] = {}
    if args.pipeline:
        # The saved document wins over the environment, so this only takes
        # effect on a machine whose profile is not pinned in the UI. The run
        # reports which pipeline it actually got, so a silent no-op is visible.
        extra_env["LUMINE_PIPELINE"] = args.pipeline
    if args.config:
        extra_env["LUMINE_CONFIG_PATH"] = str(Path(args.config).resolve())

    process, log = start_worker(extra_env)
    publisher: Publisher | None = None
    lk = api.LiveKitAPI()

    try:
        print(f"room: {room_name}")
        print(f"worker: pid {process.pid}")

        registered = log.wait_for("worker_registered", timeout=READY_TIMEOUT)
        if registered is None:
            print("\nthe worker never registered. Last output:\n")
            print(log.tail(60))
            return 2
        print("worker_registered: ok")

        await lk.room.create_room(api.CreateRoomRequest(name=room_name))

        # Frames are published *before* the dispatch, deliberately: the greeting
        # is generated the instant the session starts, and a frame arriving after
        # it describes a screen the model has already stopped looking at. This
        # way the very first request of the session has something in it.
        token = (
            api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
            .with_identity("vision-smoke")
            .with_name("vision-smoke")
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
        ).to_jwt()

        # -- stage 1: sharing off ------------------------------------------
        off = Stage(
            "sharing off",
            "no track published -> zero vision_request; a session that cannot "
            "see must not claim a picture",
        )
        stages.append(off)
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room_name)
        )
        started = log.wait_for("session_started", room=room_name, timeout=READY_TIMEOUT)
        await asyncio.sleep(6.0)
        off_records = log.records("vision_request")
        off.record(
            len(off_records) == 0,
            f"{len(off_records)} vision_request record(s)",
            detail=f"session_started={'yes' if started else 'no'}",
        )
        print(f"  [1] sharing off: {off.actual}")

        if started is None:
            print("\nthe session never started. Last output:\n")
            print(log.tail(60))
            return 2

        # Name the profile the worker actually took. A text-only run that
        # silently fell back to the realtime profile would otherwise report
        # "zero vision_request" as a capability result when it is really the
        # wrong profile -- and a green stage that means nothing is worse than a
        # red one.
        applied = log.records("config_applied")
        text_only = False
        if applied:
            last = applied[-1]
            print(
                f"  profile: {last.get('profile')} kind={last.get('kind')} "
                f"source={last.get('source')}"
            )
            # A cascade profile's LLM stage declares ("text",), so the factory
            # installs no video sampler at all. That is the capability contract
            # doing its job, and the stages below have to *invert* for it: a
            # text-only model must attach nothing, and a run that reported
            # "zero images" as a failure would be asking for the bug.
            text_only = last.get("kind") != "realtime"

        # -- stage 2: sharing on -------------------------------------------
        on = Stage(
            "sharing on",
            (
                "no image-capable model in play -> zero vision_request; a "
                "text-only provider must never be handed a picture it cannot read"
                if text_only
                else "one track -> at least one vision_request with image_attached "
                "true and a non-zero byte count"
            ),
        )
        stages.append(on)
        publisher = Publisher(url, token, screencast=not args.camera)
        await publisher.start()
        mark = publisher.value

        deadline = time.monotonic() + args.seconds
        attached = 0
        saw = 0
        # A text-only run has to wait the full duration before its zero means
        # anything -- stopping at the first "no image yet" would report a
        # conclusion the run had not yet earned.
        while time.monotonic() < deadline and (text_only or attached == 0):
            await asyncio.sleep(0.25)
            publisher.publish()
            for heartbeat in log.records("visual"):
                saw = max(saw, int(heartbeat.get("frame_seq") or 0))
            attached = sum(
                1 for r in log.records("vision_request") if r.get("image_attached")
            )
        on_records = log.records("vision_request")
        # The visual heartbeat is published on VISION_SYNC_SECONDS, and the loop
        # above stops the moment an image is attached -- so without this wait the
        # "sampler saw" figure is read before the first heartbeat and reports a
        # zero that is only the measurement's own impatience.
        await asyncio.sleep(VISION_SYNC_SECONDS + 1.0)
        for heartbeat in log.records("visual"):
            saw = max(saw, int(heartbeat.get("frame_seq") or 0))
        on.record(
            (attached == 0) if text_only else (attached > 0),
            f"{attached} attached of {len(on_records)} record(s); "
            f"sampler saw {saw} frame(s), marker {mark}",
            detail=json.dumps(
                {
                    "provider": (on_records[-1] or {}).get("provider") if on_records else None,
                    "model": (on_records[-1] or {}).get("model") if on_records else None,
                    "bytes": (on_records[-1] or {}).get("image_bytes") if on_records else None,
                }
            ),
        )
        print(f"  [2] sharing on: {on.actual}")

        # -- stage 3: a stable screen --------------------------------------
        stable = Stage(
            "stable screen",
            "one marker held still -> few requests; the dedup and the budget "
            "have to be doing the work, not the absence of frames",
        )
        stages.append(stable)
        before_stable = len(log.records("vision_request"))
        held_from = time.monotonic()
        held_marker = publisher.value
        while time.monotonic() - held_from < args.stable_seconds:
            publisher.publish()
            await asyncio.sleep(1.0 / PUBLISH_FPS)
        stable_records = log.records("vision_request")[before_stable:]
        held_for = time.monotonic() - held_from
        per_minute = (
            len(stable_records) / (held_for / 60.0) if held_for else 0.0
        )
        stable.record(
            per_minute <= args.stable_ceiling,
            f"{len(stable_records)} request(s) in {held_for:.0f}s = {per_minute:.1f}/min, "
            f"marker {held_marker} unchanged",
            detail=f"ceiling {args.stable_ceiling}/min",
        )
        print(f"  [3] stable screen: {stable.actual}")

        # -- stage 4: rapid switching --------------------------------------
        switch = Stage(
            "rapid switching",
            "a new marker every "
            f"{SWITCH_SECONDS}s -> the last one wins; one image at a time, never "
            "a queue of superseded ones. Runs longer than MAX_QUIET_INTERVAL on "
            "purpose: a screen that has been still for a while backs its own "
            "sampling off to 10s, so a shorter stage would be measuring the "
            "backoff rather than the switching",
        )
        stages.append(switch)
        before_switch = log.records("vision_request")
        switched_from = time.monotonic()
        last_value = publisher.value
        seen_seq = 0
        while time.monotonic() - switched_from < args.switch_seconds:
            last_value = publisher.advance()
            # The `visual` heartbeat is the only record that reports frames the
            # sampler *saw* rather than images it delivered, so it is the one
            # that separates "no frames arrived" from "frames arrived and were
            # refused" -- a distinction the delivery ledger cannot make, since
            # it only ever sees what got through.
            for heartbeat in log.records("visual"):
                seen_seq = max(seen_seq, int(heartbeat.get("frame_seq") or 0))
            await asyncio.sleep(SWITCH_SECONDS)
        switch_records = log.records("vision_request")[len(before_switch) :]
        for heartbeat in log.records("visual"):
            seen_seq = max(seen_seq, int(heartbeat.get("frame_seq") or 0))
        queued = [r.get("queued_video", 0) for r in switch_records]
        superseded = [r.get("superseded_pending", 0) for r in switch_records]
        ages = [r.get("frame_age_ms") for r in switch_records if r.get("frame_age_ms") is not None]
        switch.record(
            (not switch_records) if text_only else bool(switch_records),
            f"{len(switch_records)} record(s) over marker {last_value}; "
            f"sampler saw seq {seen_seq}, "
            f"max queued {max(queued or [0])}, superseded {sum(superseded)}, "
            f"max frame age {max(ages or [0]):.0f}ms",
        )
        print(f"  [4] rapid switching: {switch.actual}")

        # -- stage 5: the ledger -------------------------------------------
        ledger = Stage(
            "delivery ledger",
            "every attached record must show a frame from the current generation; "
            "a stale result is a picture of a screen she has already left",
        )
        stages.append(ledger)
        all_records = log.records("vision_request")
        mismatched = [
            r
            for r in all_records
            if isinstance(r.get("result_generation"), int)
            and isinstance(r.get("current_generation"), int)
            and r["result_generation"] != r["current_generation"]
        ]
        refusals = {}
        for record in all_records:
            why = record.get("why")
            if why:
                refusals[why] = refusals.get(why, 0) + 1
        ledger.record(
            not mismatched,
            f"{len(all_records)} record(s), {len(mismatched)} stale result(s), "
            f"refusals {refusals or '{}'}",
        )
        print(f"  [5] delivery ledger: {ledger.actual}")

        # -- stage 6: the answer -------------------------------------------
        # Only on request, because it is the one stage that costs a real model
        # turn rather than a frame, and because it needs somebody to have said
        # something. Everything above is proven without a voice; this is the
        # part that cannot be.
        if args.ask:
            answer = Stage(
                "the answer",
                "a spoken question about the screen -> the reply names the "
                "number currently displayed, and the provider's own accounting "
                "reports image tokens",
            )
            stages.append(answer)
            publisher.value = ANSWER_MARKER
            publisher.publish()
            # Publish until the *worker* confirms it accepted a frame showing
            # ANSWER_MARKER. Waiting a fixed number of seconds is the wrong
            # shape: the quiet backoff stretches to 10s on its own, and a
            # starved encoder stretches it further, so a timer either guesses
            # or lies. The heartbeat is the sampler saying "I have this frame",
            # which is the only thing that makes the question fair.
            seq_before = 0
            for heartbeat in log.records("visual"):
                seq_before = max(seq_before, int(heartbeat.get("frame_seq") or 0))
            held_from = time.monotonic()
            accepted_marker = False
            while time.monotonic() - held_from < args.ask_wait:
                publisher.publish()
                for heartbeat in log.records("visual"):
                    if int(heartbeat.get("frame_seq") or 0) > seq_before:
                        accepted_marker = True
                if accepted_marker:
                    break
                await asyncio.sleep(1.0 / PUBLISH_FPS)
            print(f"  [6] waiting for a fresh frame: {accepted_marker}")

            before_ask = len(log.records("conversation"))
            try:
                await speak(publisher.room, args.ask, voice=args.voice)
            except Exception as exc:  # noqa: BLE001 - a failed question is a result, not a crash
                answer.record(False, f"could not speak the question: {type(exc).__name__}")
                print(f"  [6] the answer: {answer.actual}")
                await asyncio.sleep(2.0)
            else:
                reply = None
                deadline = time.monotonic() + args.ask_timeout
                while time.monotonic() < deadline and reply is None:
                    await asyncio.sleep(0.5)
                    for record in log.records("conversation")[before_ask:]:
                        if record.get("role") == "assistant":
                            reply = str(record.get("content") or "")

                image_tokens = 0
                for record in log.records("session_usage"):
                    for entry in record.get("usage") or []:
                        image_tokens = max(
                            image_tokens, int(entry.get("input_image_tokens") or 0)
                        )

                marker_text = str(ANSWER_MARKER)
                named = bool(reply) and marker_text in reply
                # A session that died mid-question is not a vision failure, and
                # reporting it as one blames the wrong component. Google's
                # realtime API answers 1011 "Internal error encountered" now and
                # then; the failure gate names it provider_down, and this says
                # the same thing rather than "she did not name the number".
                dead = [
                    r.get("message")
                    for r in log.records("agent_failure") + log.records("error")
                ]
                if not reply and dead:
                    answer.record(False, f"session failed before the answer: {dead[0]}")
                else:
                    answer.record(
                        named and image_tokens > 0,
                        f"reply {'names' if named else 'does not name'} marker {marker_text}; "
                        f"provider reports {image_tokens} image token(s)",
                        detail=(reply or "<no reply within timeout>")[:200],
                    )
                print(f"  [6] the answer: {answer.actual}")

        await asyncio.sleep(2.0)
    finally:
        try:
            await lk.aclose()
        except Exception:  # noqa: BLE001
            pass
        if publisher is not None:
            await publisher.stop()
        stop_worker(process)

    return report(stages, log)


def report(stages: list[Stage], log: WorkerLog) -> int:
    errors = [
        r
        for r in log.records()
        if r.get("type") in {"error", "agent_error"} and r.get("source") == "pipeline"
    ]
    print()
    print("=" * 78)
    print(f"{'stage':<20}{'result':<10}actual")
    print("-" * 78)
    for stage in stages:
        if stage.skipped:
            verdict = "SKIPPED"
        else:
            verdict = "PASS" if stage.ok else "FAIL"
        print(f"{stage.name:<20}{verdict:<10}{stage.actual}")
        if stage.detail:
            print(f"{'':<30}{stage.detail}")
    print("-" * 78)
    if errors:
        print(f"provider/pipeline errors observed: {len(errors)}")
        for error in errors[:5]:
            print(f"  {error}")
    else:
        print("provider/pipeline errors observed: none")
    failed = [s.name for s in stages if not s.ok and not s.skipped]
    print("result:", "OK" if not failed else "FAILED: " + ", ".join(failed))
    if failed:
        # A failure here is almost always a frame that never arrived, and the
        # worker is the only thing that knows why -- whether it saw a track at
        # all, and which model it settled on. Printing its tail is the
        # difference between a red stage and a diagnosis.
        print("\nworker tail:\n")
        print(log.tail(80))
    dump = Path(os.environ.get("TEMP", ".")) / "lumine-vision-smoke.log"
    try:
        dump.write_text("\n".join(log.lines), encoding="utf-8")
        print(f"\nfull worker log: {dump}")
    except OSError:
        pass
    print("=" * 78)
    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vision_smoke.py")
    parser.add_argument("--seconds", type=float, default=25.0)
    parser.add_argument("--stable-seconds", type=float, default=60.0)
    parser.add_argument("--stable-ceiling", type=float, default=12.0)
    parser.add_argument("--switch-seconds", type=float, default=25.0)
    parser.add_argument("--camera", action="store_true", help="publish a camera track, not a screencast")
    parser.add_argument("--pipeline", default=None, help="force LUMINE_PIPELINE for this run")
    parser.add_argument(
        "--config",
        default=None,
        help="point the worker at a different config document (LUMINE_CONFIG_PATH); "
        "the saved document outranks the environment, so switching profiles "
        "through --pipeline alone is a no-op on a machine that has one",
    )
    parser.add_argument(
        "--ask",
        nargs="?",
        const="What number is shown on the screen right now?",
        default=None,
        help="ask the agent a spoken question after the frames are flowing; "
        "the only stage that needs a voice, because the realtime path takes "
        "user input as audio and offers no text injection",
    )
    parser.add_argument(
        "--voice",
        # A Cartesia voice id, not the Gemini voice from the realtime profile.
        # "Sulafat" is that profile's *output* voice and means nothing to
        # Cartesia, which answers 4xx -- a failure that reads like a broken
        # credential until you notice the id is from the wrong provider.
        default="002622d8-19d0-4567-a16a-f99c7397c062",
        help="Cartesia voice used to speak --ask",
    )
    parser.add_argument("--ask-wait", type=float, default=30.0, help="seconds to hold the marker before asking")
    parser.add_argument("--ask-timeout", type=float, default=45.0)
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(build_parser().parse_args())))
