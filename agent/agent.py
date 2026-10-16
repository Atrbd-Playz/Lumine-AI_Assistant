import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

try:
    from .settings.config_store import (SOURCE_UI, active_profile, config_search_summary, effective_document, resolve_profile)
    from .runtime.context_trim import history_budget, max_context_items, trim_context
    from .runtime.emotion_contract import build_emotion_event, extract_emotion_sequence, infer_emotion_from_text
    from .runtime.emotion_voice import TurnVoiceControls
    from .runtime.failure_gate import APOLOGY, FailureGate, describe, handle_llm_error
    from .runtime.history_consent import release as release_history_consent
    from .runtime.latency import LatencyTracker, register_tracker, unregister_tracker
    from .runtime.voice_line import SILENT, speak_line, spoken_via_label
    from .runtime.permission_gate import acquire_gate, release_gate
    from .runtime.progress import register_publisher, unregister_publisher
    from .runtime.visual_state import VisualState
    from .runtime.frame_sampler import register_sampler, unregister_sampler
    from .runtime.vision_link import VisionLink
    from .pipeline.pipeline_factory import ConfigurationRejected, build_pipeline, build_resolved
    from .settings.pipeline_config import pipeline_name
    from .runtime.runtime_events import RuntimeEventPublisher, ToolEventBridge, write_event_line
    from .settings.session_preferences import interruption_mode_from_metadata, job_preferences_from_metadata
    from .tools.browser.session import release as release_browser
    from .tools.http_client import acquire as acquire_http_client, release as release_http_client
    from .tools.tools_policy import compose_instructions
    from .tools.tools_registry import get_tools, tool_ids
except ImportError:
    from settings.config_store import (SOURCE_UI, active_profile, config_search_summary, effective_document, resolve_profile)
    from runtime.context_trim import history_budget, max_context_items, trim_context
    from runtime.emotion_contract import build_emotion_event, extract_emotion_sequence, infer_emotion_from_text
    from runtime.emotion_voice import TurnVoiceControls
    from runtime.failure_gate import APOLOGY, FailureGate, describe, handle_llm_error
    from runtime.history_consent import release as release_history_consent
    from runtime.latency import LatencyTracker, register_tracker, unregister_tracker
    from runtime.voice_line import SILENT, speak_line, spoken_via_label
    from runtime.permission_gate import acquire_gate, release_gate
    from runtime.progress import register_publisher, unregister_publisher
    from runtime.visual_state import VisualState
    from runtime.frame_sampler import register_sampler, unregister_sampler
    from runtime.vision_link import VisionLink
    from pipeline.pipeline_factory import ConfigurationRejected, build_pipeline, build_resolved
    from settings.pipeline_config import pipeline_name
    from runtime.runtime_events import RuntimeEventPublisher, ToolEventBridge, write_event_line
    from settings.session_preferences import interruption_mode_from_metadata, job_preferences_from_metadata
    from tools.browser.session import release as release_browser
    from tools.http_client import acquire as acquire_http_client, release as release_http_client
    from tools.tools_policy import compose_instructions
    from tools.tools_registry import get_tools, tool_ids

try:
    from livekit.agents import (
        Agent,
        AgentSession,
        AgentServer,
        JobContext,
        WorkerOptions,
        cli,
    )
except ImportError:
    class Agent:
        def __init__(self, *args, **kwargs):
            pass

    class AgentSession:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("livekit-agents is required to run the Lumine voice worker.")

    class _MissingServer:
        @staticmethod
        def from_server_options(_options):
            return _MissingServer()

        def on(self, *_args, **_kwargs):
            return lambda fn: fn

    class AgentServer:
        @staticmethod
        def from_server_options(_options):
            return _MissingServer()

    class JobContext:
        pass

    class WorkerOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _MissingCli:
        def run_app(self, *_args, **_kwargs):
            raise RuntimeError("livekit-agents is required to run the Lumine voice worker.")

    cli = _MissingCli()

try:
    from livekit import rtc
except ImportError:
    class _MissingRTC:
        class ParticipantKind:
            PARTICIPANT_KIND_AGENT = "agent"
    rtc = _MissingRTC()


AGENT_DIR = Path(__file__).resolve().parent

#: The persona, split in two for token reasons rather than editorial ones.
#:
#: `persona_core.md` is what every request carries. `persona.md` is the whole
#: thing, unchanged, and the `recall_persona` tool reads it on demand. The full
#: text was ~2,700 tokens and was being re-sent and re-charged on every single
#: turn, which on a per-minute token allowance is a large fixed cost for
#: material most turns never need.
PERSONA_CORE = (AGENT_DIR / "prompts" / "persona_core.md").read_text(encoding="utf-8")

# Tauri launches this script with the repository root as cwd; keep worker
# configuration anchored to the agent directory like manual `python agent.py`.
load_dotenv(AGENT_DIR / ".env")

logging.basicConfig(level=logging.INFO)

# A broken stderr pipe must not turn every log line into a traceback.
#
# The desktop app pipes the worker's output, and if that pipe's read end goes away
# then `StreamHandler.emit` fails on `self.stream.flush()`. Left alone, logging
# prints a "--- Logging error ---" block with a full traceback *for every single
# record* -- which is how a real one-line failure ended up buried under thousands
# of identical ones. The record is still lost either way; only the noise goes.
logging.raiseExceptions = False

logger = logging.getLogger("lumine")
WORKER_STARTED_AT = time.monotonic()
EMOTION_DEBUG = os.getenv("LUMINE_DEBUG_EMOTION", "false").lower() == "true"

