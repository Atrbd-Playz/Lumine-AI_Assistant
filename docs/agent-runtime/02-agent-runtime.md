# Phase 2 — `AgentRuntime` façade

- **Status:** specified, **not approved.** Blocked on Phase 1.

## 1. The problem

The agent lifecycle has **no single owner**. It is assembled across three layers:

| Layer | Owns today |
|---|---|
| the model | decides *what* the user wants, and when a task is needed |
| `tools/task_runner.py` | builds the plan, owns the run, holds the session registry, defines the step implementations |
| `runtime/task_engine.py` | advances the task: dependencies, retries, cancellation |
| `agent.py` | wires it to LiveKit, the room, the publisher, the gate |

Each layer holds state the others cannot see. `run_task` is simultaneously a tool
call, a planner, an executor and a session-scoped registry. That is why "the
lifecycle" has no testable definition, and why the request that opened this track
asked for an `AgentRuntime`.

## 2. The slice

One owner for the lifecycle, and nothing else.

```python
class AgentRuntime:
    def __init__(self, *, registry, planner, gate=None, events=None): ...

    async def handle(self, request: str) -> Outcome: ...
    async def run(self, task: Task) -> Task: ...
    async def cancel(self, task_id: str) -> bool: ...
```

## 3. Do

- New `runtime/agent_runtime.py`.
- `tools/task_runner.py` becomes a **caller**, not the owner. `execute()`'s
  signature and behaviour are preserved so existing callers are untouched.
- Gate, planner, executor and capability registry are imported one-way or
  injected. The runtime knows nothing about Playwright, UI Automation, or
  `subprocess`.
- Promote the manual smoke scenarios to automated tests.

## 4. Do not

- Add a capability, a tool, a permission, or a state file.
- Import LiveKit. It must run with `session=None`.
- Move persona or personality code. **Expression is a separate layer** — the
  runtime controls execution, the personality layer controls expression.
- Move the step implementations out of `task_runner.py` in this phase. They are
  ~600 lines of accumulated measured behaviour; moving them changes the failure
  surface for no gain.

## 5. Complexity routing

What the user speaks determines whether a task exists at all. This must stay
**cheap and deterministic** — a classifier that costs a model call to answer
"is this simple?" is worse than the ceremony it avoids.

| Shape | Example | Behaviour |
|---|---|---|
| **simple** | "What's the weather?" | one tool call, no task |
| **medium** | "Open Notepad." | small plan, execute, verify |
| **complex** | "Open Chrome, search YouTube for X, play it, start a focus timer." | persistent task with steps |

The rule of thumb is the one `tools_policy.py` already teaches: **one job is one
tool.** Two or more *dependent* steps is a task. `task_runner.MAX_STEPS = 8`
already encodes the upper bound, and its comment is the reason:

> *"A voice request that needs more is a request whose intent was not understood,
> and continuing would spend the user's time discovering that."*

## 6. Tests

`agent/tests/test_agent_runtime.py`:

- complexity routing: simple / medium / complex
- a single-step request creates no `Task`
- the runtime runs with `session=None`
- a cancelled task cannot be resumed by a later call
- a task with no runnable steps reports a **deadlock**, not a loop
- `handle()` returns an `Outcome` that names what was *uncertain*

## 7. Exit criteria

- `2232+ tests pass`
- no observable behaviour change
- `agent.py` loses orchestration responsibility and gains none
- a reader can find the whole lifecycle in one file
