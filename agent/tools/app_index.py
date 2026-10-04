"""What is installed on this machine, without a shell and without the model.

`open_app` used to resolve a name through PATH and the OS registry alone, which
meant anything reachable only through a Start Menu shortcut -- most Store apps,
every PWAs shortcut Chrome and Edge write, half of what a normal person calls
"an app" -- was invisible to it. This module builds the index that closes that
gap, under three rules:

1. **No model text ever reaches a command line.** The one subprocess here runs
   a constant ``Get-StartApps`` script with no interpolation, and every target
   it produces is validated against a strict pattern before use. A launch is a
   list handed to ``Popen``/``startfile``, never a string with anything spliced
   in -- `apps.launch` keeps that promise, and this module only ever hands it
   data to honor it with.
2. **Collection is best-effort and pales beside nothing.** A collector that
   fails is logged and skipped; the index is allowed to be incomplete (the
   legacy PATH/registry chain in `apps.launch` still runs after a miss), but it
   is never *wrong* in a way that could start something the user did not
   install: protocol handlers pass a deny list before they are indexed, and
   ``startfile("scheme:")`` carries no arguments of ours.
3. **Matching is pure.** :func:`match` takes a query and a list of entries and
   returns a ranked list -- no I/O, no globals -- so the ranking a test asserts
   is the ranking the tool runs.

The cache is a TTL, not a session: apps get installed and uninstalled while
the worker is alive, and an index built at startup would refuse a program
installed a minute ago until the worker restarted.

## Why there are two tiers

A full build costs **~2.0 s** on the machine this was written on, every time,
and measured three ways each: Start Menu 37/15/17 ms, App Paths 10/4/4 ms, PATH
bins 41/27/30 ms -- against the `Get-StartApps` probe at 1931/1350/1373 ms and
the registry-wide protocol walk at 467/565/486 ms. Two collectors are 2.4 s of
a 2.0 s build and contribute 133 of 1054 entries.

So the build is split. The fast tier is built inline and answers immediately;
the slow tier starts **on a miss**, in a daemon thread, and a lookup that has
already found something never waits for it. Both halves are derived from
:func:`_collectors`, so a new collector lands in exactly one tier by default
and `fast + slow` cannot stop equalling `all`.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

logger = logging.getLogger("lumine")

# -- entry kinds -------------------------------------------------------------

#: Start Menu shortcuts (``.lnk``/``.url``). Covers classic programs and the
#: shortcuts browsers write for PWAs, so a PWA is a normal app here.
KIND_SHORTCUT = "shortcut"
#: A UWP/Store AppUserModelID from ``Get-StartApps``.
KIND_START_APP = "startapp"
#: A registered App Paths executable (``chrome.exe`` and friends).
KIND_APP_PATH = "apppath"
#: An executable found on ``PATH``.
KIND_PATH = "path"
#: A registered URL-protocol handler, launched as a bare ``scheme:``.
KIND_PROTOCOL = "protocol"
#: A macOS ``.app`` bundle.
KIND_BUNDLE = "bundle"
#: A Linux ``.desktop`` entry, launched by id through the desktop environment.
KIND_DESKTOP = "desktop"

#: Resolution order when two entries claim the same key. A real shortcut beats
#: a protocol handler because a protocol is the most indirect launch there is,
#: and it is the one whose registration a user is least likely to remember
#: making.
KIND_PRIORITY: dict[str, int] = {
    KIND_SHORTCUT: 0,
    KIND_START_APP: 1,
    KIND_BUNDLE: 1,
    KIND_APP_PATH: 2,
    KIND_PATH: 3,
    KIND_DESKTOP: 3,
    KIND_PROTOCOL: 4,
}

#: Absolute ceiling on one index build. A pathological machine (or a PATH full
#: of node_modules) must not turn a tool call into an inventory of the disk.
MAX_ENTRIES = 5_000

#: How the scan bounds itself while walking a Start Menu tree.
MAX_WALK_FILES = 20_000
MAX_WALK_DEPTH = 8


@dataclass(frozen=True)
class AppEntry:
    """One installed thing: what it is called, and what starts it.

    ``target`` is data, never a command: a path to hand to ``startfile``/``Popen``,
    an AppID to pair with the constant ``shell:AppsFolder\`` prefix, a bare
    ``scheme:`` for a protocol, or a desktop-file id. Nothing appends arguments
    to it, and the value came from the machine rather than from a request.
    """

    name: str
    kind: str
    target: str


def entry(name: str, kind: str, target: str) -> AppEntry:
    """Build an entry, dropping anything without a name to match on."""
    clean = " ".join(str(name or "").split())
    if not clean:
        raise ValueError("an app entry needs a name")
    return AppEntry(name=clean, kind=kind, target=str(target or ""))


def normalize_key(name: object) -> str:
    """What "the same app" means to :func:`match`.

    Case-folded, whitespace-collapsed, and stripped of the extensions that are
    suffixes rather than identity -- ``Google Chrome.lnk``, ``chrome.exe`` and
    ``Chrome`` are all spells of one app. ``launch_key`` in `apps` does the same
    job for the dedup window; they agree on purpose, because a model that spells
    one request three ways should get neither three windows nor three matches.
    """
    key = " ".join(str(name or "").split()).casefold()
    for extension in (".lnk", ".url", ".exe", ".app", ".desktop", ".com"):
        if key.endswith(extension):
            key = key[: -len(extension)].rstrip()
    return key


# -- matching (pure) ---------------------------------------------------------


def match(query: object, entries: Sequence[AppEntry], *, limit: int = 5) -> list[AppEntry]:
    """Entries whose name answers ``query``, best first.

    Three tiers: exact key, prefix, containment -- so ``code`` finds Visual
    Studio Code and ``google`` finds Google Chrome without either being spelled
    out in full. Within a tier the kind decides (a Start Menu shortcut beats a
    protocol for the same name) and the key breaks what is left, so the order is
    a fact a test can pin rather than a walk order of the filesystem.

    A miss returns ``[]``; callers decide whether a miss is an error, and
    `apps.launch` decides it after its own legacy chain has also had a turn.
    """
    query_key = normalize_key(query)
    if not query_key:
        return []
    found: list[tuple[int, int, str, AppEntry]] = []
    for app in entries:
        key = normalize_key(app.name)
        if key == query_key:
            tier = 0
        elif key.startswith(query_key):
            tier = 1
        elif query_key in key:
            tier = 2
        else:
            continue
        found.append((tier, KIND_PRIORITY.get(app.kind, 9), key, app))
    found.sort(key=lambda row: (row[0], row[1], row[2]))
    return [app for _tier, _kind, _key, app in found[: max(0, limit)]]


# -- Windows: the constant PowerShell probe ---------------------------------

#: The only script in this module, and it takes no input. ``Get-StartApps`` is
#: what the Start Menu itself reads: names plus AppUserModelIDs, which is the
#: one list that includes Store/UWP apps that have no ``.lnk`` anywhere.
#: ``[char]9`` is a literal tab -- a here-string would invite quoting games for
#: no benefit.
_START_APPS_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue'; "
    "Get-StartApps | ForEach-Object { $_.Name + [char]9 + $_.AppID }"
)

#: An AppUserModelID: ``PackageFamilyName!ApplicationId``. Anything else in the
#: output is either a classic AppID (no bang, alnum/dot/dash) or garbage we
#: refuse to launch with.
_APPID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}(?:![A-Za-z0-9][A-Za-z0-9._\-]{0,63})?$")

#: Schemes that fetch remote content or run scripting when invoked. We pass no
#: arguments, but the scheme alone is enough for these to do something outside
#: "start the program the user meant": ``file:`` is arbitrary local content,
#: ``http``/``https`` are arbitrary remote content, ``ms-msdt`` and friends are
#: documented exploit carriers. Deny-by-name for this set, allow for the rest --
#: the rest have already been checked against their handler below.
DENIED_SCHEMES = frozenset(
    {
        "file", "http", "https", "ftp", "sftp", "javascript", "vbscript", "jar",
        "ms-msdt", "ms-settings", "search-ms", "ms-officeapp", "ms-defender",
        "powershell", "pwsh", "cmd", "ms-word", "ms-excel", "ms-powerpoint",
        "outlook", "onenote",
    }
)

#: Handler executables a protocol may not be registered to. A local registry
#: entry pointing at a script host is either an accident or something that got
#: there without asking; either way a launch through it is not "open Spotify".
DENIED_COMMANDS = frozenset(
    {
        "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
        "wscript", "wscript.exe", "cscript", "cscript.exe", "mshta", "mshta.exe",
        "rundll32", "rundll32.exe", "regsvr32", "regsvr32.exe", "bitsadmin",
        "bitsadmin.exe", "certutil", "certutil.exe", "msiexec", "msiexec.exe",
        "pcalua", "pcalua.exe", "forfiles", "forfiles.exe", "bash", "sh", "zsh",
    }
)

#: A scheme we are willing to treat as an app name at all.
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]{1,31}$")


def valid_app_id(value: object) -> bool:
    """Whether a value may be paired with the constant ``shell:AppsFolder\\``.

    Checked at collection *and* again in `apps.start_entry`: an AppID is the
    one index target that gets concatenated with a prefix, and a prefix plus
    an unchecked value is how "no shell" quietly turns into one.
    """
    return bool(_APPID.match(str(value or "")))

#: Shortcuts whose name says what they do: running one starts an uninstaller,
#: which is not what anyone means by "open the app".
_UNINSTALL_NAME = re.compile(r"^(uninstall|remove)\b", re.IGNORECASE)


def command_basename(command: object) -> str:
    """The executable a registry command line would run, lowercased.

    Parsing only -- the result is compared against :data:`DENIED_COMMANDS` and
    never executed. Quoted and bare forms both come out as a filename, on any
    platform, so the deny test is pure.
    """
    text = str(command or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        token = text[1:end] if end > 0 else ""
    else:
        token = text.split(" ", 1)[0]
    return token.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def is_launchable_scheme(scheme: object, handler_command: object) -> bool:
    """Whether a protocol may be indexed, given the handler it is registered to.

    All three conditions must hold: the scheme itself looks like an app name we
    would accept, it is not on the deny list, and its handler is a real program
    rather than a script host. A handler we cannot parse fails closed -- a
    protocol we cannot inspect is one we simply do not offer.
    """
    name = normalize_key(scheme)
    if not _SCHEME.match(name) or name in DENIED_SCHEMES:
        return False
    basename = command_basename(handler_command)
    if not basename:
        return False
    return basename not in DENIED_COMMANDS


def parse_startapps(text: object) -> list[AppEntry]:
    """``Get-StartApps`` output as entries. Pure, so a fixture can drive it.

    Lines are ``name<TAB>AppID``; a line without a valid AppID is dropped
    rather than trusted, because this list is what gets paired with
    ``shell:AppsFolder\`` later.
    """
    entries: list[AppEntry] = []
    for line in str(text or "").splitlines():
        if "\t" not in line:
            continue
        name, _sep, app_id = line.partition("\t")
        app_id = app_id.strip()
        if not name.strip() or not _APPID.match(app_id):
            continue
        entries.append(entry(name, KIND_START_APP, app_id))
    return entries


# -- collectors -------------------------------------------------------------- #
#
# Every collector is best-effort: it returns what it found or an empty list,
# and never raises past `build_entries`. Platform branches live inside each one
# so importing this module stays safe everywhere.


def start_menu_roots() -> list[str]:
    """The per-user and machine-wide Start Menu Program roots, if they exist."""
    roots: list[str] = []
    for base in (os.environ.get("APPDATA"), os.environ.get("ProgramData")):
        if not base:
            continue
        path = os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs")
        if os.path.isdir(path):
            roots.append(path)
    return roots


def collect_start_menu() -> list[AppEntry]:
    """``.lnk``/``.url`` shortcuts, recursively -- where PWAs live too."""
    if sys.platform != "win32":
        return []
    found: list[AppEntry] = []
    checked = 0
    for root in start_menu_roots():
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath.count(os.sep) - root.count(os.sep)
            if depth >= MAX_WALK_DEPTH:
                # Files at the cap are kept; the descent below it is not, and
                # clearing `dirnames` is what actually stops `os.walk` from
                # taking another step down.
                dirnames[:] = []
            for filename in filenames:
                checked += 1
                if checked > MAX_WALK_FILES:
                    return found
                lowered = filename.casefold()
                if not lowered.endswith((".lnk", ".url")):
                    continue
                stem = filename[: filename.rfind(".")]
                if _UNINSTALL_NAME.match(stem.strip()):
                    continue
                found.append(entry(stem, KIND_SHORTCUT, os.path.join(dirpath, filename)))
    return found


def collect_start_apps() -> list[AppEntry]:
    """UWP/Store apps through one constant PowerShell invocation.

    No input is interpolated: the script is a literal, the arguments are
    flags, and the output is parsed by `parse_startapps`. A machine without
    PowerShell simply contributes nothing -- the other collectors still run.
    """
    if sys.platform != "win32":
        return []
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return []
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", _START_APPS_SCRIPT],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("[app_index] Get-StartApps failed: %s", exc)
        return []
    return parse_startapps(result.stdout)


def collect_app_paths() -> list[AppEntry]:
    """Registered App Paths executables -- ``chrome.exe`` without a PATH entry."""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - winreg is Windows-only by build
        return []
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    found: list[AppEntry] = []
    seen: set[str] = set()
    for hive in hives:
        for view in views:
            try:
                with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | view) as root:
                    count = winreg.QueryInfoKey(root)[0]
                    for index in range(count):
                        try:
                            subkey_name = winreg.EnumKey(root, index)
                        except OSError:
                            continue
                        try:
                            with winreg.OpenKey(root, subkey_name) as subkey:
                                target, _kind = winreg.QueryValueEx(subkey, "")
                        except OSError:
                            continue
                        display = subkey_name[:-4] if subkey_name.casefold().endswith(".exe") else subkey_name
                        name = normalize_key(display)
                        if not name or name in seen or not str(target).strip():
                            continue
                        seen.add(name)
                        found.append(entry(display, KIND_APP_PATH, str(target)))
            except OSError:
                continue
    return found


def collect_path_bins() -> list[AppEntry]:
    """Executables on ``PATH`` -- the set the legacy chain already resolves,
    made visible to `find_app` so it can list them too.

    ``.exe``/``.com`` only. A batch file on PATH is reachable through the
    legacy ``shutil.which`` chain; indexing it here would make `find_app`
    advertise something this module deliberately does not name.
    """
    if sys.platform != "win32":
        return []
    found: list[AppEntry] = []
    seen: set[str] = set()
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        if not directory or not os.path.isdir(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for filename in names:
            lowered = filename.casefold()
            if not lowered.endswith((".exe", ".com")):
                continue
            name = normalize_key(filename)
            if not name or name in seen:
                continue
            seen.add(name)
            found.append(entry(filename, KIND_PATH, os.path.join(directory, filename)))
    return found


def collect_protocols() -> list[AppEntry]:
    """Registered URL-protocol handlers, after the deny checks.

    These are what makes ``open spotify`` work when Spotify installed a
    ``spotify:`` handler and nothing else. Each candidate must pass
    :func:`is_launchable_scheme` against its actual registered command --
    a scheme whose handler is ``mshta`` does not get indexed, and a scheme we
    cannot read the handler of does not get indexed either.
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows-only by build
        return []
    found: list[AppEntry] = []
    try:
        root = winreg.ConnectRegistry(None, winreg.HKEY_CLASSES_ROOT)
    except OSError:
        return []
    try:
        try:
            total = winreg.QueryInfoKey(root)[0]
        except OSError:
            return []
        for index in range(total):
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                continue
            if name.startswith("."):
                continue  # a file extension, not a scheme
            try:
                with winreg.OpenKey(root, name) as scheme_key:
                    winreg.QueryValueEx(scheme_key, "URL Protocol")
            except OSError:
                continue  # no URL Protocol value: not a registered scheme
            try:
                with winreg.OpenKey(root, name) as scheme_key:
                    with winreg.OpenKey(scheme_key, r"shell\open\command") as command_key:
                        command, _kind = winreg.QueryValueEx(command_key, "")
            except OSError:
                continue
            if not is_launchable_scheme(name, command):
                continue
            found.append(entry(name, KIND_PROTOCOL, f"{name.lower()}:"))
    finally:
        try:
            root.Close()
        except OSError:  # pragma: no cover - closing is best-effort
            pass
    return found