#: How often the sight state is re-read between events.
#:
#: Both things it exists to notice have no event of their own. Frames that stop
#: arriving produce nothing at all -- the sampler goes quiet, and quiet is
#: exactly what a dead stream and an idle screen look like from here -- and a
#: share whose first frame is late changes state with no record anywhere. So a
#: slow tick recomputes the block, and only a *changed* block does anything:
#: the comparison happens here and the cost is one string build.
#:
#: Five seconds rather than one: it is a resolution nobody can perceive on a
#: voice turn, and it bounds how long `vision_degraded` can be wrong by.
VISION_SYNC_SECONDS = 5.0


def emit_runtime_event(event_type: str, **payload):
    """Keep the worker bootstrap event contract for Tauri readiness handling."""
    record = {"type": event_type}
    record.update({key: value for key, value in payload.items() if key != "type"})
    # Routed through the publisher's writer rather than printing directly: a write
    # to a broken stdout pipe raises OSError, and an event about the worker failing
    # to start must not be the thing that stops it starting.
    write_event_line(json.dumps(record, default=str, separators=(",", ":")))
    return record


async def publish_emotion_event(room: rtc.Room, event: dict[str, object]) -> None:
    payload = event.get("payload", {})
    if EMOTION_DEBUG:
        logger.info(
            "[Emotion] resolved: %s %.2f (%s)",
            payload.get("primary"),
            payload.get("intensity"),
            payload.get("source"),
        )
        logger.info("[Emotion] publishing LiveKit event")
    try:
        await room.local_participant.publish_data(
            json.dumps(event),
            reliable=True,
            topic="lumine.emotion",
        )
        if EMOTION_DEBUG:
            logger.info("[Emotion] packet published")
    except Exception:
        logger.exception("[Emotion] packet publish failed")


#: Sentinel for a `getattr` default that must never match a real track source.
#: Using `None` here would make an unknown source look like "no source", and an
#: audio track subscribing would then be read as "the screen share stopped".
_NO_MATCH = object()


def video_source_name(publication: Any) -> str | None:
    """``camera``, ``screen``, or ``None`` for anything that is not a video track.

    Read from the publication rather than the track because the publication is
    what the room reports on both subscribe and unsubscribe, and because the
    LiveKit stub used when the package is absent exposes no ``TrackSource`` at
    all -- a missing SDK must degrade to "we cannot tell", never to a guess.
    """
    track_source = getattr(rtc, "TrackSource", None)
    if track_source is None:
        return None
    source = getattr(publication, "source", None)
    if source is None:
        return None
    if source == getattr(track_source, "SOURCE_SCREENSHARE", _NO_MATCH):
        return "screen"
    if source == getattr(track_source, "SOURCE_CAMERA", _NO_MATCH):
        return "camera"
    return None


def build_instructions(profile: str) -> str:
    instructions = compose_instructions(PERSONA_CORE)
    if profile == "gemini_live":
        instructions += """

## Response continuity
Finish the current response before yielding, unless the user clearly interrupts.
"""
    return instructions


def speak_now(session, text: str, publisher) -> None:
    """Say something immediately, on a best-effort basis.

    Used for the apology after a failure. It is scheduled rather than awaited
    because the error handler is synchronous, and it deliberately does not go
    through the LLM: the LLM is what just failed, and asking it to explain its
    own failure is how a turn turns into silence.

    The speech itself is delegated to :func:`speak_line`, which owns the
    escalation. This function used to call ``say()`` directly and swallow the
    failure, which is *why the apology was inaudible* on the default profile:
    a realtime model has no TTS stage, so ``say()`` raises, and the only symptom
    was a debug line. Silence after a failure is indistinguishable from a broken
    microphone, which is the only reason this function exists -- so a line that
    reaches no channel at all is now published to the app rather than logged and
    forgotten, which is the difference between a user who is told and a user who
    is left wondering whether the microphone died.
    """
    line = str(text or "").strip()
    if not line or session is None:
        return

    async def announce() -> None:
        channel = await speak_line(session, line, allow_interruptions=True)
        if channel == SILENT:
            logger.warning(
                "[LLM] no audio channel for the failure notice; the app was told instead"
            )
            try:
                publisher.emit_and_publish(
                    "lumine.notice",
                    {
                        "type": "info",
                        "title": "Something went wrong",
                        "message": (
                            "I hit a problem and could not say so out loud. "
                            f"{line}"
                        ),
                    },
                )
            except Exception:  # noqa: BLE001 - never let a courtesy notice break a session
                logger.debug("[LLM] the failure notice did not send", exc_info=True)
        else:
            logger.info("[LLM] failure notice spoken via %s", spoken_via_label(channel))

    try:
        asyncio.get_running_loop().create_task(announce())
    except RuntimeError:
        # No running loop: a test or a script. Speak it here rather than dropping
        # the line on the floor.
        try:
            asyncio.run(announce())
        except Exception as exc:  # noqa: BLE001
            logger.debug("[LLM] could not speak the failure notice: %s", exc)


