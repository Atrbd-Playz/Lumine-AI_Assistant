# Testing `desktop_control`

Mouse and keyboard control is the one capability in Lumine that reaches the
machine outside the app, so it needs more than "the unit tests pass". This is
how to check it actually works on your machine, and how to read what it tells
you when it does not.

Three tools, in the order you should reach for them:

| Tool | Answers | Touches your machine? |
| --- | --- | --- |
| `agent/desktop_probe.py` | What does the current window expose? | **No** |
| `agent/desktop_smoke.py` | Does the whole thing work end to end? | Only at `--stage click` and beyond |
| `agent/tools/desktop/ops.py` | What is each verb allowed to do? | **No** |

---

## Start here: is the machine even capable?

```powershell
.\.venv\Scripts\python.exe agent\desktop_probe.py
```

Read-only. Safe to run while you are doing something else, and it does not
focus or reveal anything — it reads the tree of the window you already have in
front.

Three things to look for:

```
window      Lumine
process     lumine.exe
dpi aligned True
elements    8

  #   role          name
  1   field        (no name)
  2   tab          Lumine - Memory usage - 141 MB
```

- **`dpi aligned True`** — the one that silently ruins everything if it is
  false. A process that is not DPI-aware reads window rectangles in virtualized
  coordinates and sends clicks in physical ones, so every click lands at the
  scale factor's distance from where it was aimed. Nothing errors. If this is
  `False`, set your display to 100% scaling and it usually fixes itself.
- **`elements 3`** on a full application means the content is in a child window
  and is not being walked. Modern Electron and WinUI apps put their content in
  `Chrome_RenderWidgetHostHWND`; the tree hanging off the top-level handle is
  the title bar and nothing else. Seeing only `Minimize` / `Maximize` / `Close`
  on a busy window is the signature of that.
- **`elements 0`** is not a bug. A canvas, a game, or a terminal exposes no
  names, and Lumine can still act on a coordinate — the weakest tier, which
  always asks first.

Other flags:

```powershell
.\.venv\Scripts\python.exe agent\desktop_probe.py --json     # machine-readable
.\.venv\Scripts\python.exe agent\desktop_probe.py --capability
```

---

## Then: does it work? The smoke test

```powershell
.\.venv\Scripts\python.exe agent\desktop_smoke.py
```

Read-only, and it is the default for a reason. Add stages as you are willing to
let it move your mouse:

```powershell
.\.venv\Scripts\python.exe agent\desktop_smoke.py --stage observe   # still read-only
.\.venv\Scripts\python.exe agent\desktop_smoke.py --stage refuse    # still read-only
.\.venv\Scripts\python.exe agent\desktop_smoke.py --stage click     # MOVES YOUR MOUSE
.\.venv\Scripts\python.exe agent\desktop_smoke.py --stage full      # types and scrolls too
```

`--stage` is ordered, so each stage includes everything before it.

### What each stage covers, and why the order is this

| Stage | Touches input? | What it proves |
| --- | --- | --- |
| `read` | No | Platform, driver, DPI, the switch, the five verbs, the exclusion, screen geometry |
| `observe` | No | A real window can be read, names found, geometry sane |
| `refuse` | **No** | Every safety property holds, and *nothing was injected* |
| `click` | Yes | One real click lands where it was aimed |
| `type` | Yes | Real typing, a real modifier chord, a real clear |

`--stage full` is an alias for `type`, the last stage.

The order is deliberate and it is the important part. **`refuse` comes before
anything that moves the mouse.** A feature that works but sends a WhatsApp
message without asking has traded the entire permission model for convenience,
and that regression matters more than any successful click. The `refuse` stage
asserts:

- a browser window is **driven**, with a note that `web_control` is steadier
- a WhatsApp window is **driven**, with a note about `send_whatsapp`
- her **own** window is still refused — an agent that can click anything could
  otherwise click the Tools page and switch itself off mid-turn
- `msedgewebview2.exe` is **not** treated as a browser — this is the bug that
  shipped and would have reported every Tauri and WinUI app as one