def collect_applications() -> list[AppEntry]:
    """macOS ``.app`` bundles. The cross-platform seam, exercised on darwin."""
    if sys.platform != "darwin":
        return []
    found: list[AppEntry] = []
    for base in ("/Applications", "/System/Applications"):
        user = os.path.expanduser("~/Applications") if base == "/Applications" else None
        for directory in filter(None, (base, user)):
            try:
                for filename in sorted(os.listdir(directory)):
                    if filename.endswith(".app"):
                        found.append(entry(filename[:-4], KIND_BUNDLE, os.path.join(directory, filename)))
            except OSError:
                continue
    return found


def collect_desktop_files() -> list[AppEntry]:
    """Linux ``.desktop`` entries. Launched by id; the Exec line is never read.

    Reading ``Exec=`` would mean parsing -- and therefore trusting -- a command
    line written by any package that ever installed itself. The desktop
    environment resolves the id the same way the app menu does, which keeps the
    trust decision where a human already made it.
    """
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return []
    found: list[AppEntry] = []
    candidates = [
        "/usr/share/applications",
        "/usr/local/share/applications",
        os.path.expanduser("~/.local/share/applications"),
    ]
    for directory in candidates:
        try:
            filenames = sorted(os.listdir(directory))
        except OSError:
            continue
        for filename in filenames:
            if not filename.endswith(".desktop"):
                continue
            path = os.path.join(directory, filename)
            name = ""
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if line.startswith("Name="):
                            name = line[5:].strip()
                            break
                        if line.startswith("[") and name:
                            break
            except OSError:
                continue
            if name:
                found.append(entry(name, KIND_DESKTOP, filename[: -len(".desktop")]))
    return found