def _missing_credential_labels(diagnostics: object) -> list[str]:
    """Which providers a rejection says are missing a key.

    Narrow on purpose. A *credential* rejection is the one a person can fix, and
    it is the one that must not be reduced to "the configuration was rejected" --
    that sentence sends somebody to look for a typo in a JSON file when the actual
    problem is an empty slot on the Providers page. Anything else is returned
    empty, so a malformed profile is not mislabelled as a missing key.
    """
    labels: list[str] = []
    for diagnostic in diagnostics or ():
        code = str(getattr(diagnostic, "code", "") or "")
        message = str(getattr(diagnostic, "message", "") or "")
        if code != "credential.missing":
            continue
        # "Groq has no credential available." -> "Groq", without re-deriving the
        # provider table: the message was written to be read.
        label = message.split(" has no credential", 1)[0].strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def _rejection_reason(exc: BaseException) -> str:
    """One sentence a person can act on, from whatever the builder raised.

    The provider exceptions are the useful case and they are not uniform: some
    carry a ``diagnostics`` list written for a settings screen, some are a bare
    ``ValueError`` from a plugin's constructor. Both are reduced to the first
    diagnostic that names a provider, and anything else falls back to the
    exception's own text -- which is better than a generic sentence, because the
    alternative is the traceback this exists to replace.
    """
    diagnostics = getattr(exc, "diagnostics", None) or ()
    for diagnostic in diagnostics:
        if str(getattr(diagnostic, "code", "")) == "credential.missing":
            return str(getattr(diagnostic, "message", "")) or "a credential is missing."
    for diagnostic in diagnostics:
        message = str(getattr(diagnostic, "message", "") or "").strip()
        if message:
            return message
    text = str(exc).strip()
    return text or f"{type(exc).__name__} while building the voice profile."


def _publish_setup_notice(publisher: Any, title: str, message: str) -> None:
    """Tell the app a profile was not applied, and what is missing.

    Published on the data channel as well as stdout, for the same reason every
    other notice is: a notice that only exists when the worker happened to be
    started from a console is a notice that silently does not arrive in the app
    that has to act on it. Never raises -- this runs on the failure path, and the
    failure path is the wrong place to add a second failure.
    """
    try:
        publisher.emit_and_publish("lumine.notice", {"type": "warn", "title": title, "message": message})
    except Exception:  # noqa: BLE001 - a notice must never break session setup
        logger.debug("[Config] the setup notice did not send", exc_info=True)


class Lumine(Agent):
    def __init__(self, tools=None, profile: str = "gemini_live", visual: VisualState | None = None):
        # Held beside the instructions rather than folded into them, because one
        # of them changes and the other does not: `base` is the persona and is
        # paid exactly once, and the sight block is the part a push re-sends.
        # Composing them at every push from the two pieces is what makes the
        # whole thing diffable against the last push.
        self._base_instructions = build_instructions(profile)
        self._vision = visual
        self._vision_block = visual.vision_block() if visual is not None else ""
        super().__init__(
            instructions=self._base_instructions + self._vision_block,
            tools=tools if tools is not None else get_tools(),
        )

    async def sync_vision_instructions(self) -> bool:
        """Push the sight state to the model when, and only when, it changed.

        The block is compared as a whole string, so a session whose sight never
        changes pays for one push at construction and nothing after. That
        matters more than it looks: ``update_instructions`` re-sends the *entire*
        instruction string to the provider, so every push costs the persona
        again -- which is exactly why a push on a timer with no comparison would
        be a steady tax on every conversation for a line that had not moved.

        Returns whether a push happened, so the caller can log one line per
        real transition instead of one per tick.
        """
        if self._vision is None:
            return False
        block = self._vision.vision_block()
        if block == self._vision_block:
            return False
        try:
            await self.update_instructions(self._base_instructions + block)
        except Exception:  # noqa: BLE001 - a push must never end a session
            # Left unadvanced on purpose. The next transition will try again
            # rather than leaving her with a sight state that is wrong for the
            # rest of the conversation, and a provider that refuses this call
            # says so once per transition rather than once per tick.
            logger.warning("[vision] could not push the sight state to the model", exc_info=True)
            return False
        self._vision_block = block
        # One line, whole block, no frames: `[vision]` marks it as the same
        # channel the rest of this module logs on, and a squashed single line is
        # greppable in a way a wrapped paragraph is not.
        logger.info("[vision] sight state pushed: %s", " ".join(block.split()))
        return True

    async def llm_node(self, chat_ctx, tools, model_settings):
        """Stream the reply out while teaching the voice how it is being said.

        The override exists for one reason: *when*. The emotion the reply
        demonstrates is only knowable once some of it has arrived, and the
        obvious place to look — ``conversation_item_added``, where the avatar
        already gets its emotion — runs when the reply is finished. Setting the
        voice there would shape every reply except the one that earned it.

        So it happens here instead, on the way out. Each chunk is observed
        *before* it is yielded, so the control reaches the synthesizer ahead of
        the text that revealed it rather than behind it. Text from earlier
        chunks may already be spoken under the profile's default; there is no
        way around that without holding the reply back, and a turn that waits
        to be sure how it feels is a turn that has stopped answering.

        ``chat_ctx``, ``tools`` and ``model_settings`` are passed through
        untouched — this is a listener, not a replacement for the node.
        """
        # No synthesizer of ours here means no control to apply, and the
        # guard is on the *object* rather than the profile because the profile
        # only says what was configured.
        try:
            activity = self._get_activity_or_raise()
        except Exception:  # noqa: BLE001 - the node below raises its own error
            activity = None
        controls = TurnVoiceControls(getattr(activity, "tts", None))

        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            # The node may yield a ChatChunk, a bare str, or a flush sentinel;
            # only the first two carry text, and a sentinel must not be read as
            # one.
            if isinstance(chunk, str):
                delta = chunk
            else:
                delta = getattr(getattr(chunk, "delta", None), "content", None)
            if isinstance(delta, str) and delta:
                controls.observe(delta)
            yield chunk