- a password field is refused, and for the *right reason* (it checks the message
  says "password", not merely that something failed)
- a control that has moved is refused
- a control that stayed put is **confirmed** — so the check cannot pass by
  refusing everything
- `Win+L` is refused
- turning the switch **on** silences `browse` and `web_control`, and turning it
  **off** gives them straight back
- **no input was injected by any of the above**

That last one is the load-bearing assertion: the refusals are checked with a
recording stub in place of the real engine, so a leak anywhere above shows up as
a failure rather than as a surprise.

### The stages that move your mouse

`click` opens Notepad, reads it, and clicks a control **it found itself** — a
menu or tab, which is the safest thing to open because it closes again and
cannot alter anything. Then it types into the throwaway window, presses
`Ctrl+A`, clears it, and closes Notepad.

It never opens a browser or WhatsApp, never presses Enter (in a text field Enter
is a submit, which is not a place to discover that), and never types into
anything it did not open.

`--keep-open` leaves the Notepad window up so you can look at it.

### Reading a failure

The `detail` column is the point. A smoke test that says `FAIL: typing` has
cost ten minutes; one that says *"the field never took focus, so the keystrokes
went nowhere"* has cost thirty seconds.

Common ones:

| Detail | Means |
| --- | --- |
| `clicks may land at the scale factor's distance` | DPI awareness failed. Display scaling at 100%. |
| `nothing exposes a name` | Expected on a canvas or terminal. Not a bug. |
| `redirected to web_control` on an app you wanted | The app *is* a browser. Use `web_control`. |
| `Windows refused that input` | You ran Lumine **as administrator** and the target is not. UIPI blocks injection downward. Run both at the same level. |
| `I couldn't bring that window to the front` | Something stole focus mid-run. Retry. |

---

## Turning it on

The tool ships **off**, deliberately — it can click any pixel and press any
named key.

**The Tools page card is the switch.** Desktop → "Control the mouse and
keyboard" → on.

It was dead in **two** ways, and either one alone would have looked like a
broken feature:

1. It wrote `tools.desktop_control` while the tool read `desktop.input` — two
   switches, and the visible one did nothing.
2. `save_config` cleared the config cache but **not the tool catalog**, and the
   Tools page is rendered *from the tool catalog*. The write succeeded and the
   next read handed back the catalog from before it — so flipping a switch
   appeared to revert on its own, and a page reload brought the stale answer
   back.

**Turning it on also switches off `browse` and `web_control`.** Deliberate:
`desktop_control` can drive a browser window itself, and three tools that can
all click the same page is how the model ends up in the weakest one. Turn it
back off and both come straight back — nothing is disabled permanently, and
each card's reason names the switch responsible.

```powershell
# the browser tools are silenced while it is on, and restored when it is off
.\.venv\Scripts\python.exe agent\desktop_smoke.py --stage read
```

`desktop.input` and `LUMINE_ENABLE_DESKTOP_INPUT` still exist, underneath it,
for a machine configured by hand:

```jsonc
// lumine.config.json — an explicit answer, and it wins over the card
{ "desktop": { "input": "on" } }
```

```powershell
# agent/.env, or the environment
LUMINE_ENABLE_DESKTOP_INPUT=true
```

Precedence, most explicit first: `desktop.input` → the card → the environment
→ **off**. An unrecognised value reads as **off** at every level — the opposite
of a plain `tools.<id>` switch, because that one reduces authority and this one
grants it.

Check:

```powershell
.\.venv\Scripts\python.exe agent\desktop_probe.py | Select-String "switched on"
```

The switch takes effect on the **next call** — `get_tools()` runs per dispatched
room, so no worker restart.

---

## Using it live

Three rules that are enforced in code, so you do not have to remember them:

1. **Always observe first.** `observe` lists what is clickable by number, and
   any act on a number from a screen that has since moved is **refused**. If
   Lumine says the screen moved, that is a refusal, not an obstacle to retry
   around — she should observe again.
2. **Only two things ever ask.** A control whose own label reads as a
   submission or a deletion — `Send`, `Pay`, `Delete`, `Empty trash` — and
   `press`, because Enter *is* a submit. Everything else runs. She never types
   into a password field, and never drives her own window.