def _collectors() -> tuple[Callable[[], Iterable[AppEntry]], ...]:
    """Which collectors apply here. Decided per build, so tests can swap it."""
    if sys.platform == "win32":
        return (
            collect_start_menu,
            collect_start_apps,
            collect_app_paths,
            collect_path_bins,
            collect_protocols,
        )
    if sys.platform == "darwin":
        return (collect_applications,)
    return (collect_desktop_files,)


#: The collectors that cost real time. Measured on the machine this was written
#: on, three builds each: Start Menu 37/15/17 ms, App Paths 10/4/4 ms, PATH bins
#: 41/27/30 ms -- against the protocol walk at 467/565/486 ms and the PowerShell
#: probe at 1931/1350/1373 ms. The full build is ~2.0 s every time, and these two
#: account for all but ~88 ms of it while contributing 133 of 1054 entries.
#:
#: Membership is a set of the functions themselves rather than a second list of
#: names, and the tiers below are *derived* from `_collectors`, so a new
#: collector is in exactly one tier by default and the two cannot drift apart.
SLOW_COLLECTORS = frozenset({collect_start_apps, collect_protocols})


def _fast_collectors() -> tuple[Callable[[], Iterable[AppEntry]], ...]:
    """Everything that answers from a directory walk or one targeted registry
    key. Order is `_collectors`' order with the slow ones removed, so first-wins
    still means "the better kind wins" inside the tier."""
    return tuple(collect for collect in _collectors() if collect not in SLOW_COLLECTORS)


