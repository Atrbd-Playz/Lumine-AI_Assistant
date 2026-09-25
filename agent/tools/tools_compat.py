"""LiveKit tool imports that degrade gracefully, like agent.py does.

agent.py tolerates a missing livekit-agents install so the module can still be
imported for tests and tooling. Tools follow the same rule: they only need to
import cleanly in order to be listed, and they only execute inside a running
session where livekit-agents is guaranteed to be present.
"""

try:
    from livekit.agents import RunContext, function_tool
    from livekit.agents.llm import ToolError
except ImportError:  # pragma: no cover - only reached without livekit-agents
    RunContext = object

    class ToolError(Exception):
        """Stand-in for livekit.agents.llm.ToolError."""

    def function_tool(f=None, **_kwargs):
        if f is None:
            return lambda fn: fn
        return f
