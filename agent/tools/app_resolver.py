"""Resolving "the app the user meant" to exactly one launchable target.

`app_index` answers *what is installed*. This module answers the question that
actually reaches it: **given a name the user said out loud, what should happen --
focus what is already open, or start a new one?** Those are different actions
with different consequences, and answering the second when the first was right is
how "open Krita" produces two Krita windows, and how "open Chrome" ends up
alongside a browser Lumine started.

## The seven questions, in order

1. Is something with this name *running* right now?
2. Does it have an *interactive window*, as opposed to a background process, a
   tray process, or a helper? A running process is not a window: minimised-to-
   tray Electron apps and background updaters both run with no window at all,
   and "focus it" aimed at one of those either fails or, worse, succeeds while
   showing nothing.
3. Is there a window whose *title* answers the name? Window titles are the only
   evidence that says which instance the user is looking at.
4. Which executable or AppID starts it?
5. Is it an installed *web app* (PWA) rather than a classic program?
6. Does the user want a new instance, or the existing one?
7. After starting, did a window actually appear?

Step 7 is the one that most implementations skip, and it is the step that makes
"opened Krita" a claim rather than a guess. `apps.launch` returning proves a
process was requested. It does not prove a window the user can see now exists.
So :func:`open_target` waits for one and reports ``observed`` or ``timed_out``.

## Browser ownership

Four things can be "the browser", and conflating them is the specific bug this
module is here to prevent:

* the user's own Chrome / Edge / Firefox -- a desktop application;
* any *other* installed browser;
* Lumine's isolated Playwright browser -- not a program, it is a process this
  worker owns, and there is no window title to match on;
* an installed PWA running in a browser-managed window, whose title looks like a
  web page rather than like a program.

So the resolver returns an :class:`AppTarget` that names *which* of these it
resolved, and :mod:`agent.runtime.router` uses that to keep "open Chrome" and
"open YouTube in your browser" on separate paths. A browser request never
silently becomes a Playwright session, and a Playwright session never silently
becomes a Chrome launch.

## No model text reaches a process

Every target here comes from `app_index` (a path, an AppID, a bare scheme, a
desktop id) or from a validated window handle, and every launch is a list handed
to `Popen`/`startfile`. The only string this module constructs is the constant
``shell:AppsFolder\\`` prefix, and the value it is joined to is re-validated by
`app_index.valid_app_id` at the moment of use.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import app_index
from .tools_compat import ToolError
from .tools_text import clip

# Sibling modules: always reachable by relative name, because `tools` is a
# package in both import shapes. `runtime` is one level up and is genuinely
# unreachable when there is no parent package, so it gets its own door.
from . import apps, windows_control

try:
    from ..runtime import verification
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime import verification  # type: ignore[no-redef]

logger = logging.getLogger("lumine")

# -- how long we wait for a window to appear ----------------------------------

#: After a launch, how long to watch for a usable window. Long enough for a
#: cold Krita or Photoshop start (several seconds), short enough that a launch
#: which will never produce a window does not hold a voice turn open.
WINDOW_APPEAR_SECONDS = 12.0
WINDOW_POLL_SECONDS = 0.4

#: A cached "this does not resolve" verdict is worth having, but not for long: a
#: program installed after the miss should be found on the next request.
NEGATIVE_TTL_SECONDS = 30.0

# -- browser identities -------------------------------------------------------

#: Programs that are browsers, and what their windows are called. Used so a
#: request naming one never resolves to a Playwright page.
BROWSER_PROGRAMS: dict[str, str] = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "chrome.exe": "chrome",
    "msedge": "edge",
    "microsoft edge": "edge",
    "edge": "edge",
    "firefox": "firefox",
    "mozilla firefox": "firefox",
    "brave": "brave",
    "brave browser": "brave",
    "opera": "opera",
    "vivaldi": "vivaldi",
    "chromium": "chromium",
    "safari": "safari",
}

#: Window-title fragments that identify a browser's own window. A browser window
#: is titled with the *page*, so matching is on the program, not the title -- but
#: the executable behind a window is the honest signal, and the title fragment is
#: the fallback for the case where a process cannot be read.
_BROWSER_WINDOW_MARKERS: tuple[str, ...] = (
    "chrome",
    "edge",
    "firefox",
    "brave",
    "opera",
    "vivaldi",
    "chromium",
)


def browser_program(name: object) -> str:
    """``chrome``/``firefox``/... for a program name, or ``""``."""
    key = app_index.normalize_key(name)
    return BROWSER_PROGRAMS.get(key, "")


def looks_like_browser_window(title: object, process_name: object = "") -> bool:
    """Whether a window is *hosted by* a browser, from its process.

    Deliberately a statement about the host and nothing else. A PWA window is
    browser-hosted, so this returns ``True`` for it -- and that is correct: the
    caller needs to know the window is not a native application of its own, which
    is what decides "can we drive this?".

    Whether the window is *the user's browser* rather than an app running inside
    one is a different question with a different answer, and :func:`classify_window`
    is where it is asked. Confusing the two is how an installed Instagram gets
    reported as Chrome opening, or the other way round.
    """
    if browser_program(process_name):
        return True
    # Only when the process could not be read: a caller with a window but no
    # process name has nothing else to go on, and a wrong guess here is a
    # misclassification rather than a security decision.
    text = str(title or "").casefold()
    return not process_name and any(marker in text for marker in _BROWSER_WINDOW_MARKERS)


#: Window titles that are the *site*, not the application, for the installed
#: web apps this product knows about. A window titled like one of these is a PWA
#: even though its process is a browser.
_WEB_APP_TITLES: dict[str, str] = {
    "instagram": "web_app",
    "whatsapp": "web_app",
    "netflix": "web_app",
    "spotify": "web_app",
    "telegram": "web_app",
    "discord": "web_app",
}


def classify_window(title: object, process_name: object = "", entry: app_index.AppEntry | None = None) -> str:
    """What kind of thing a window is: ``web_app``, ``browser``, or ``program``.

    Three questions in order, and the order is the whole point:

    1. Is this a *site's* window? An installed web app carries the site's name as
       its title, and that is what tells it apart from the browser hosting it.
    2. Is it a browser? Read from the process, which is the honest signal.
    3. Otherwise it is a program.

    Without step 1, "Instagram" resolves as Chrome -- the reported bug -- and
    without step 2, a page whose *title* mentions a browser name resolves as that
    browser.
    """
    text = app_index.normalize_key(title)
    for name, kind in _WEB_APP_TITLES.items():
        if text == name or text.startswith(name + " ") or text.endswith(" - " + name):
            return kind
    if entry is not None and is_web_app_entry(entry):
        return "web_app"
    if looks_like_browser_window(title, process_name):
        return "browser"
    return "program"


# -- the resolved target ------------------------------------------------------


@dataclass(frozen=True)
class AppTarget:
    """One application, resolved, with the evidence behind the resolution."""

    name: str
    #: What kind of thing this is: ``program``, ``web_app``, ``browser``,
    #: ``protocol``, or ``""`` when nothing resolved.
    kind: str = ""
    entry: app_index.AppEntry | None = None
    #: The window that already answers this name, when one does.
    window_title: str = ""
    window_handle: int = 0
    #: How many processes carry this name. Reported so the *caller* can say
    #: "already running" honestly; the resolver never acts on it alone.
    process_count: int = 0
    #: Whether it is a PWA. A Start Menu shortcut written by a browser is the
    #: honest signal available without a Windows API we do not call.
    is_web_app: bool = False
    #: Whether the resolver believes a usable window already exists.
    has_window: bool = False
    notes: dict[str, Any] = field(default_factory=dict)

    #: Kinds, in the order they are asked about. ``web_app`` is not a program and
    #: is not the browser that hosts it, which is the distinction Scenario D turns
    #: on, so it is a kind of its own rather than a flag.
    KIND_WEB_APP = "web_app"
    KIND_BROWSER = "browser"
    KIND_PROGRAM = "program"

    @property
    def resolved(self) -> bool:
        return bool(self.entry is not None or self.window_handle)

    @property
    def running(self) -> bool:
        return self.process_count > 0 or self.has_window

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        for key in ("kind", "windowTitle", "isWebApp", "hasWindow", "processCount"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.entry is not None:
            out["target"] = self.entry.target
        return out


#: Names of web apps that can be installed. Deliberately short: the purpose is
#: to tell an *installed web app* from a *desktop program*, and a heuristic that
#: guessed "anything multi-word" would call "Google Chrome" and "Visual Studio
#: Code" web apps -- which is the classification that starts a browser instead of
#: focusing a window that is already open.
WEB_APP_NAMES: frozenset[str] = frozenset(
    {"instagram", "whatsapp", "netflix", "spotify", "telegram", "discord", "maps"}
)


def is_web_app_entry(app: app_index.AppEntry) -> bool:
    """Whether an index entry is an installed web app rather than a program.

    Three signals, all of them *positive evidence* rather than an absence of
    something:

    * the kind is a protocol handler -- an installed web app registers one, and a
      classic desktop program usually does not;
    * the display name carries a host in parentheses, which is the shape browsers
      give the shortcuts they write ("Instagram (www.instagram.com)");
    * the name is a known web app (:data:`WEB_APP_NAMES`), optionally with a
      ``Desktop`` suffix -- "WhatsApp Desktop" is the app, and so is the PWA.

    What this deliberately does *not* use: "the name has a space in it". That
    would classify Google Chrome, Visual Studio Code and half the Start Menu as
    web apps, and every one of those would then be opened in a browser.
    """
    if app.kind == app_index.KIND_PROTOCOL:
        return True
    name = app.name
    if "(" in name and ")" in name:
        return True
    key = app_index.normalize_key(name)
    stripped = key.removesuffix(" desktop").strip()
    return stripped in WEB_APP_NAMES


# -- process and window facts -------------------------------------------------


def process_names(prefix_filter: str = "") -> list[str]:
    """Executable names of the running processes, lowercase.

    Used for the "is it running" question only -- never to decide *which* window
    the user means, because a process list cannot say that. Non-Windows returns
    an empty list rather than pretending: the caller treats "no facts" as "no
    facts", and the index plus windows still answer.
    """
    if sys.platform != "win32":
        return []
    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None  # type: ignore[assignment]
    if psutil is not None:
        try:
            return sorted({(p.info.get("name") or "").casefold() for p in psutil.process_iter(["name"])})
        except Exception:  # noqa: BLE001 - fall through to the ctypes path
            pass
    return _process_names_toolhelp(prefix_filter)


def _process_names_toolhelp(prefix_filter: str = "") -> list[str]:
    """The same answer from a Toolhelp snapshot, with no third-party package."""
    import ctypes

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    found: list[str] = []
    try:
        entry = ProcessEntry32()
        entry.dwSize = ctypes.sizeof(ProcessEntry32)
        if kernel32.Process32First(snapshot, ctypes.byref(entry)):
            while True:
                name = entry.szExeFile.decode("utf-8", "replace").casefold()
                if name and (not prefix_filter or name.startswith(prefix_filter)):
                    found.append(name)
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
    except Exception:  # noqa: BLE001 - a partial list beats raising
        logger.debug("[resolver] process enumeration failed", exc_info=True)
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def process_count_for(name: object) -> int:
    """How many running processes carry this program's name."""
    key = app_index.normalize_key(name)
    if not key:
        return 0
    executable = key if key.endswith(".exe") else f"{key}.exe"
    if executable in process_names():
        return 1
    return process_names().count(executable)