def _slow_collectors() -> tuple[Callable[[], Iterable[AppEntry]], ...]:
    """The spawn-a-process and walk-everything collectors. Run off the critical
    path, and on a miss rather than on every lookup."""
    return tuple(collect for collect in _collectors() if collect in SLOW_COLLECTORS)


def _run_collectors(collectors: Iterable[Callable[[], Iterable[AppEntry]]]) -> list[AppEntry]:
    """Run collectors in order, deduplicated by normalized key.

    First collector wins a key: a Start Menu shortcut for Chrome is a better
    thing to launch than Chrome's protocol handler, and both are better than a
    raw PATH hit. A collector that raises is logged and skipped -- one broken
    source must not cost the whole index.
    """
    out: list[AppEntry] = []
    seen: set[str] = set()
    for collect in collectors:
        try:
            produced = collect()
        except Exception:  # noqa: BLE001 - a collector must never kill the index
            logger.debug("[app_index] collector %s failed", getattr(collect, "__name__", collect), exc_info=True)
            continue
        for app in produced:
            if len(out) >= MAX_ENTRIES:
                return out
            key = normalize_key(app.name)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(app)
    return out


def build_fast_entries() -> list[AppEntry]:
    """The tier a lookup may wait for: ~88 ms cold on the measured machine."""
    return _run_collectors(_fast_collectors())


