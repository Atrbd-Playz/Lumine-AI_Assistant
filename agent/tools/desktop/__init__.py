"""Controlling this computer's mouse and keyboard, for applications Lumine
has no dedicated tool for.

Four modules, split by what changes:

``uia``
    The Windows UI Automation harness -- one worker thread, a COM apartment
    per thread, a watchdog, and the keystroke/injection primitives. It was
    extracted from ``messaging.whatsapp_desktop``, which had the first and
    only copy of it, so there is one driver to keep in step rather than two.
``input``
    ``SendInput`` itself: move, click, drag, scroll, type, hotkey. No COM,
    no accessibility tree, and no dependency on ``uiautomation`` being
    installed -- which is what lets it drive a canvas or a game that exposes
    no elements at all.
``capture``
    What is on screen right now, and a generation token for it. The token is
    the whole reason this module exists; see its docstring.
``ops`` and ``control``
    The operation table and the ``desktop_control`` tool that reads it.

## What this is for, and what it is not for

This is the *fallback*. Anything with a dedicated tool keeps it: the browser
goes to ``browse``/``web_control``, WhatsApp to the messaging tools. A
selector is more reliable than a blind click, and ``control`` refuses to
become a way around the questions those tools ask. The reasoning is in
``ops.INTEGRATED_WINDOWS``, which is where that rule lives rather than being
scattered across call sites as an ``if``.
"""
