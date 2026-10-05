<div align="center">
  <img src="lumine-ui/public/Lumine-GitHub-animated.gif" width="128" alt="Lumine">
  <h1>Lumine</h1>
  <p><strong>A local-first AI companion that lives on your desktop.</strong><br>
  Voice, memory and tools run on your own machine.</p>
</div>

<div align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-D22128?style=flat-square" alt="Apache 2.0 License"></a>
  <img src="https://img.shields.io/badge/Tauri-2-24C8DB?style=flat-square" alt="Tauri 2">
  <img src="https://img.shields.io/badge/React-19-61DAFB?style=flat-square" alt="React 19">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square" alt="Python 3.10 or newer">
  <img src="https://img.shields.io/badge/LiveKit-Agents-1.8-0B0B0B?style=flat-square" alt="LiveKit Agents 1.8">
</div>

---

Talk to it. It hears you, thinks, and answers out loud — and when a request needs
hands, it reaches for them: a browser it drives, a program it opens, a message it
drafts for your approval.

Everything runs on your own machine. The desktop app is a Tauri shell over a
persistent Python LiveKit worker. There is no cloud backend of Lumine's own, and
no API key ever reaches the webview.

This is a working voice session with a real presentation around it — not a
packaged product. See [Project Status](#project-status) before you install
anything.

## Contents

- [What Lumine Is](#what-lumine-is)
- [Screenshots](#screenshots)
- [Features](#features)
- [Architecture](#architecture)
- [The Tool Registry](#the-tool-registry)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [Testing](#testing)
- [Documentation](#documentation)
- [Project Status](#project-status)
- [Contributing](#contributing)
- [License](#license)
- [Author](#author)

## What Lumine Is

Lumine is three processes and one conversation.

A **Tauri** desktop shell in **Rust** owns the window, the OS keyring and the
lifecycle of a **Python** worker. That worker is a persistent LiveKit
`AgentServer`; each conversation is a fresh room with its own `AgentSession`
built from the profile you configured. The **React** webview never talks to
LiveKit's secrets — it asks Rust for a token and consumes high-level state
events, so the UI holds no provider knowledge at all.

The default voice path is a single native-audio **Gemini Live** realtime model
that hears, thinks and speaks in one turn. A Groq + Silero + Cartesia cascade is
preserved and still selectable.

## Screenshots

Every shot below is the running app, in the two palettes this project ships
with its own design: a sage-green light theme and its dark counterpart.

### The companion

<table>
<tr>
<td width="50%"><img src="docs/images/home-light.png" alt="Lumine home in the light theme"></td>
<td width="50%"><img src="docs/images/home-dark.png" alt="Lumine home in the dark theme"></td>
</tr>
<tr>
<td><sub>Presence, call bar, weather, news, notes, transcript</sub></td>
<td><sub>The same room in dark</sub></td>
</tr>
</table>

### What she can reach

<table>
<tr>
<td width="50%"><img src="docs/images/tools-light.png" alt="The Tools page"></td>
<td width="50%"><img src="docs/images/tools-dark.png" alt="The Tools page in dark"></td>
</tr>
<tr>
<td><sub>All 17 tools, five categories, one switch each</sub></td>
<td><sub>16 of 17 available — desktop control ships off</sub></td>
</tr>
</table>

### What the worker reported

<table>
<tr>
<td width="50%"><img src="docs/images/activity-light.png" alt="The Activity feed"></td>
<td width="50%"><img src="docs/images/activity-dark.png" alt="The Activity feed in dark"></td>
</tr>
<tr>
<td><sub>Latency, problems and tool calls, persisted across restarts</sub></td>
<td><sub>Filterable by kind, counted per session</sub></td>
</tr>
</table>

### The face

<table>
<tr>
<td width="50%"><img src="docs/images/avatar-lab-light.png" alt="Avatar Lab"></td>
<td width="50%"><img src="docs/images/avatar-lab-dark.png" alt="Avatar Lab in dark"></td>
</tr>
<tr>
<td><sub>27 emotions, eye construction, activity — driven by the same engine the home screen runs</sub></td>
<td><sub>&nbsp;</sub></td>
</tr>
</table>

### The AI Control Center

<table>
<tr>
<td width="50%"><img src="docs/images/settings-voice-light.png" alt="Settings, Voice page"></td>
<td width="50%"><img src="docs/images/settings-voice-dark.png" alt="Settings, Voice page in dark"></td>
</tr>
<tr>
<td><sub>Which profile speaks, and the stack it resolves to</sub></td>
<td><sub>&nbsp;</sub></td>
</tr>
</table>

<table>
<tr>
<td width="33%"><img src="docs/images/settings-models-light.png" alt="Settings, Models page"></td>
<td width="33%"><img src="docs/images/providers-light.png" alt="Settings, Providers page"></td>
<td width="33%"><img src="docs/images/permissions-light.png" alt="Settings, Permissions page"></td>
</tr>
<tr>
<td><sub>Stages and models</sub></td>
<td><sub>Credential slots in the OS keyring</sub></td>
<td><sub>The one folder she may read</sub></td>
</tr>
</table>

## Features

**Voice and presence**
- One persistent worker, one fresh session per conversation — the worker outlives the call.
- Native-audio realtime by default; the legacy VAD/STT/LLM/TTS cascade is still selectable.
- Camera and screen input for models that declare those modalities, and a refusal with the real reason when the platform cannot.
- An SVG avatar with a canonical 27-emotion vocabulary plus an `idle` state, an activity layer, cursor gaze, and barge-in.

**Seventeen tools, five categories**
- *Web* — weather, news, search, plus two page drivers: one addressed by reference, one by what controls are called.
- *Internal* — her own guidance, read on demand rather than preloaded into every turn.
- *Media* — one folder you choose; a subfolder is a playlist.
- *Messaging* — WhatsApp: chats, unread, messages, search, contact details, chat management, account verbs, and sending.
- *Desktop* — find and open programs, arrange windows, drive the mouse and keyboard, and run a multi-step request end to end.

**Desktop control**
- Mouse and keyboard for any application without a dedicated tool, through Windows UI Automation. Ships **off**; the Tools page card is the switch.
- It refuses exactly two things: Lumine's own window, and password fields. Everything else runs.

**Messaging that asks first**
- A send types the text into the chat and reads it back out of the composer *before* the approval question, so what you approve is on your own screen.
- Reading someone's messages is a separate tool that asks once per conversation and says out loud that the grant lasts the rest of it.

**Boundaries rather than warnings**
- A model-supplied path is resolved first and compared second, so `..`, an absolute path and a symlink out all land in the same refusal.
- The music folder is a boundary: no verb moves, renames or deletes anything.
- Every side-effecting action passes one gate that speaks the question, shows a card carrying the same sentence, and appends a SHA-256-digested record to a local audit log.

## Architecture

```text
React 19 + Vite  (webview)
  │   Tauri commands + events — no LiveKit secret ever crosses this line
  ▼
Rust  (src-tauri)   agent_manager · credentials · settings_store · media · focus
  │   spawns the worker, injects keyring values one variable per slot,
  │   re-emits its LUMINE_EVENT stdout stream as Tauri events
  ▼
Python  (agent/)   persistent AgentServer "lumine" · one AgentSession per room
  │
  ▼
Gemini Live realtime      —or—      Groq + Silero + Cartesia
```

| | Tauri shell | Python worker |
|---|---|---|
| Language | Rust | Python 3.10+ |
| Owns | window, keyring, worker lifecycle, settings document | the voice session, the tools, the browser |
| Lifetime | as long as the app | outlives the app's conversations |
| Talks to | the OS, the webview, the worker | LiveKit, providers, the user's machine |
| Knows nothing about | Groq, Cartesia, VAD | React, the window, the theme |

The worker is **persistent**, not per-session: one process, one registration,
many rooms. That is why a profile change takes effect on the next call without a
restart, and why "the worker is running" is not the same statement as "Lumine is
listening" — the UI reports those separately.

**Stack:** Tauri 2 · React 19 · TypeScript 5.8 · Vite 7 · Tailwind CSS 4 ·
Rust 2021 · Python 3.10–3.14 · LiveKit Agents 1.8 (Google, Groq, OpenAI, Silero,
Cartesia plugins) · Playwright · UI Automation.

## The Tool Registry

`agent/tools/tools_registry.py` is the source of truth, and `agent/tool_catalog.py`
prints the live list. Every tool has an action class — `read`, `act`, `sensitive`
or `destructive` — that the saved config can only raise, never lower.

| Tool | Category | What it does | Gate |
| --- | --- | --- | --- |
| `get_weather` | Web | Current conditions and a two-day forecast | auto |
| `get_news` | Web | Recent headlines on a topic, or the top stories | auto |
| `search_web` | Web | Looks something up; returns result titles only | auto |
| `browse` | Web | Drives a page by reference — open, observe, act, back, forward, scroll, tab, close | ask |
| `web_control` | Web | Drives a page by what its controls are called | ask |
| `recall_persona` | Internal | Her own guidance for a delicate moment | auto |
| `play_media` | Media | Finds and plays from the one folder you chose | auto |
| `whatsapp_read` | Messaging | Chats, unread, a chat's messages, message search, a contact | ask |
| `whatsapp_chats` | Messaging | Mark read, pin, mute, archive, star, delete, block | ask |
| `whatsapp_account` | Messaging | Presence, linked devices, about, unlink, call | ask |
| `send_whatsapp` | Messaging | Sends, only after you approve the exact text | ask |
| `find_app` | Desktop | Names of installed programs. Opens nothing | auto |
| `open_app` | Desktop | Starts an installed application by name | auto |
| `manage_window` | Desktop | Lists, focuses, resizes and closes windows | ask |
| `desktop_control` | Desktop | Mouse and keyboard in any application — **ships off** | ask |
| `run_task` | Desktop | A multi-step request end to end, checking each step worked | ask |
| `cancel_task` | Desktop | Stops the current task, including steps not yet started | auto |

## Getting Started

### Prerequisites

| | |
| --- | --- |
| **OS** | Windows (verified). The code avoids Windows-only assumptions, but no other platform has been exercised. |
| **Python** | 3.10 – 3.14. This is `livekit-agents` 1.8.3's own supported range, not a preference. |
| **Node** | 20 or newer, for Vite 7 and the Tauri CLI. |
| **Rust** | Stable toolchain. On Windows, Visual Studio Build Tools with the C++ workload. |
| **Accounts** | A LiveKit project (cloud or self-hosted) and a Google AI Studio API key. |

### 1. Clone and set up Python

```powershell
git clone https://github.com/Atrbd-Playz/Lumine-AI_Assistant.git
cd Lumine-AI_Assistant

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r agent\requirements.txt
```

### 2. Install the browser engine

`playwright` ships a driver, not a browser. The tools report this sentence rather
than failing to start:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

### 3. Add four credentials

The default pipeline is `gemini_live`, so a working install needs exactly four
values from two providers:

| Variable | Where |
| --- | --- |
| `LIVEKIT_URL` | <https://cloud.livekit.io/> — `wss://…` for cloud |
| `LIVEKIT_API_KEY` | Settings → API Keys, same project |
| `LIVEKIT_API_SECRET` | Settings → API Keys, same project |
| `GOOGLE_API_KEY` | <https://aistudio.google.com/apikey> |

**Prefer the app.** Settings → AI → Providers writes each one into the OS
keyring — Windows Credential Manager, macOS Keychain, Linux Secret Service — and
the Rust layer injects them into the worker process one variable per slot. That
path never writes a secret to disk in this repository.

To use files instead, copy the examples and fill them in:

```powershell
Copy-Item agent\.env.example agent\.env
Copy-Item lumine-ui\.env.example lumine-ui\.env
```

`agent/.env.example` ships with **every credential line empty**, and that is
deliberate rather than tidiness. Every credential check in the product treats "the
variable holds a non-empty string" as "you have this key", because telling a
placeholder from a real key would cost a billable request. A shipped
`your-…` placeholder therefore reads as a working credential: the setup gate says
you are ready and the first turn is a `401`. An empty value is a real absence, and
it leaves the line there to paste into.

### 4. Run it

```powershell
Push-Location lumine-ui
npm ci
npm run tauri dev
Pop-Location
```

That is the whole loop. The app starts the Python worker itself, waits until it
registers with LiveKit, and injects the keyring credentials — so voice does not
depend on a process you remembered to start.

### If something looks wrong

```powershell
# Which config file the worker is actually reading, and what it searched
.\.venv\Scripts\python.exe agent\validate_config.py --describe
```

- **Vite is on port 5173, strictly.** That is not the Tauri-conventional `1420`
  because Hyper-V and WSL reserve `1351–1450` on Windows, and binding an excluded
  port fails with `EACCES` rather than `EADDRINUSE`. Do not start a second Vite
  server there.
- **Stop any standalone worker before testing settings changes.** LiveKit
  dispatches a room to exactly one agent, so a worker left registered as `lumine`
  absorbs the next dispatch — which looks precisely like a broken worker. For
  debugging the worker on its own, run
  `.\.venv\Scripts\python.exe agent\agent.py dev` in a second terminal.

## Configuration

Precedence is **saved `lumine.config.json` → `agent/.env` → catalog defaults**,
resolved per key. The desktop app writes the document; a worker started outside
Tauri still finds it.

The settings that change what Lumine can do:

| Key | Default | What it decides |
| --- | --- | --- |
| `tools.<id>` | `on` | One switch per tool, written by the Tools page |
| `desktop.input` | `off` | Whether she may move the mouse and press keys |
| `messaging.whatsapp.mode` | `deeplink` | `off`, `deeplink` (you press send), or `automation` |
| `messaging.whatsapp.history` | `ask` | `off`, `ask` (once per conversation), or `auto` |
| `media.enabled` | `on` | The music library capability |
| `media.library` | — | The one folder she may read |
| `desktop.browsers` | asks | Whether "open a website" means your browser or hers |

Two of those fail in **opposite** directions on purpose. `tools.<id>` only ever
reduces authority, so an unrecognised value reads as `on`. `desktop.input` and
`media.enabled` *grant* a capability, so an unrecognised value there reads as
`off` and is reported as an error rather than a warning — falling through on a
misspelling there would mean `LUMINE_ENABLE_MEDIA=enabled` turned it on, the exact
inverse of what was meant.

`agent/settings/providers.py` is the executable authority and
`agent/settings/validation.py` is the schema. `LUMINE_PIPELINE=legacy_cascade` in
`agent/.env` switches to the Groq/Cartesia stack if you would rather not use
Gemini.

## Testing

```powershell
# Python — 66 test modules
.\.venv\Scripts\python.exe -m unittest discover -s agent\tests -p "test_*.py"

# A single test
.\.venv\Scripts\python.exe -m unittest agent.tests.test_emotion_contract.EmotionContractTests.test_normalize_emotion_keeps_canonical_values

# Frontend typecheck and build
Push-Location lumine-ui; npm run build; Pop-Location

# Frontend contract checks
Push-Location lumine-ui; npm run check; Pop-Location

# Rust
Push-Location lumine-ui\src-tauri; cargo check; Pop-Location
```

The Python suite exercises parsing, permissions and formatting locally. It does
not depend on live providers, so it is safe to run offline.

Live probes, for when you need to look at the real thing:

| Command | What it tells you |
| --- | --- |
| `python agent\desktop_probe.py` | What the current window exposes. Read-only |
| `python agent\desktop_smoke.py` | End-to-end checks, staged so nothing moves until asked |
| `python agent\whatsapp_probe.py` | Which WhatsApp selectors are live in the current build |
| `python agent\tool_catalog.py --check` | Fails on a registered tool with no card |
| `python agent\provider_probe.py` | One cheap authenticated request per provider |
| `python agent\validate_config.py --describe` | Which config file won, and what was searched |

## Documentation

| Document | What is in it |
| --- | --- |
| [`docs/ai-control-center.md`](docs/ai-control-center.md) | The AI Control Center decision record — credential slots, the catalog, why they are shaped that way |
| [`docs/voice-architecture.md`](docs/voice-architecture.md) | The process model and the full session lifecycle |
| [`docs/livekit-integration.md`](docs/livekit-integration.md) | LiveKit specifics and the pieces that surprised us |
| [`docs/desktop-control-testing.md`](docs/desktop-control-testing.md) | How to test desktop control, and how to read a failure rather than only that one happened |
| [`docs/focus-workspace.md`](docs/focus-workspace.md) | The Focus workspace data layer and its security boundaries |
| [`AGENTS.md`](AGENTS.md) | Repository instructions and the load-bearing invariants |
| [`agent/AGENTS.md`](agent/AGENTS.md) · [`lumine-ui/AGENTS.md`](lumine-ui/AGENTS.md) | Area-specific background for each half |

## Project Status

**Alpha, and honest about it.** Built and working:

- [x] Persistent LiveKit worker with a per-room session, and a readiness gate that blocks until it registers
- [x] Seventeen gated tools across five categories
- [x] Two page drivers, WhatsApp read / manage / send, desktop control, and a music library that queues
- [x] AI Control Center: provider catalog, per-variable keyring slots, live probes, fail-closed setup gate
- [x] An activity feed that survives a reload, and an appearance system with importable palettes
- [x] 66 Python test modules and six frontend contract checks

Not built:

- [ ] **There is no installer.** `bundle.active` is `false`, so `npm run tauri build`
      writes `src-tauri/target/release/lumine.exe` and stops there.
- [ ] **The worker is not packaged as a sidecar.** A working install needs Python
      on the machine. That is the single biggest thing between this and a
      download.
- [ ] **No tray, no minimize-to-tray, no wake word.** No typed sleep/wake command
      either; LiveKit dispatch metadata plus the event stream are the seam.
- [ ] **No crash recovery or auto-restart.**
- [ ] **Windows only in practice.** Cross-platform by intent, unverified anywhere else.
- [ ] **The Focus workspace is a work in progress.**
- [ ] **No video output.** Lumine does not send frames back; the SVG avatar is the answer for now.

There is no CI workflow yet, which is why the badges above say nothing about
build status. Run the checks locally before opening a pull request.

## Contributing

Run the Python suite, `npm run check` and `cargo check` first. There is no CI to
do it for you yet.

Most of the value of this repository is in `AGENTS.md`, which records *why* each
piece is shaped the way it is — usually because the obvious version was tried and
measured first. A few invariants that fail quietly when broken:

- **Ship a credential empty, never as a placeholder.** Every presence check in the
  product reads "non-empty" as "you have this key".
- **A tool result is re-sent on every later turn.** Keep it keyed, structured and
  small; the budget is applied to the structure, not to a rendered string.
- **Resolve a model-supplied path before comparing it** to an allowlist, or `..`
  and a symlink out walk straight through.
- **A `useSyncExternalStore` `getSnapshot` must return a cached value.** A selector
  that derives a new object or array will re-render forever and take the page
  down with it.
- **A broken selector must degrade to a refusal**, never to a plausible wrong
  action.

## License

The source code is licensed under the Apache License 2.0 — see [LICENSE](LICENSE).

```text
Copyright 2026 Abdur Rahman Toha

Licensed under the Apache License, Version 2.0 (the "License"); you may not use
this file except in compliance with the License. You may obtain a copy of the
License at

    http://www.apache.org/licenses/LICENSE-2.0
```

The **Lumine name, logo, avatar artwork and written persona are not covered by
the licence** and are not granted for reuse. The licence covers the source code.

## Author

**Abdur Rahman Toha** — [@Atrbd-Playz](https://github.com/Atrbd-Playz)

- Email: [abdurramantoha@gmail.com](mailto:abdurramantoha@gmail.com)
- Issues and bugs: [github.com/Atrbd-Playz/Lumine-AI_Assistant/issues](https://github.com/Atrbd-Playz/Lumine-AI_Assistant/issues)

<div align="center">
  <img src="lumine-ui/public/Lumine-GitHub-animated.gif" width="96" alt="Lumine">
</div>