def build_slow_entries() -> list[AppEntry]:
    """The tier that runs in the background: ~2.4 s, 133 entries."""
    return _run_collectors(_slow_collectors())


def build_entries() -> list[AppEntry]:
    """Run every collector, in priority order. Still the full index in one pass.

    Kept because it is the definition the tiers are split *from*, and because a
    test patches `_collectors` and reads the answer back from here.
    """
    return _run_collectors(_collectors())


def merge_entries(fast: Sequence[AppEntry], slow: Sequence[AppEntry]) -> list[AppEntry]:
    """Fold the slow tier under the fast one, letting a better kind win a name.

    The two tiers were collected separately, so the "first collector wins" rule
    cannot be applied by concatenation: `collect_start_apps` runs *before*
    `collect_app_paths` in the full order, but lands in the *other* tier. A
    Store app that shares a name with an App Paths entry must still beat it, and
    a protocol must still lose to a PATH hit -- so the decision is restated as
    what it always was, `KIND_PRIORITY`, instead of being approximated by
    "whichever tier finished first".

    Names only in the slow tier are appended, which is the whole point: those
    133 entries are invisible until this runs.
    """
    if not slow:
        return list(fast)
    out = list(fast)
    position_of: dict[str, int] = {}
    for position, app in enumerate(out):
        key = normalize_key(app.name)
        if key and key not in position_of:
            position_of[key] = position
    for app in slow:
        key = normalize_key(app.name)
        if not key:
            continue
        position = position_of.get(key)
        if position is None:
            if len(out) >= MAX_ENTRIES:
                break
            position_of[key] = len(out)
            out.append(app)
            continue
        current = out[position]
        if KIND_PRIORITY.get(app.kind, 9) < KIND_PRIORITY.get(current.kind, 9):
            out[position] = app
    return out