def window_facts() -> list[tuple[str, int, str]]:
    """``(title, handle, process_name)`` for every visible window.

    Read through `windows_control`, which already excludes this worker's own
    windows and cloaked UWP shells, and which fails with a plain sentence on a
    platform without windows. A read that raises returns no facts at all, which
    the caller treats as "cannot tell" -- never as "nothing is open", because the
    difference decides between focusing a window and launching a second copy.
    """
    try:
        windows = windows_control.list_windows()
    except Exception:  # noqa: BLE001 - no window facts is a valid answer
        return []
    names = set(process_names())
    out: list[tuple[str, int, str]] = []
    for window in windows:
        program = _process_name_for_pid(window.pid, names)
        out.append((window.title, int(window.handle), program))
    return out


def _process_name_for_pid(pid: int, names: Iterable[str]) -> str:
    """Best-effort executable name for a pid. ``""`` when it cannot be read.

    Read from the same snapshot the process list uses where possible; a window
    whose process cannot be identified still returns a title, and the caller can
    fall back to title matching. Refusing to guess is important: a wrong process
    name turns "is this a browser window" into a coin flip.
    """
    if not pid:
        return ""
    try:
        import psutil  # type: ignore

        return (psutil.Process(pid).name() or "").casefold()
    except Exception:  # noqa: BLE001 - psutil absent, denied, or already gone
        return ""


