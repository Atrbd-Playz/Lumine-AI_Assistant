import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

try:
    from .settings.config_store import (SOURCE_UI, active_profile, config_search_summary, effective_document, resolve_profile)
    from .runtime.context_trim import max_context_items, trim_context
    from .runtime.emotion_contract import build_emotion_event, extract_emotion_sequence, infer_emotion_from_text
    from .runtime.emotion_voice import TurnVoiceControls
    from .runtime.failure_gate import APOLOGY, FailureGate, describe, handle_llm_error
    from .runtime.latency import LatencyTracker
    from .pipeline.pipeline_factory import ConfigurationRejected, build_pipeline, build_resolved
    from .settings.pipeline_config import pipeline_name
    from .runtime.runtime_events import RuntimeEventPublisher, ToolEventBridge, write_event_line
    from .settings.session_preferences import interruption_mode_from_metadata, job_preferences_from_metadata
    from .tools.http_client import acquire as acquire_http_client, release as release_http_client
    from .tools.tools_policy import compose_instructions
    from .tools.tools_registry import get_tools, tool_ids
except ImportError:
    from settings.config_store import (SOURCE_UI, active_profile, config_search_summary, effective_document, resolve_profile)
    from runtime.context_trim import max_context_items, trim_context
    from runtime.emotion_contract import build_emotion_event, extract_emotion_sequence, infer_emotion_from_text
    from runtime.emotion_voice import TurnVoiceControls
    from runtime.failure_gate import APOLOGY, FailureGate, describe, handle_llm_error
    from runtime.latency import LatencyTracker
    from pipeline.pipeline_factory import ConfigurationRejected, build_pipeline, build_resolved
    from settings.pipeline_config import pipeline_name
    from runtime.runtime_events import RuntimeEventPublisher, ToolEventBridge, write_event_line
    from settings.session_preferences import interruption_mode_from_metadata, job_preferences_from_metadata
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

    Failures here are swallowed. A session that cannot speak the apology is still
    a working session, and raising would replace a quiet failure with a crash.
    """
    try:
        from livekit.agents import speech_handle

        handle = speech_handle.SpeechHandle(text=text, source="lumine_error", allow_interruptions=True)
        session.say(handle, chunk_size=120)
    except Exception as exc:  # noqa: BLE001 - never let a courtesy speech break a session
        logger.debug("[LLM] could not speak the failure notice: %s", exc)


class Lumine(Agent):
    def __init__(self, tools=None, profile: str = "gemini_live"):
        super().__init__(
            instructions=build_instructions(profile),
            tools=tools if tools is not None else get_tools(),
        )

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
                publisher.emit(
                    "config_rejected",
                    room=room_name,
                    profile=resolved.profile_id,
                    config_path=config_path_label,
                    diagnostics=[d.to_dict() for d in exc.diagnostics],
                )
                logger.warning(
                    "[Config] saved profile %r was rejected; using the environment instead",
                    resolved.profile_id,
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

    components = await build_pipeline(pipeline_name(), interruption_mode=interruption_mode)
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
    tool_events = ToolEventBridge(publisher)
    # Counts consecutive LLM failures and opens a circuit on a run of them, so a
    # rate-limited session stops trying instead of spending what is left.
    gate = FailureGate()

    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        item = event.item
        # Trim as the conversation grows rather than when something breaks. A
        # context-length error is the provider's 400, and by then the turn has
        # already failed silently from the user's side.
        outcome = trim_context(session)
        if outcome.trimmed:
            publisher.emit(
                "context_trimmed",
                items_before=outcome.items_before,
                items_after=outcome.items_after,
                limit=max_context_items(),
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
        try:
            await session.aclose()
        finally:
            # The last session to finish closes the shared HTTP client.
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
            ctx.shutdown(reason="user left room")

    await session.start(
        room=ctx.room,
        agent=Lumine(tools, profile=components.profile),
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
