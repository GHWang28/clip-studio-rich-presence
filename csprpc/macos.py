"""macOS backend: observing CLIP STUDIO PAINT through built-in system tools.

Everything here shells out to tools that ship with macOS, so there is nothing
to install. Only the window title strategy needs a privacy permission; the
rest works out of the box.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from csprpc.model import (  # noqa: F401 - re-exported as part of the backend API
    DocumentInfo,
    Observation,
    PermissionDenied,
    ProcessInfo,
    clean_title,
    filter_document_paths,
    pick_document_from_titles,
)

PLATFORM = "macOS"

# Reading another app's window titles is gated behind Accessibility.
NEEDS_WINDOW_PERMISSION = True
WINDOW_PERMISSION_NAME = "Accessibility"
WINDOW_PERMISSION_HINT = (
    "System Settings > Privacy & Security > Accessibility, then enable the app "
    "running csprpc (Terminal, iTerm, or your editor). Also check Privacy & "
    "Security > Automation and allow it to control System Events."
)

_APPLESCRIPT_WINDOW_TITLES = """
on run argv
\tset pidValue to (item 1 of argv) as integer
\tset output to {}
\ttell application "System Events"
\t\tset matches to (every process whose unix id is pidValue)
\t\tif matches is {} then return ""
\t\tset theProcess to item 1 of matches
\t\trepeat with w in windows of theProcess
\t\t\ttry
\t\t\t\tset t to name of w
\t\t\t\tif t is not missing value and t is not "" then set end of output to t
\t\t\tend try
\t\tend repeat
\tend tell
\tset AppleScript's text item delimiters to linefeed
\treturn output as text
end run
"""

# osascript error codes that mean "the user has not granted permission yet".
_PERMISSION_ERRORS = ("-1728", "-1743", "-25211", "-10004")
_PERMISSION_PHRASES = ("not allowed assistive access", "not authorized", "not permitted")

# Directories that hold CSP's own resources rather than the user's artwork.
_OPEN_FILE_EXCLUDES = (
    "/system/",
    "/usr/",
    "/private/var/folders/",
    "/private/tmp/",
    "/.trash/",
    "/celsys/",
    "/library/application support/",
    "/library/caches/",
    "/library/fonts/",
    "/library/preferences/",
)


def _run(argv: Sequence[str], timeout: float = 5.0) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "{}: command not found".format(argv[0])
    except subprocess.TimeoutExpired:
        return 124, "", "{}: timed out after {}s".format(argv[0], timeout)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


#: Run a command and return (exit code, stdout, stderr) without raising.
run_command = _run


# -- process discovery ----------------------------------------------------

_bundle_id_cache: Dict[str, Optional[str]] = {}


def bundle_id_for_executable(executable: str) -> Optional[str]:
    """Read CFBundleIdentifier from the .app bundle containing an executable."""
    if executable in _bundle_id_cache:
        return _bundle_id_cache[executable]

    bundle_id: Optional[str] = None
    match = re.match(r"(.*?\.app)/", executable)
    if match:
        info_plist = Path(match.group(1)) / "Contents" / "Info.plist"
        try:
            with info_plist.open("rb") as handle:
                data = plistlib.load(handle)
            value = data.get("CFBundleIdentifier")
            bundle_id = str(value) if value else None
        except (OSError, ValueError):
            bundle_id = None

    _bundle_id_cache[executable] = bundle_id
    return bundle_id


def list_processes() -> List[Tuple[int, str]]:
    """Every process as (pid, executable path). Needs no special permission."""
    code, out, err = _run(["ps", "-A", "-o", "pid=,comm="])
    if code != 0:
        raise RuntimeError("ps failed: {}".format(err.strip() or code))
    processes: List[Tuple[int, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        try:
            processes.append((int(pid_text), command.strip()))
        except ValueError:
            continue
    return processes


def find_process(match: Dict[str, Any]) -> Optional[ProcessInfo]:
    """Locate the running CLIP STUDIO PAINT process, if there is one."""
    wanted_bundles = {b.lower() for b in match.get("bundle_ids", [])}
    wanted_names = [n.lower() for n in match.get("name_contains", [])]

    fallback: Optional[ProcessInfo] = None
    for pid, executable in list_processes():
        lowered = executable.lower()
        # Helper processes live inside the same bundle; prefer the real app but
        # accept a helper rather than reporting nothing.
        if not any(name in lowered for name in wanted_names):
            continue
        bundle_id = bundle_id_for_executable(executable)
        info = ProcessInfo(pid=pid, executable=executable, bundle_id=bundle_id)
        if bundle_id and bundle_id.lower() in wanted_bundles:
            return info
        if "helper" in lowered or "crashpad" in lowered:
            fallback = fallback or info
            continue
        return info
    return fallback


def frontmost_pid() -> Optional[int]:
    """PID of the app the user is currently in. Needs no special permission."""
    if shutil.which("lsappinfo") is None:
        return None
    code, out, _ = _run(["lsappinfo", "front"], timeout=3.0)
    asn = out.strip()
    if code != 0 or not asn:
        return None
    code, out, _ = _run(["lsappinfo", "info", "-only", "pid", asn], timeout=3.0)
    if code != 0:
        return None
    match = re.search(r"(\d+)\s*$", out.strip())
    return int(match.group(1)) if match else None


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input, system wide."""
    code, out, _ = _run(["ioreg", "-c", "IOHIDSystem", "-d", "1", "-r"], timeout=3.0)
    if code != 0:
        return 0.0
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    if not match:
        return 0.0
    return int(match.group(1)) / 1_000_000_000.0