def match_windows(
    query: object, windows: Sequence[tuple[str, int, str]], *, limit: int = 5
) -> list[tuple[str, int, str]]:
    """Windows whose title answers ``query``, best first. Same three tiers as
    everywhere else -- exact, prefix, containment -- because a person says
    "krita", not the whole title bar.

    Enumeration order breaks ties, so two identically-titled windows resolve to
    the same one on every call and a test can assert which.
    """
    wanted = " ".join(str(query or "").split()).casefold()
    if not wanted:
        return []
    hits: list[tuple[int, int, tuple[str, int, str]]] = []
    for position, (title, handle, program) in enumerate(windows):
        normalized = " ".join(str(title or "").split()).casefold()
        if normalized == wanted:
            tier = 0
        elif normalized.startswith(wanted):
            tier = 1
        elif wanted in normalized:
            tier = 2
        else:
            continue
        hits.append((tier, position, (title, handle, program)))
    hits.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in hits[: max(0, limit)]]


# -- resolution ---------------------------------------------------------------


class AppResolver:
    """Resolves names to targets, with a small positive cache.

    The cache holds *resolved targets*, not the whole index -- ``app_index``
    already owns that, with its TTL and its miss-cooldown. What is worth caching
    here is the answer to a question `app_index` cannot answer: whether this name
    currently has a window. That changes far more often than "is it installed",
    which is exactly why it has its own short lifetime (10 s) rather than
    sharing the index's five minutes.
    """

    def __init__(
        self,
        *,
        window_reader: Any = None,
        process_reader: Any = None,
        finder: Any = None,
        clock: Any = time.monotonic,
        window_ttl: float = 10.0,
    ) -> None:
        # Injected so a test can drive resolution without a desktop. Each default
        # is resolved *at call time*, not bound here, so patching a module
        # function is what a test does -- same rule the WhatsApp backend follows.
        self._window_reader = window_reader
        self._process_reader = process_reader
        self._finder = finder
        self._clock = clock
        self._window_ttl = float(window_ttl)
        self._lock = asyncio.Lock()
        self._windows: list[tuple[str, int, str]] = []
        self._windows_at = 0.0
        self._negative: dict[str, float] = {}

    # -- facts --------------------------------------------------------------

    def _windows_now(self) -> list[tuple[str, int, str]]:
        reader = self._window_reader or window_facts
        now = self._clock()
        if self._windows and now - self._windows_at < self._window_ttl:
            return self._windows
        try:
            self._windows = list(reader())
        except Exception:  # noqa: BLE001 - stale facts beat none
            logger.debug("[resolver] window read failed", exc_info=True)
        self._windows_at = now
        return self._windows

    def invalidate(self) -> None:
        """Forget cached window facts. Called after a launch or a close."""
        self._windows_at = 0.0

    # -- the resolution -----------------------------------------------------

    async def resolve(self, name: str) -> AppTarget:
        """One :class:`AppTarget` for ``name``, or an unresolved one.

        Blocking reads go to a worker thread: window enumeration is a ctypes walk
        and the process snapshot is a syscall loop, and neither may sit on the
        event loop where a voice turn is waiting.
        """
        query = " ".join(str(name or "").split())
        if not query:
            return AppTarget(name="")
        return await asyncio.to_thread(self._resolve_sync, query)

    def _resolve_sync(self, query: str) -> AppTarget:
        key = app_index.normalize_key(query)
        if not key:
            return AppTarget(name=query)

        windows = self._windows_now()
        matches = match_windows(query, windows)

        # A window is the strongest evidence there is: it says the app is
        # installed, it is running, and the user is looking at it.
        if matches:
            title, handle, program = matches[0]
            entry = self._find(key)
            return AppTarget(
                name=title,
                kind=self._kind_for(entry, program, title),
                entry=entry,
                window_title=title,
                window_handle=handle,
                process_count=max(1, self._processes_for(key, program)),
                is_web_app=bool(entry is not None and is_web_app_entry(entry)),
                has_window=True,
                notes={"matchedWindows": len(matches), "program": program},
            )

        entry = self._find(key)
        if entry is not None:
            count = self._processes_for(key, "")
            return AppTarget(
                name=entry.name,
                kind=self._kind_for(entry, "", ""),
                entry=entry,
                process_count=count,
                is_web_app=is_web_app_entry(entry),
                # Running but windowless is the case worth naming: the resolver
                # found a process and no window, which is *not* a window to focus.
                has_window=False,
                notes={"windowless": count > 0},
            )

        # Nothing installed, nothing running, nothing open -- but a browser may
        # still be able to open it as a site. That is a *fallback*, recorded as a
        # note, because silently opening a browser for "open Krita" is the exact
        # duplicate-launch behaviour this module exists to prevent.
        return AppTarget(name=query, kind="", entry=None, notes={"unresolved": True})

    def _find(self, key: str) -> app_index.AppEntry | None:
        finder = self._finder or app_index.find
        try:
            found = finder(key, limit=1)
        except TypeError:
            # A test double with a narrower signature is a legitimate reader.
            found = finder(key)
        except Exception:  # noqa: BLE001 - an index failure is a miss, not a crash
            logger.debug("[resolver] app index lookup failed", exc_info=True)
            return None
        return found[0] if found else None

    def _processes_for(self, key: str, program: str) -> int:
        if program:
            return 1
        reader = self._process_reader or process_count_for
        try:
            return int(reader(key))
        except Exception:  # noqa: BLE001 - "cannot tell" is not "not running"
            return 0

    @staticmethod
    def _kind_for(entry: app_index.AppEntry | None, program: str, title: str) -> str:
        if entry is None and not title:
            return ""
        return classify_window(title, program, entry)

    # -- negative caching ---------------------------------------------------

    def note_unresolved(self, name: str) -> None:
        self._negative[app_index.normalize_key(name)] = self._clock()

    def recently_unresolved(self, name: str) -> bool:
        at = self._negative.get(app_index.normalize_key(name))
        if at is None:
            return False
        if self._clock() - at > NEGATIVE_TTL_SECONDS:
            self._negative.pop(app_index.normalize_key(name), None)
            return False
        return True


