"""Report the message-bubble and send-button selectors against a live chat.

Usage::

    python agent/whatsapp_message_probe.py

Why this exists separately from ``whatsapp_probe.py``
----------------------------------------------------
``whatsapp_probe.py`` reads a *closed* chat, so it cannot see the send button --
that only exists once there is text in the composer -- and it cannot see a
message bubble unless the conversation already has one on screen. Both were
reported missing, and neither is a stale selector.

This one therefore does what the operations do: opens a chat, types a probe
string, and looks again. Then it **clears the composer**, because a draft left
behind is a draft the next send refuses on.

It types into a chat and reads it back. It never presses send.

Output is the two things still unknown:

- which selector finds an outgoing bubble, which is what ``delivery_seen`` uses
  to confirm a message actually left. If this is stale, every send ends in "I
  could not confirm that WhatsApp accepted the message" and the ledger holds for
  five minutes -- which looks exactly like "she cannot send even after I said yes".
- which selector finds the send button, which appears only with text present.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

try:
    from dotenv import load_dotenv

    load_dotenv(AGENT_DIR / ".env")
except ImportError:  # pragma: no cover - dotenv is a hard dependency in practice
    pass

from tools.browser.session import whatsapp_page  # noqa: E402
from tools.messaging import whatsapp_web as web  # noqa: E402

#: Every send-ish control, by icon, test id and label.
_FIND_SEND = """
() => {
  const out = [];
  for (const el of document.querySelectorAll(
    '#main button, #main [role="button"], #main span[data-icon], #main div[data-icon]'
  )) {
    const icon = el.getAttribute('data-icon') || '';
    const testid = el.getAttribute('data-testid') || '';
    const label = el.getAttribute('aria-label') || '';
    if (!/send/i.test(icon + testid + label)) continue;
    const rect = el.getBoundingClientRect();
    out.push({ icon, testid, label, w: Math.round(rect.width), h: Math.round(rect.height) });
  }
  return out;
}
"""

#: Candidate bubbles, found *structurally* rather than by class name: a leaf
#: element with text, sitting in the right-hand column. This works whatever
#: WhatsApp calls its classes today, which is the whole point -- it is how the
#: live selector gets found instead of guessed.
_FIND_BUBBLES = """
() => {
  const main = document.querySelector('#main');
  if (!main) return { error: 'no #main' };
  const rows = Array.from(main.querySelectorAll('[role="row"]'));
  const edges = window.innerWidth * 0.55;
  const right = [];
  const left = [];
  for (const row of rows) {
    const rect = row.getBoundingClientRect();
    const text = (row.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!text || text.length > 300) continue;
    const entry = {
      cls: typeof row.className === 'string' ? row.className.trim().slice(0, 60) : '',
      data_id: row.getAttribute('data-id') || '',
      testid: row.getAttribute('data-testid') || '',
      role: row.getAttribute('role') || '',
      text: text.slice(0, 34)
    };
    if (rect.right > edges) right.push(entry); else left.push(entry);
  }
  // The class names of the message containers themselves, which is where the
  // answer to "what does this build call an outgoing bubble" actually lives.
  const classes = [];
  for (const el of main.querySelectorAll('div')) {
    if (typeof el.className !== 'string' || !el.className) continue;
    for (const name of el.className.trim().split(/\\s+/)) {
      if (/message|row|incoming|outgoing/i.test(name) && classes.indexOf(name) === -1) {
        classes.push(name);
      }
    }
  }
  return {
    rows: rows.length,
    right_aligned: right.slice(-4),
    left_aligned: left.slice(-4),
    message_classes: classes.slice(0, 20)
  };
}
"""


async def main() -> int:
    try:
        page = await whatsapp_page()
    except Exception as exc:  # noqa: BLE001 - a CLI reports, it does not traceback
        print(f"could not open WhatsApp: {exc}")
        return 1

    token = await web.state()
    print(f"state: {token}")
    if token != "ready":
        print("WhatsApp is not ready. Re-run once the tab has settled.")
        return 1

    title = ""
    try:
        title = str(await page.evaluate(web._JS_MARK_FIRST_CHAT) or "").strip()
        if title:
            await page.click("[data-lumine-wa-row='1']", timeout=5000)
            await asyncio.sleep(2.5)
    except Exception as exc:  # noqa: BLE001
        print(f"could not open a chat: {exc}")
    print(f"chat: {title or '(none)'}  header: {await web.active_chat()!r}")

    print("\n--- send button, composer EMPTY ---")
    print(json.dumps(await page.evaluate(_FIND_SEND)))

    print("\n--- bubbles already on screen ---")
    print(json.dumps(await page.evaluate(_FIND_BUBBLES), indent=1)[:1800])

    print("\n--- typing 'probe' ---")
    try:
        await web.type_message("probe")
        composer = await web.composer()
        print(f"composer now: {composer.get('text')!r}  hasSend: {composer.get('hasSend')}")
    except Exception as exc:  # noqa: BLE001
        print(f"typing failed: {exc}")
        return 1

    await asyncio.sleep(1.0)
    print("\n--- send button, composer FULL ---")
    print(json.dumps(await page.evaluate(_FIND_SEND)))
    print("bubbles after typing (an unsent draft must not appear as one):")
    print(json.dumps(await page.evaluate(_FIND_BUBBLES), indent=1)[:900])

    print("\n--- clearing the composer ---")
    cleared = await web._clear_composer()
    print(f"cleared: {cleared}  composer: {(await web.composer()).get('text')!r}")
    print("\nNothing was sent. This probe only ever types and clears.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))