# -- the cache ---------------------------------------------------------------

#: How long a built index is trusted.
DEFAULT_TTL_SECONDS = 300.0
MIN_TTL_SECONDS = 15.0
MAX_TTL_SECONDS = 3600.0

#: A miss may not rebuild more often than this. Without a cooldown every typo
#: would spawn PowerShell; with only the TTL, a program installed one minute ago
#: would be invisible for five. A single rebuild per window after a miss gets
#: both right.
MISS_REBUILD_SECONDS = 20.0

TTL_VAR = "LUMINE_APP_INDEX_TTL"


def ttl_seconds(raw: object = None) -> float:
    """Index lifetime from ``LUMINE_APP_INDEX_TTL``, clamped.

    Unparseable and infinite values fall back to the default rather than to
    "never rebuild": an index that outlives the machine's software list is a
    list of things that are no longer true.
    """
    if raw is None:
        raw = os.getenv(TTL_VAR, "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf
        return DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, value))


#: How long a lookup that has already missed may wait for the slow tier. Three
#: seconds is a PowerShell probe and a whole registry walk with room to spare on
#: a cold disk (measured: 1.9 s and 0.5 s warm). Past that the caller is better
#: served by the answer the fast tier already gave it than by holding a voice
#: turn open for a completeness that may not change the result -- the wait is
#: bounded, and the build it is waiting for keeps running.
SLOW_WAIT_SECONDS = 3.0