async def build_session_components(publisher, room_name: str, interruption_mode: str):
    """Resolve the configuration for this job and build the session components.

    Precedence is the rule from ``config_store``: a saved document wins over
    ``agent/.env``, which wins over the built-in defaults. A saved profile is only
    used when it resolves *and* validates; anything unexpected falls back to the
    environment rather than failing the job, because losing a voice session to a
    bad settings file would be a far worse outcome than ignoring it.
    """
    document, source, diagnostics = effective_document()

    # Named on every outcome, so "I saved it and nothing changed" is answerable
    # from the log instead of requiring a guess about who started the worker.
    config_path_label = config_search_summary()

    if source == SOURCE_UI:
        profile = active_profile(document)
        if profile is not None:
            resolved = resolve_profile(profile, interruption_mode=interruption_mode)
            try:
                components, build_diagnostics = await build_resolved(resolved, validate=True)
            except ConfigurationRejected as exc:
                # Falling back is right -- a working voice beats no voice -- but
                # falling back *silently between stacks* is how "the pipeline voice
                # has a problem" became unanswerable. A user picks Pipeline, a
                # missing key rejects it, and they are quietly given Realtime (or
                # nothing) with no way to learn why. The rejection is the only
                # evidence anyone has, so it is published in full, named on the
                # event, and raised as a notice the app can toast.
                missing = _missing_credential_labels(exc.diagnostics)
                publisher.emit(
                    "config_rejected",
                    room=room_name,
                    profile=resolved.profile_id,
                    kind=resolved.kind,
                    config_path=config_path_label,
                    diagnostics=[d.to_dict() for d in exc.diagnostics],
                    # Named on the event rather than left inside `diagnostics`,
                    # because that is the field the activity panel renders and the
                    # one a person reads first.
                    reason=(
                        f"{', '.join(missing)} has no key, so this profile cannot be built"
                        if missing
                        else "the saved profile could not be built"
                    ),
                    fell_back=True,
                    fell_back_to=pipeline_name(),
                )
                logger.error(
                    "[Config] saved profile %r rejected (%s); falling back to the "
                    "environment pipeline %r",
                    resolved.profile_id,
                    "; ".join(d.message for d in exc.diagnostics) or "no reason given",
                    pipeline_name(),
                )
                if missing:
                    _publish_setup_notice(
                        publisher,
                        f"Profile '{resolved.name}' needs a {', '.join(missing)} key",
                        "She is using the fallback voice instead. Add the key in "
                        "Settings to get the voice you chose.",
                    )
            else:
                publisher.emit(
                    "config_applied",
                    room=room_name,
                    profile=resolved.profile_id,
                    source="ui",
                    kind=resolved.kind,
                    providers=list(resolved.providers()),
                    config_path=config_path_label,
                    diagnostics=[d.to_dict() for d in build_diagnostics],
                )
                return components
        else:
            publisher.emit(
                "config_rejected",
                room=room_name,
                reason="the active profile reference does not resolve",
                config_path=config_path_label,
            )
            logger.warning("[Config] active profile reference does not resolve; using the environment")

    for diagnostic in diagnostics:
        if diagnostic.severity == "error":
            logger.warning("[Config] ignoring saved configuration: %s", diagnostic.message)

    # The fallback is the last thing standing between a rejected profile and a
    # session, so it is guarded rather than left to raise. Unguarded, a second
    # rejection -- which is what happens when the reason the first was rejected is
    # a missing credential, because the fallback needs its own -- ended the
    # entrypoint with a traceback written into a pipe. The observable result was
    # the worst one available: the worker was alive, the app showed its events, and
    # there was no session, no greeting and no audio, with nothing anywhere saying
    # which key was absent.
    try:
        components = await build_pipeline(pipeline_name(), interruption_mode=interruption_mode)
    except Exception as exc:  # noqa: BLE001 - reported, then re-raised
        reason = _rejection_reason(exc)
        publisher.emit(
            "config_rejected",
            room=room_name,
            reason=reason,
            kind="realtime" if pipeline_name() == "gemini_live" else "pipeline",
            config_path=config_path_label,
            diagnostics=getattr(exc, "diagnostics", []) and [
                d.to_dict() for d in exc.diagnostics
            ]
            or [],
            fell_back=False,
        )
        _publish_setup_notice(
            publisher,
            "No voice profile could be started",
            f"{reason} Add the key in Settings, then start the call again.",
        )
        logger.error("[Config] the fallback profile could not be built either: %s", reason)
        raise
    publisher.emit(
        "config_applied",
        room=room_name,
        profile=components.profile,
        source="env",
        kind="realtime" if components.profile == "gemini_live" else "pipeline",
        config_path=config_path_label,
    )
    if source != SOURCE_UI:
        logger.info(
            "[Config] agent/.env is governing. Saved settings are read from: %s",
            config_path_label,
        )
    return components


