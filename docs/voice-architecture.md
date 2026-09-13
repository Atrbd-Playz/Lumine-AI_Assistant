# Lumine voice architecture

## Process model

Lumine keeps the desktop shell and the Python LiveKit worker as separate processes during development.

- Tauri / React owns the desktop UI and session orchestration.
- Python `agent/agent.py` runs as a persistent LiveKit `AgentServer`.
- Each user conversation creates a fresh LiveKit room and a single temporary job.
- The persistent worker remains alive after a session ends.

## Frontend lifecycle

The frontend no longer owns a single shared room object across the lifetime of the app. Instead, each start action creates a new `Room` and a fresh `SessionContext` instance. The current session reference is detached before cleanup begins so an old session cannot mutate the new one.

### Lifecycle steps

1. User clicks Start.
2. `LumineVoiceManager.start()` is called.
3. A new `Room` is created with a unique room name.
4. A token is requested for the user identity.
5. The room connects to LiveKit.
6. The `lumine` agent is explicitly dispatched.
7. The manager watches the agent and audio tracks.
8. User clicks End or a failure occurs.
9. `stop()` clears the active session reference before cleanup.
10. The room is disconnected, mic disabled, listeners removed, and room deleted.
11. The manager returns to idle and is ready for the next session.

## Python AgentServer lifecycle

The Python process uses a persistent `AgentServer` with an explicit `agent_name="lumine"` and a per-job `AgentSession`.

This means:

- the worker remains alive after job completion;
- each room/job has isolated conversation state;
- a new room receives a new dispatch and a new agent job;
- stale state is not retained globally.

## Authentication and dispatch

- The frontend requests a token from Tauri via `get_livekit_token`.
- Tauri invokes the Python helper script to generate a token without exposing the secret in the browser.
- The room is dispatched to the `lumine` agent with `dispatch_agent`.
- Every conversation uses a unique room name such as `lumine-<uuid>`.

## Error handling

The manager treats failed sessions as isolated events. A failing connect or dispatch always leads to cleanup and reset to idle so the next `start()` is a normal first-class session.

## Future readiness

The voice layer is designed as a reusable communication boundary so future modules can call `start()`, `stop()`, or additional voice operations without rewriting the LiveKit session architecture.