class AppIndex:
    """A TTL-cached, thread-safe index. One instance per process.

    Two tiers, because on the machine this was measured on one of them is 88 ms
    and the other is 2.0 s:

    * the **fast** tier (Start Menu, App Paths, PATH) is built inline, which is
      what every lookup sees the moment it asks;
    * the **slow** tier (the `Get-StartApps` probe and the registry-wide
      protocol walk) starts *on a miss*, runs in a daemon thread, and is merged
      in when it lands. A hit never pays for it.

    ``builder`` and ``now`` are injectable so a test can drive a build and a
    clock without touching a filesystem -- the cache is the fiddly part, and
    the part that decides whether a stale path gets launched. Supplying a
    ``builder`` alone means the caller owns the whole index, so no background
    thread is ever scheduled: a test counting builds must not also be paying for
    PowerShell. Supplying ``slow_builder`` as well opts back into the two-tier
    path, which is how the tiering itself is tested without a real machine.
    """

    def __init__(
        self,
        *,
        builder: Callable[[], list[AppEntry]] | None = None,
        now: Callable[[], float] = time.monotonic,
        slow_builder: Callable[[], list[AppEntry]] | None = None,
    ) -> None:
        self._builder = builder if builder is not None else build_fast_entries
        self._slow_builder = slow_builder if slow_builder is not None else build_slow_entries
        self._tiered = builder is None or slow_builder is not None
        self._now = now
        self._lock = threading.Lock()
        self._entries: tuple[AppEntry, ...] = ()
        self._slow_entries: tuple[AppEntry, ...] = ()
        self._merged: tuple[AppEntry, ...] = ()
        self._built_at: float | None = None
        self._slow_running = False
        self._slow_pending = False
        self._slow_ready = threading.Event()
        self._generation = 0

    def get(self, *, force: bool = False) -> tuple[AppEntry, ...]:
        """The index, built if it is missing or older than the TTL.

        The build runs *outside* the lock: it can take a second (PowerShell,
        two Start Menu trees) and a second tool call waiting on it would be a
        second stalled voice turn. The loser of a build race simply writes its
        result second, and both are current.

        The fast tier is what comes back. The slow tier is marked pending and
        left to a background thread -- deliberately *not* started here, because
        a lookup that is about to hit would never need it.
        """
        with self._lock:
            if not force and self._built_at is not None and self._now() - self._built_at < ttl_seconds():
                return self._merged
        built = tuple(self._builder())
        with self._lock:
            self._entries = built
            self._built_at = self._now()
            # A fresh fast tier must not be presented alongside a slow tier from
            # the previous cycle: apps get installed and uninstalled, and a
            # Store app that was removed should stop resolving when everything
            # else has caught up.
            self._slow_entries = ()
            self._slow_pending = self._tiered
            self._refresh_merged_locked()
            return self._merged

    def await_slow(self, timeout: float = SLOW_WAIT_SECONDS) -> bool:
        """Start the slow tier if it is owed, then wait for it. Bounded.

        Returns whether a build completed during this call -- which is the only
        case where re-reading the index can produce a different answer. Returns
        ``False`` immediately when there is nothing owed, when the index has
        never been built (so a test with a patched reader starts no process),
        or when the index is not tiered at all.
        """
        with self._lock:
            if not self._tiered or self._built_at is None:
                return False
            if self._slow_running:
                ready = self._slow_ready
            elif self._slow_pending:
                ready = self._start_slow_locked()
            else:
                return False
        return ready.wait(timeout)

    def _start_slow_locked(self) -> threading.Event:
        """Arm one background slow build. Caller holds the lock."""
        ready = threading.Event()
        generation = self._generation
        self._slow_ready = ready
        self._slow_running = True
        thread = threading.Thread(
            target=self._run_slow,
            args=(ready, generation),
            name="lumine-app-index-slow",
            daemon=True,
        )
        thread.start()
        return ready

    def _run_slow(self, ready: threading.Event, generation: int) -> None:
        try:
            built = tuple(self._slow_builder())
        except Exception:  # noqa: BLE001 - a background build must never surface
            logger.debug("[app_index] slow build failed", exc_info=True)
            built = ()
        with self._lock:
            # A `reset()` during the build means its answer is describing a
            # cache nobody is reading any more; writing it would resurrect the
            # entries the reset just threw away.
            if generation == self._generation:
                self._slow_entries = built
                self._slow_running = False
                self._slow_pending = False
                self._refresh_merged_locked()
        ready.set()

    def _refresh_merged_locked(self) -> None:
        self._merged = tuple(merge_entries(self._entries, self._slow_entries))

    def mark_built(self) -> None:
        """Record a fresh build time without building. Tests use it to arm the
        miss-cooldown without a filesystem."""
        with self._lock:
            self._built_at = self._now()

    def miss_rebuild_due(self) -> bool:
        """Whether a failed match may spend a rebuild. See `MISS_REBUILD_SECONDS`."""
        with self._lock:
            if self._built_at is None:
                return True
            return self._now() - self._built_at >= MISS_REBUILD_SECONDS

    def forget(self) -> None:
        """Drop everything cached, including an in-flight slow build's claim
        on it. Tests and nothing else call this."""
        with self._lock:
            self._generation += 1
            self._entries = ()
            self._slow_entries = ()
            self._merged = ()
            self._built_at = None
            self._slow_running = False
            self._slow_pending = False
            self._slow_ready = threading.Event()