# -- opening ------------------------------------------------------------------
#
# `open_target` is where "opened" stops being a word and becomes a claim. The
# three states it can return are the honest ones:
#
#   * ``observed``  -- a window existed and we focused it, or a launch was made
#                      and a window appeared afterwards;
#   * ``accepted``  -- the launch was made and no window appeared inside the
#                      deadline. The process exists; we are not claiming a window;
#   * ``failed``    -- nothing was resolved, or the launch raised.
#
# ``accepted`` is not a success and is deliberately not ``observed``: an app that
# takes forty seconds to show a splash screen on a slow machine would otherwise
# be reported as failed for being slow, and an app that never shows a window would
# be reported as opened. Those two failures are different and the user's next
# question differs.


def focus_window(handle: int) -> None:
    """Bring one window to the front, or raise.

    Delegates to `windows_control` so the foreground-lock workaround stays in one
    place. The handle is an int from this module's own enumeration, never a title
    a request supplied.
    """
    if sys.platform != "win32":
        raise ToolError("Window control works on Windows right now.")
    match = next(
        (item for item in windows_control.list_windows() if int(item.handle) == int(handle)),
        None,
    )
    if match is None:
        raise OSError("that window is no longer open")
    windows_control._focus(match)  # noqa: SLF001 - one applier, one caller