def find_app_path() -> Optional[str]:
    """Where CLIP STUDIO PAINT is installed, if it can be located."""
    # Only the top two levels: CSP installs either directly in /Applications or
    # inside a versioned folder, and a full walk of every bundle is slow.
    for pattern in ("CLIP STUDIO PAINT.app", "*/CLIP STUDIO PAINT.app"):
        for candidate in Path("/Applications").glob(pattern):
            return str(candidate)
    code, out, _ = _run(
        ["mdfind", "kMDItemCFBundleIdentifier == 'jp.co.celsys.clipstudiopaint'"], timeout=5.0
    )
    if code == 0:
        for line in out.splitlines():
            if line.strip().endswith(".app"):
                return line.strip()
    return None


# -- document discovery ---------------------------------------------------


def window_titles(pid: int, timeout: float = 5.0) -> List[str]:
    """Titles of the process's windows, via Accessibility.

    Raises PermissionDenied if macOS has not granted access yet.
    """
    code, out, err = _run_applescript(_APPLESCRIPT_WINDOW_TITLES, [str(pid)], timeout)

    if code != 0:
        lowered = err.lower()
        if any(token in err for token in _PERMISSION_ERRORS) or any(
            phrase in lowered for phrase in _PERMISSION_PHRASES
        ):
            raise PermissionDenied(err.strip() or "Accessibility permission denied")
        raise RuntimeError(err.strip() or "osascript exited with {}".format(code))

    return [line.strip() for line in out.splitlines() if line.strip()]


def _run_applescript(script: str, args: Sequence[str], timeout: float) -> Tuple[int, str, str]:
    argv = ["osascript", "-"] + list(args)
    try:
        proc = subprocess.run(
            argv,
            input=script.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "osascript: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", "osascript timed out after {}s".format(timeout)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def open_document_paths(
    pid: int,
    extensions: Sequence[str],
    timeout: float = 5.0,
) -> List[str]:
    """Artwork files the process currently has open, newest first."""
    if shutil.which("lsof") is None:
        return []
    code, out, _ = _run(["lsof", "-w", "-n", "-P", "-p", str(pid), "-F", "n"], timeout=timeout)
    # lsof exits non-zero when some file descriptors cannot be inspected, which
    # is normal, so only bail out when it produced nothing at all.
    if not out:
        return []
    paths = [line[1:] for line in out.splitlines() if line.startswith("n/")]
    return filter_document_paths(paths, extensions, _OPEN_FILE_EXCLUDES)


def detect_document(
    pid: int,
    document_config: Dict[str, Any],
    notes: Optional[List[str]] = None,
) -> Optional[DocumentInfo]:
    """Run the configured strategies in order until one finds a document."""
    notes = notes if notes is not None else []
    extensions = document_config.get("extensions", [])
    ignore_titles = document_config.get("ignore_titles", [])

    for strategy in document_config.get("strategies", []):
        if strategy == "window_title":
            try:
                titles = window_titles(pid)
            except PermissionDenied as exc:
                notes.append(
                    "window_title unavailable: {} - grant Accessibility access, see "
                    "`csprpc doctor`".format(str(exc).splitlines()[0])
                )
                continue
            except RuntimeError as exc:
                notes.append("window_title failed: {}".format(exc))
                continue
            document = pick_document_from_titles(titles, extensions, ignore_titles)
            if document:
                return document
            if not titles:
                notes.append("window_title found no windows (is a canvas open?)")

        elif strategy == "open_files":
            paths = open_document_paths(pid, extensions)
            if paths:
                return DocumentInfo(
                    name=os.path.basename(paths[0]),
                    path=paths[0],
                    source="open_files",
                )
            notes.append("open_files found no artwork files held open by the process")

    return None


def observe(config: Dict[str, Any]) -> Observation:
    """Take one full snapshot of the system."""
    result = Observation()

    try:
        process = find_process(config.get("process_match", {}))
    except RuntimeError as exc:
        result.notes.append(str(exc))
        return result

    if process is None:
        return result

    result.running = True
    result.process = process
    result.frontmost = frontmost_pid() == process.pid
    result.idle_seconds = idle_seconds()
    result.document = detect_document(process.pid, config.get("document", {}), result.notes)
    return result