_INDEX = AppIndex()


def entries(*, force: bool = False) -> tuple[AppEntry, ...]:
    """The process-wide index."""
    return _INDEX.get(force=force)


def find(query: object, *, limit: int = 5, force: bool = False, complete: bool = False) -> list[AppEntry]:
    """Best matches for ``query``. ``force`` rebuilds before matching.

    ``complete`` is the slow tier's door, and it opens on a miss and only on a
    miss: the fast tier answers in ~88 ms and most queries are answered by it,
    so a lookup that has already found something is never asked to wait two
    seconds to be *more* right. A lookup that found nothing may wait
    `SLOW_WAIT_SECONDS` for the background build, then match again against the
    merged index. It never waits when the index has not been built (a test with
    a patched reader must not start a process) or when there is no slow tier
    owed.
    """
    found = match(query, entries(force=force), limit=limit)
    if found or not complete:
        return found
    if _INDEX.await_slow():
        found = match(query, entries(), limit=limit)
    return found


def resolve(query: object) -> AppEntry | None:
    """The one entry `apps.launch` should try first, or ``None``.

    Three looks, cheapest first, each only reached if the one before it found
    nothing: the fast tier, then the slow tier behind a bounded wait, then --
    once per `MISS_REBUILD_SECONDS` -- a rebuild.

    The rebuild deliberately matches **without** waiting for the slow tier
    again. Its purpose is "I just installed it", and a program installed in the
    last twenty seconds appears in the Start Menu, which the rebuild refreshes.
    The slow tier was already run moments earlier by the miss above; running it
    a second time would buy freshness measured in seconds at two and a half
    seconds apiece, on a query that has already been through both collectors.
    """
    found = find(query, limit=1, complete=True)
    if found:
        return found[0]
    if _INDEX.miss_rebuild_due():
        found = find(query, limit=1, force=True)
        return found[0] if found else None
    return None


def reset() -> None:
    """Forget the cached index. Tests and nothing else call this."""
    _INDEX.forget()