3. **The Tools page card, the approval card, and the on-screen panel all agree.**
   The desktop panel in the corner shows `screen #N` — that is the generation
   she is reasoning about, and it is the one the approval card is asking you to
   agree to.

Watch the panel, not the mouse. When she is about to do something irreversible
the approval card names the control in full — `2. button "Delete all"` — which
is the moment to say something.

### Talking to it

```
"Lumine, what can you click on right now?"     -> observe
"Lumine, click 4."                             -> click
"Lumine, move to 12."                          -> move
"Lumine, type hello."                          -> type
"Lumine, press ctrl+s."                        -> press
"Lumine, click Save in Notepad."               <- naming the window is reliable
```

**Name the window when several are open.** Lumine reads the most *useful*
window, not the focused one — because while you are talking to her, the focused
window is her. With one real application open she will find it every time.

---

## Reading the operation table

The classes and the refusal rules are one table, and reading it answers "what
can this thing do to my machine" without running anything:

```powershell
.\.venv\Scripts\python.exe -c "from agent.tools.desktop import ops; [print(f'{o.verb:<13} {o.action_class:<12} {o.verify}') for o in ops.OPERATIONS]"
```

| Verb | Class | What it can reach |
| --- | --- | --- |
| `observe` | `read` | Anything. Never asks. |
| `move` | `act` | The pointer. Cannot press anything. |
| `click` | `act` | An ordinary control. Asks if the name says otherwise. |
| `type` | `act` | Whatever has focus. **Refuses a password field outright.** |
| `press` | `sensitive` | Always asks. Enter is a submit. |

Five, and that is the point. It was nine — `double_click`, `right_click`,
`drag` and `scroll` were four ways to ask for one of two intentions, and every
extra verb is a choice the model can lose on a voice turn's budget of attention.
The mechanics are still in `input.py`; they are simply not verbs.

The class is computed from the **control's own name**, and only ever raised by
it. Clicking `Next` runs; clicking `Send` asks; clicking `Delete` asks hardest.
A control called `Sender` is a column heading and does not ask.

**No window is off limits** except Lumine's own. A browser or WhatsApp window
is driven like any other — the approval card just adds a note that
`web_control` would be steadier there. That used to be a hard refusal, and it
made the tool useless for anything the dedicated tools could not phrase for.

---

## If something looks wrong

**The click did nothing.** Check `dpi aligned` from the probe first. Then check
whether the control was *disabled* — that is refused with its own sentence, not
silently ignored.

**She says she cannot find the window.** `--stage observe` prints how many
windows matched. Two Notepads are two documents and she will not guess; you will
be asked which one.

**She says she can only see a blank page.** That was a real bug, now fixed:
she used to read whichever window had focus, which while you are talking to
her is *Lumine*, and which an always-on-top clock widget can steal without you
doing anything — and a clock widget exposes zero controls. She now skips any
window with nothing in it. If you still see it, run `desktop_probe.py`, and if
the window you meant genuinely exposes no names (a canvas, a terminal, a game)
say so — she can still act on a coordinate.

**She refuses an app you expected her to drive.** She should not — no window is
off limits except her own. If she refuses her own window, that is correct: an
agent that can click anything could otherwise click the Tools page and switch
itself off.

**Nothing happens when she tries to type.** Windows refuses to hand the
foreground to a background process while you are typing. That is a focus lock,
not a broken feature, and `uia.bring_to_front` works around it with
`AttachThreadInput` — the same technique `windows_control` uses for focusing.
If you are driving the worker from a terminal, give it a moment with your hands
off the keyboard.

**The harness is cooling down.** A UIA call overran 25 seconds, so everything
refuses for 60. `python agent/whatsapp_probe.py` is the same worker; a wedged
WhatsApp cools down desktop control too, which is correct — the worker is one
thread.

**Windows refuses input.** You are running Lumine elevated and the target is
not, or the other way round. UIPI blocks injection downward and this is
reported rather than ignored, because an unchecked `SendInput` would report a
click that never happened.