def start_entry(app: app_index.AppEntry) -> None:
    """Start one indexed entry. Same rule as `apps.start_entry`, same promise.

    Re-exported rather than reimplemented so there is exactly one function that
    turns an `AppEntry` into a process, and therefore exactly one place where the
    AppID is validated before being paired with ``shell:AppsFolder\\``.
    """
    apps.start_entry(app)


async def open_target(
    target: AppTarget,
    *,
    new_instance: bool = False,
    appear_timeout: float = WINDOW_APPEAR_SECONDS,
    on_progress: Any = None,
) -> verification.ActionOutcome:
    """Focus the existing window, or launch -- and then *check*.

    ``new_instance`` is the only thing that authorises a second window, and it is
    never inferred: a caller that has not asked for a new instance focuses what is
    already there. That is the single most valuable rule in this file, because it
    is what makes "open Krita" twice in one task produce one window.
    """
    action = f"open {target.name}" if target.name else "open the app"

    # -- already open: focus it --------------------------------------------
    if target.has_window and not new_instance:
        try:
            await asyncio.to_thread(focus_window, target.window_handle)
        except Exception as exc:  # noqa: BLE001 - a refused focus is a real answer
            return verification.uncertain(
                action,
                f"{clip(target.window_title, 60)} is open but I could not bring it to the front",
            )
        return verification.observed(
            action,
            f"Brought {clip(target.window_title, 50)} to the front",
            evidence="its window was already open",
            focused=True,
        )

    if target.entry is None:
        return verification.failed(action, f"I couldn't find an app called {clip(target.name, 40)}")

    # -- running but windowless --------------------------------------------
    # A process with no window -- a tray app, a background updater -- is not
    # something to focus, and "focus it" aimed at one either fails or succeeds
    # while showing nothing. The user asked to *open* it, so one start request is
    # made to bring its window up, and the outcome is judged on whether a window
    # appeared. The note says what was already running, because that is what the
    # caller will want to know if it did not.
    windowless = target.running and not new_instance and not target.has_window

    try:
        await asyncio.to_thread(apps.launch_with_entry, target.entry)
    except Exception as exc:  # noqa: BLE001 - launch failures are sentences
        return verification.failed(
            action, f"I found {clip(target.name, 40)} but couldn't start it: {clip(str(exc), 80)}"
        )

    window = await _await_window(target.name, appear_timeout)
    _resolver.invalidate()
    if window is not None:
        title, _handle, _program = window
        return verification.observed(
            action,
            f"Opened {clip(title, 50)}",
            evidence="a window appeared after the launch",
            launched=True,
            title=title,
            alreadyRunning=windowless,
        )
    return verification.accepted(
        action,
        (
            f"{clip(target.name, 50)} was already running, and still has no window"
            if windowless
            else f"Started {clip(target.name, 50)}, but no window has appeared yet"
        ),
        launched=True,
        window_seen=False,
        alreadyRunning=windowless,
    )


async def _await_window(name: str, timeout: float) -> tuple[str, int, str] | None:
    """Watch for a window answering ``name``, up to ``timeout``.

    Bounded by construction and polled rather than sleeping once, because a slow
    app is a reason to keep waiting a little and a crashed one is a reason to stop
    sooner. The cache is invalidated first so the read cannot return the state that
    existed *before* the launch -- which would report a window that was already
    there as proof the launch worked.
    """
    _resolver.invalidate()
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        matches = match_windows(name, window_facts())
        if matches:
            return matches[0]
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(WINDOW_POLL_SECONDS)


_resolver = AppResolver()


def resolver() -> AppResolver:
    """The process-wide resolver."""
    return _resolver


async def resolve(name: str) -> AppTarget:
    return await _resolver.resolve(name)


def invalidate() -> None:
    _resolver.invalidate()