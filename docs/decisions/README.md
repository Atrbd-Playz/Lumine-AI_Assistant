# Decision Records

One file per load-bearing choice. Each is short on purpose: **Context** (what made
it a decision at all), **Decision**, **Why not the alternative**, **Consequences**,
and **Revisit when**.

The format is deliberately small because a decision record nobody reads is a
decision record that does not bind anybody. If a decision is superseded, edit the
status and add a line pointing at the record that replaced it — do not delete the
file. "We tried the other thing" is the most useful sentence in this directory.

| # | Record | Status |
| --- | --- | --- |
| [0001](0001-companion-not-application.md) | Companion, not application | Accepted |
| [0002](0002-single-resident-runtime.md) | One resident runtime process, island as its own entry | Proposed |
| [0003](0003-wake-layer-separation.md) | Wake is local, continuous, and separate from the agent | Accepted |
| [0004](0004-wake-not-a-transcript.md) | Wake detection is keyword/phonetic, never a transcript match | Accepted |
| [0005](0005-single-lifecycle-authority.md) | One lifecycle state machine is the only authority | Accepted |
| [0006](0006-island-as-primary-surface.md) | The island is the primary voice interaction surface | Accepted |
| [0007](0007-one-shot-commands.md) | One-shot commands are an acceptance criterion | Accepted |
| [0008](0008-idle-is-not-sleeping.md) | Idle is not sleeping | Accepted |
| [0009](0009-self-audio-protection.md) | Self-audio protection ships before wake does | Accepted |
| [0010](0010-layer-ownership.md) | No new subsystem without a named owner | Accepted |
| [0011](0011-no-overbuild.md) | Build vertically; the roadmap preserves direction, not work | Accepted |
| [0012](0012-process-vs-companion-state.md) | Process state and companion state are different types | Accepted |
| [0013](0013-greeting-conditional.md) | Greeting is conditional on having nothing to do | Accepted |
| [0014](0014-canonical-capability-namespace.md) | Canonical capability ids do not split the model-facing tools | Accepted |

Also see [`../../VISION.md`](../../VISION.md) for the decision rule these records
serve, and [`../../ROADMAP.md`](../../ROADMAP.md) for which phase consumes them.

0011–0014 bind the **runtime** track rather than the companion track. Their
phases, goals and definition of done are in
[`../agent-runtime/`](../agent-runtime/README.md).