async def entrypoint(ctx: JobContext):
    publisher = RuntimeEventPublisher(ctx.room, room_name=ctx.room.name)
    latency = LatencyTracker(publisher.emit)
    latency.mark("entrypoint", room=ctx.room.name)

    logger.info("Connecting to room: %s", ctx.room.name)
    tools = get_tools()
    metadata = getattr(getattr(ctx, "job", None), "metadata", None)
    preferences = job_preferences_from_metadata(metadata)
    interruption_mode = preferences.interruption_mode
    logger.info("Voice interruption mode: %s", interruption_mode)
    if preferences.profile_id:
        logger.info("Requested configuration profile: %s", preferences.profile_id)
    logger.info("Enabled tools: %s", ", ".join(tool_ids(tools)) or "none")

    await ctx.connect()
    publisher.emit("connected", room=ctx.room.name)

    profile = "unknown"
    try:
        components = await build_session_components(publisher, ctx.room.name, interruption_mode)
    except Exception as exc:
        publisher.emit("error", message=str(exc), source="pipeline")
        latency.mark("pipeline_error", profile=profile)
        raise
    latency.mark(
        "pipeline_built",
        profile=components.profile,
        model=components.model_name,
        interruption_mode=components.interruption_mode,
        response_token_limit=components.response_token_limit,
    )
    logger.info(
        "Voice pipeline ready: profile=%s model=%s interruption_mode=%s",
        components.profile,
        components.model_name,
        components.interruption_mode,
    )

    session = AgentSession(**components.session_kwargs)
    tool_events = ToolEventBridge(publisher, latency=latency)
    # Counts consecutive LLM failures and opens a circuit on a run of them, so a
    # rate-limited session stops trying instead of spending what is left.
    gate = FailureGate()

    # Approvals for actions that are not pre-allowed. Session-scoped: the gate
    # is keyed by this session, its pending asks die with it, and a packet or
    # transcript that arrives after that resolves nothing -- so an approval
    # cannot be replayed into a later conversation.
    permissions = acquire_gate(session, room=ctx.room, publisher=publisher, label=ctx.room.name)

    # The timing recorder is session-scoped for the same reason the gate is: a
    # tool annotates its own work from a `RunContext`, and the only thing that
    # context carries is the session. Registered before `session.start` so the
    # greeting cannot run without it.
    register_tracker(session, latency)

    # Lets a tool narrate its own steps while it works. Same reasoning as the
    # tracker above and the gate above it: a tool is handed a RunContext and
    # nothing else, so the session is the only handle it has, and a session-keyed
    # registry is the only shape that works. Without this the user watches a tool
    # run for several seconds with nothing between "started" and "finished" --
    # which for a staged WhatsApp send is the entire experience.
    register_publisher(session, publisher)

    # How much of the provider's per-minute allowance the conversation may use,
    # worked out once per session from what the fixed prompt actually costs. This
    # is the difference between a long conversation and a rejected one: a
    # per-minute limit is charged prompt-plus-completion on *every* request, so a
    # fixed prompt that is already near the ceiling leaves nothing for the
    # exchange, and the session goes quiet with no error anywhere.
    #
    # Published as well as applied, because a number that silently decides whether
    # Lumine can remember anything is not a number anybody can act on.
    budget = history_budget(
        getattr(components, "llm_provider", ""),
        output_cap=components.response_token_limit or 0,
        persona=PERSONA_CORE,
    )
    history_tokens = int(budget["budget"])
    publisher.emit("context_budget", **budget)
    if budget["fixed"] >= budget["tpm"] > 0:
        # The condition worth shouting about: the fixed prompt alone does not fit
        # in a minute, so trimming the conversation cannot save it. Fewer enabled
        # tools is the lever -- each one is a schema on every request.
        logger.warning(
            "[Context] the fixed prompt is %d tokens against a %d TPM allowance; "
            "no conversation fits and only one request per minute will",
            budget["fixed"],
            budget["tpm"],
        )

    # What Lumine can currently see, and how old it is. The sampler decides
    # *which* frames reach the model; this records what that decision means, so
    # the UI can say "last frame 2s ago" instead of implying a live view.
    visual = VisualState(publisher.emit)
    # Asked of the catalog before anything is built, because this is the one
    # fact about sight that no room event can ever supply: a model that cannot
    # read an image receives no frames at all, and an absence of frames is
    # indistinguishable from a broken capture to anything that has to infer it.
    # `_stage_sees_frames` already answered it for `room_options`; this is the
    # same answer, carried so it can be *said* rather than only enforced.
    visual.note_capability(components.sees_frames)
    # Declared here rather than at the task, so a shutdown that races session
    # setup finds a bound name instead of an UnboundLocalError -- which would
    # be the one failure the shutdown path is not allowed to have.
    vision_tick: asyncio.Task | None = None
    sampler = components.session_kwargs.get("video_sampler")
    # The provider-boundary observer. `vision_ready` is a statement about
    # capability and permission and says nothing about the request that just
    # went out, so the only thing that can answer "did she actually look?" is
    # code standing where an image becomes a request. It is optional: a profile
    # that never sends a frame keeps the sampler and loses only the telemetry.
    link: VisionLink | None = None
    if sampler is not None:
        link = VisionLink(
            provider=getattr(components, "llm_provider", ""),
            model=components.model_name,
            emit=publisher.emit,
        )
        link.bind(sampler)
        attach_sampler = getattr(sampler, "attach_link", None)
        if callable(attach_sampler):
            attach_sampler(link)
        # The session seam a tool needs: a worker serves several rooms, so there
        # is no module-level "the sampler" to import, and the tool is handed a
        # RunContext and nothing else. Same shape as the step narrator's.
        register_sampler(session, sampler)
    attach = getattr(sampler, "attach", None)
    if callable(attach):
        attach(visual)

    @ctx.room.on("track_subscribed")
    def on_track_subscribed(participant, publication, _track):
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            return
        source = video_source_name(publication)
        if source is not None:
            visual.note_source(source)

    def on_video_track_gone(participant, publication, *_args):
        # Called for both `track_unsubscribed` (we stopped receiving) and
        # `track_unpublished` (the app stopped sending). Either means the view
        # is no longer live, and the model must not keep implying otherwise.
        if video_source_name(publication) is not None:
            visual.note_source(None)

    ctx.room.on("track_unsubscribed", on_video_track_gone)
    ctx.room.on("track_unpublished", on_video_track_gone)

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event):
        # A final transcript is the first moment a request exists: the user has
        # stopped speaking and STT has committed to words. Everything measured
        # from here is time the user is waiting for.
        if not getattr(event, "is_final", False):
            return
        latency.begin_turn()
        # Anchor the turn on a current frame. Without this the answer can be
        # built on a still that is a whole silent interval old, which is how a
        # response ends up describing the window you closed two seconds ago.
        request_fresh = getattr(sampler, "request_fresh", None)
        if callable(request_fresh):
            # Tagged, because the delivery record is what makes "the picture was
            # 40ms old when she answered" a number rather than a hope. `general`
            # is the normal-turn profile; a profile only differs when something
            # can name *what* is being read, and no realtime turn can.
            request_fresh("turn", "general")

    @session.on("metrics_collected")
    def on_metrics_collected(event):
        """Provider-reported timings, kept apart from our own.

        These numbers come off the wire rather than off our clock, so they are
        published unrounded as provider work and never re-measured locally --
        doing that would report our event-loop latency as the model's. No label
        or content travels with them: stage, phase, and a duration.
        """
        metrics = getattr(event, "metrics", None)
        if metrics is None:
            return
        kind = str(getattr(metrics, "type", "") or "")
        cancelled = bool(getattr(metrics, "cancelled", False))

        def seconds(name: str) -> float | None:
            value = getattr(metrics, name, None)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
            return None

        if kind in ("llm_metrics", "realtime_model_metrics"):
            if not cancelled:
                ttft = seconds("ttft")
                if ttft is not None:
                    latency.emit_duration("llm_first_token", ttft * 1000, phase="model")
            duration = seconds("duration")
            if duration is not None:
                latency.emit_duration("llm_request", duration * 1000, phase="model", cancelled=cancelled)
            acquire = seconds("acquire_time")
            if acquire is not None:
                latency.emit_duration("llm_connect", acquire * 1000, phase="network")
        elif kind == "stt_metrics":
            duration = seconds("duration")
            if duration is not None:
                latency.emit_duration("stt_request", duration * 1000, phase="stt", cancelled=cancelled)
            acquire = seconds("acquire_time")
            if acquire is not None:
                latency.emit_duration("stt_connect", acquire * 1000, phase="network")
        elif kind == "tts_metrics":
            if not cancelled:
                ttfb = seconds("ttfb")
                if ttfb is not None:
                    latency.emit_duration("tts_first_audio", ttfb * 1000, phase="tts")
            duration = seconds("duration")
            if duration is not None:
                latency.emit_duration("tts_request", duration * 1000, phase="tts", cancelled=cancelled)

    @ctx.room.on("data_received")
    def on_permission_response(payload, participant, _kind, topic):
        # The trust boundary for UI answers lives in the gate: this handler
        # only adds the topic filter and the sender, and the gate checks the
        # sender kind, the payload shape, and whether the id was minted here.
        if topic != "lumine.permission" or participant is None:
            return
        permissions.handle_channel_message(
            payload,
            sender_kind=getattr(participant, "kind", None),
            sender_identity=str(getattr(participant, "identity", "")),
        )

    @ctx.room.on("data_received")
    def on_vision_report(payload, participant, _kind, topic):
        # A report, not an answer: nothing is authorised by it and nothing
        # happens because of it beyond a state she is told. So the checks here
        # are the ones that matter for *that* -- the topic, and not our own
        # participant -- rather than the gate's, which exists to decide whether
        # an action may run.
        if topic != "lumine.vision" or participant is None:
            return
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            return
        # Permission is decided in a browser window this process never sees, so
        # this is the only path by which "the OS said no" can be known here.
        # Without it the model is told `enabled` for a capture that was refused,
        # and the persona's instruction to say so plainly has nothing to say.
        if visual.note_report(payload):
            logger.info(
                "[vision] capture state reported by the app: permission=%s phase=%s",
                visual.permission,
                visual.phase(),
            )

    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        item = event.item
        # Trim as the conversation grows rather than when something breaks. A
        # context-length error is the provider's 400, and by then the turn has
        # already failed silently from the user's side.
        outcome = trim_context(session, token_budget=history_tokens)
        if outcome.trimmed:
            publisher.emit(
                "context_trimmed",
                items_before=outcome.items_before,
                items_after=outcome.items_after,
                limit=max_context_items(),
                tokens_before=outcome.tokens_before,
                tokens_after=outcome.tokens_after,
                budget=outcome.budget,
            )
        elif outcome.over_budget:
            # Trimmed as far as it will go and still over: the *fixed* prompt is
            # the problem, not the conversation. Worth saying, because the panel
            # would otherwise show a conversation that is not growing and a
            # session that is not answering.
            publisher.emit(
                "context_over_budget",
                tokens=outcome.tokens_after,
                budget=outcome.budget,
                message=(
                    "Her instructions alone use more than this voice provider allows "
                    "per minute, so no reply fits. Turn off tools she does not need."
                ),
            )
        content = getattr(item, "content", [])
        text = " ".join(
            part if isinstance(part, str) else getattr(part, "text", "")
            for part in content
        ).strip()
        if not text:
            return

        publisher.emit(
            "conversation",
            role=getattr(item, "role", "assistant"),
            content=text,
        )

        role = getattr(item, "role", "")
        if role == "assistant":
            # The words are final, so the turn the user was waiting on is over.
            # Recorded before anything else here, because a latency figure that
            # calls a spoken answer an interruption is worse than no figure.
            latency.note_answer()
            emotion_events = []
            for part in re.split(r"\b(?:then|and|but|while|so|as|because)\b|[;,.!?]+", text):
                if not part.strip():
                    continue
                emotion_events.extend(extract_emotion_sequence(part, source="llm"))
            if not emotion_events:
                emotion_events = extract_emotion_sequence(text, source="llm")
        elif role == "user":
            lower = text.lower()
            roleplay_cues = (
                "act ", "be ", "pretend to be", "roleplay", "play as", "do a", "look ", "blush", "smile", "laugh",
                "wink", "shy", "proud", "angry", "jealous", "curious", "calm", "surprised", "happy", "excited"
            )
            if any(cue in lower for cue in roleplay_cues):
                emotion_events = []
                for part in re.split(r"\b(?:then|and|but|while|so|as|because)\b|[;,.!?]+", text):
                    if not part.strip():
                        continue
                    emotion_events.extend(extract_emotion_sequence(part, source="user"))
                if not emotion_events:
                    emotion_events = extract_emotion_sequence(text, source="user")
            else:
                emotion_events = []
        else:
            emotion_events = []

        if emotion_events:
            async def publish_sequence(events):
                for index, event in enumerate(events):
                    if index:
                        await asyncio.sleep(0.5)
                    await publish_emotion_event(ctx.room, event)
                    publisher.emit(
                        event["type"],
                        **{key: value for key, value in event.items() if key != "type"},
                    )

            asyncio.create_task(publish_sequence(emotion_events))

    @session.on("tool_execution_updated")
    def on_tool_execution_updated(event):
        tool_events.handle(event)

    @session.on("function_tools_executed")
    def on_function_tools_executed(event):
        # The updated event is the normal path. This batch handler is retained
        # as a compatibility fallback for SDK versions that omit one update.
        tool_events.handle_function_tools(event)

    @session.on("agent_state_changed")
    def on_agent_state_changed(event):
        state = getattr(event, "new_state", None)
        if state:
            latency.mark("agent_state", state=state)
            if state == "idle":
                # The answer has been spoken. This is the number the user feels:
                # from "the words were final" to "she has finished replying".
                latency.end_turn()

    @session.on("user_state_changed")
    def on_user_state_changed(event):
        state = getattr(event, "new_state", None)
        if state:
            latency.mark("user_state", state=state)
            publisher.emit("user_state", state=state)

    @session.on("agent_false_interruption")
    def on_false_interruption(event):
        publisher.emit(
            "false_interruption",
            resumed=bool(getattr(event, "resumed", False)),
        )

    @session.on("close")
    def on_session_close(event):
        # Refuses anything still waiting on an answer: a session that is gone
        # cannot act on an approval that arrives after it.
        permissions.close(str(getattr(event, "reason", "session closed")))
        publisher.emit("session_close", reason=str(getattr(event, "reason", "unknown")))

    @session.on("speech_created")
    def on_speech_created(event):
        latency.mark(
            "speech_created",
            source=getattr(event, "source", None),
            user_initiated=getattr(event, "user_initiated", None),
        )
        speech_handle = getattr(event, "speech_handle", None)
        add_done_callback = getattr(speech_handle, "add_done_callback", None)
        if callable(add_done_callback):
            def on_speech_done(handle):
                publisher.emit(
                    "speech_finished",
                    speech_id=getattr(handle, "id", None),
                    interrupted=bool(getattr(handle, "interrupted", False)),
                    allow_interruptions=bool(getattr(handle, "allow_interruptions", True)),
                )

            add_done_callback(on_speech_done)

    @session.on("session_usage_updated")
    def on_session_usage_updated(event):
        usage = getattr(event, "usage", None)
        model_usage = getattr(usage, "model_usage", []) if usage is not None else []
        serialized_usage = []
        for item in model_usage:
            try:
                if hasattr(item, "model_dump"):
                    serialized_usage.append(item.model_dump(mode="json"))
                else:
                    serialized_usage.append(str(item))
            except Exception:
                serialized_usage.append(repr(item))
        publisher.emit(
            "session_usage",
            usage=serialized_usage,
            response_token_limit=components.response_token_limit,
        )

    @session.on("error")
    def on_session_error(event):
        raw = getattr(event, "error", event)
        failure = handle_llm_error(gate, raw)
        payload = describe(failure)
        publisher.emit("error", message=failure.message, source=str(getattr(event, "source", "session")))
        # A separate event, because "something went wrong" and "you have hit a
        # limit" call for different reactions from the app: one is a bug report,
        # the other is a wait. Published on the data channel as well as stdout,
        # because stdout only reaches the app when Tauri is the one running the
        # worker -- and a notice that depends on how the process was started is a
        # notice that silently does not arrive.
        notice = {"type": "limit" if failure.is_limit else "agent_failure", **payload}
        publisher.emit_and_publish("lumine.notice", notice)
        if failure.retry_after:
            publisher.emit("retry_after", seconds=failure.retry_after)
        logger.warning(
            "[LLM] %s (%s)%s",
            failure.kind,
            failure.message,
            f" retry after {failure.retry_after}s" if failure.retry_after else "",
        )

        if gate.should_speak_apology:
            gate.mark_spoken()
            # Speaking is the point. Silence after a failure is
            # indistinguishable from a broken microphone, so the user is told
            # something went wrong even when there is nothing to tell them yet.
            speak_now(session, APOLOGY, publisher)

    async def close_session(reason: str = "room ended"):
        publisher.emit("session_ending", room=ctx.room.name, reason=reason)
        # Stop asking whether the picture is current before the room that made
        # it current goes away. Left running it would tick into a closed session
        # for as long as the process lives, which for a persistent worker is
        # forever.
        if vision_tick is not None:
            vision_tick.cancel()
        # Flush what a turn in flight would never close itself, and say the view
        # went dark while the publisher can still carry it.
        latency.end_turn()
        visual.note_source(None)
        try:
            await session.aclose()
        finally:
            # Drops the gate with everything it was still waiting on, then
            # releases the browser and the shared HTTP client. All three are
            # last-one-out: the browser's *profile* (logins the user typed
            # themselves) survives on disk, but its pages do not survive a
            # conversation -- a fresh session must not open on the last
            # session's screen.
            release_gate(session)
            unregister_tracker(session)
            unregister_publisher(session)
            # The sampler seam is keyed on this session for the same reason the
            # three above are: a tool reaching for a picture in the *next*
            # conversation must find that conversation's sampler, or none at all
            # -- never the last one's, which would arm a request nobody serves.
            unregister_sampler(session)
            # A history grant is a promise about *this* conversation. It must not
            # survive into the next one, where the same words would authorise a
            # different chat.
            release_history_consent(session)
            await release_browser()
            await release_http_client()
        publisher.emit("session_ended", room=ctx.room.name, reason=reason)
        await publisher.aclose()

    ctx.add_shutdown_callback(close_session)

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant):
        remaining_users = [
            remote
            for remote in ctx.room.remote_participants.values()
            if remote.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        ]
        if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT and not remaining_users:
            # The person who could answer an approval card has left. Refuse
            # whatever is pending now rather than waiting out its deadline
            # against a participant that is no longer there.
            permissions.close("user left room")
            ctx.shutdown(reason="user left room")

    # Built before `session.start` because the sight block is part of the very
    # first instruction string: telling her on the first turn that this model
    # can see, and what is shared, costs nothing -- pushing it one tick later
    # would cost a full instruction re-send to say what was knowable already.
    agent = Lumine(tools, profile=components.profile, visual=visual)
    await session.start(
        room=ctx.room,
        agent=agent,
        # Resolved by the factory from the catalog, not decided here. `video_input`
        # is what turns a published camera or screenshare track into frames this
        # session can actually read, and the factory is the only place that knows
        # whether the selected model is one that can.
        room_options=components.room_options,
    )
    latency.mark(
        "session_started",
        profile=components.profile,
        model=components.model_name,
        interruption_mode=components.interruption_mode,
        response_token_limit=components.response_token_limit,
    )
    publisher.emit(
        "session_started",
        room=ctx.room.name,
        profile=components.profile,
        model=components.model_name,
        interruption_mode=components.interruption_mode,
        response_token_limit=components.response_token_limit,
    )
    # A baseline record, so the UI never has to infer "no video" from silence.
    # Through the state rather than beside it, because the tick below asks
    # "has this phase been published yet?" and a record emitted by hand would
    # not count as an answer.
    visual.mark_published("idle")

    # Keep both readers current between events: the app, which wants a status
    # line, and the model, which wants the one line in her instructions that
    # says whether the picture in front of her is real.
    #
    # `pending_phase_change` rather than a local copy of the phase, because
    # every phase caused by an event has already published itself and a second
    # record for it is noise. What is left is precisely the transition with no
    # event: frames going quiet on a share that is still up.
    last_phase = visual.phase()
    logger.info("[vision] sight state: can_see=%s permission=%s phase=%s", visual.can_see, visual.permission, last_phase)

    async def vision_state_tick() -> None:
        nonlocal last_phase
        while True:
            await asyncio.sleep(VISION_SYNC_SECONDS)
            if visual.pending_phase_change():
                last_phase = visual.phase()
                visual.mark_published("phase")
                logger.info("[vision] phase -> %s", last_phase)
            await agent.sync_vision_instructions()

    vision_tick = asyncio.create_task(vision_state_tick())

    # Hold the shared HTTP client for the lifetime of this job, so every tool
    # call reuses one SSL context and one connection pool.
    await acquire_http_client()

    latency.mark("greeting_requested")
    await session.generate_reply(
        instructions="""
Introduce yourself as Lumine.
Greet the user briefly and ask how you can help.
"""
    )
    latency.mark("greeting_requested_done")

    # There is deliberately **no WhatsApp pre-warm here any more.**
    #
    # It was a real latency win -- starting Chromium and loading WhatsApp Web is
    # several seconds, paid during the greeting instead of during the user's first
    # request -- and it was removed because it was *racing the tool it was
    # supposed to be helping*, in three ways that only show up in a live session:
    #
    #   - **It is fire-and-forget on a shared singleton.** `prewarm_whatsapp` and
    #     the messaging tools both drive one `_Browser`. The task is never awaited,
    #     so a request arriving while the warm-up is still navigating finds a tab
    #     that exists and is not yet usable -- and the symptom is a send that
    #     "searches for the contact but can't click the result", retried until it
    #     happens to win the race. Several attempts is what losing a race looks
    #     like.
    #   - **It opened tabs nobody asked for.** One per session, created before the
    #     user had said anything, which is how several copies of WhatsApp came to
    #     be open at once and the search ran in one while the press landed in
    #     another.
    #   - **The cost was paid by machines that never send a message.**
    #
    # `whatsapp_page()` already waits for the page properly -- `state()` polls for
    # up to 90 seconds and reports `loading` as its own answer rather than
    # claiming the account is signed out. So the first request is slower by a few
    # seconds and is *correct*, which is the trade this product should be making:
    # a visible pause beats a message sent into a chat that may not be the one.
    logger.info("[WhatsApp] no pre-warm; the first request waits for the page")


options = WorkerOptions(
    entrypoint_fnc=entrypoint,
    agent_name="lumine",
)
server = AgentServer.from_server_options(options)


def on_worker_registered(worker_id, _server_info):
    emit_runtime_event(
        "worker_registered",
        worker_id=worker_id,
        agent_name="lumine",
        registration_ms=round((time.monotonic() - WORKER_STARTED_AT) * 1000),
    )


server.on("worker_registered", on_worker_registered)

if __name__ == "__main__":
    cli.run_app(server)
