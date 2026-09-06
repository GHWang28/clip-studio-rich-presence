"""Windows backend: observing CLIP STUDIO PAINT through the Win32 API.

Everything here goes through ``ctypes`` against DLLs that are part of Windows,
so there is nothing to install. Unlike macOS, reading another application's
window titles needs no privacy permission, so the file name is available out
of the box.

This module imports cleanly on any platform; the Win32 entry points are only
bound when actually running on Windows, and calling them elsewhere raises.
"""

from __future__ import annotations

import os
import subprocess
import sys
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

PLATFORM = "Windows"

# Win32 lets any process read any window's title, so there is nothing to grant.
NEEDS_WINDOW_PERMISSION = False
WINDOW_PERMISSION_NAME = None
WINDOW_PERMISSION_HINT = ""

IS_WINDOWS = sys.platform == "win32"

# Hides the console window that would otherwise flash for each helper command.
_CREATE_NO_WINDOW = 0x08000000

_TICK_MASK = 0xFFFFFFFF

if IS_WINDOWS:  # pragma: no cover - exercised only on Windows
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetTickCount.restype = wintypes.DWORD
    _kernel32.GetTickCount.argtypes = []

    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.GetLastInputInfo.restype = wintypes.BOOL
    _user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise RuntimeError("the Windows backend is only usable on Windows")


def run_command(argv: Sequence[str], timeout: float = 5.0) -> Tuple[int, str, str]:
    """Run a command without flashing a console window."""
    try:
        proc = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            creationflags=_CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        return 127, "", "{}: command not found".format(argv[0])
    except subprocess.TimeoutExpired:
        return 124, "", "{}: timed out after {}s".format(argv[0], timeout)
    encoding = "mbcs" if IS_WINDOWS else "utf-8"
    return (
        proc.returncode,
        proc.stdout.decode(encoding, "replace"),
        proc.stderr.decode(encoding, "replace"),
    )


# -- process discovery ----------------------------------------------------


def _full_image_path(pid: int) -> Optional[str]:
    """The process's full executable path, or None if it cannot be read."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        _kernel32.CloseHandle(handle)


def list_processes() -> List[Tuple[int, str]]:
    """Every process as (pid, executable path)."""
    _require_windows()
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        raise RuntimeError(
            "CreateToolhelp32Snapshot failed: {}".format(ctypes.get_last_error())
        )
    processes: List[Tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return processes
        while True:
            pid = int(entry.th32ProcessID)
            # The full path needs an open handle, which can be refused; the
            # bare executable name is still enough to match on.
            processes.append((pid, _full_image_path(pid) or entry.szExeFile))
            if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        _kernel32.CloseHandle(snapshot)
    return processes


def find_process(match: Dict[str, Any]) -> Optional[ProcessInfo]:
    """Locate the running CLIP STUDIO PAINT process, if there is one."""
    wanted_names = [n.lower() for n in match.get("name_contains", [])]

    fallback: Optional[ProcessInfo] = None
    for pid, executable in list_processes():
        lowered = executable.lower().replace("\\", "/")
        if not any(name in lowered for name in wanted_names):
            continue
        info = ProcessInfo(pid=pid, executable=executable)
        if "helper" in lowered or "crashpad" in lowered or "subprocess" in lowered:
            fallback = fallback or info
            continue
        return info
    return fallback


def frontmost_pid() -> Optional[int]:
    """PID owning the foreground window."""
    _require_windows()
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) or None


def idle_from_ticks(now_ticks: int, last_input_ticks: int) -> float:
    """Idle seconds from two 32-bit tick counts, tolerating the 49.7 day wrap."""
    return ((now_ticks - last_input_ticks) & _TICK_MASK) / 1000.0


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input, system wide."""
    _require_windows()
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return idle_from_ticks(int(_kernel32.GetTickCount()), int(info.dwTime))


def candidate_install_paths() -> List[str]:
    """Where CLIP STUDIO PAINT is typically installed on Windows."""
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    patterns = []
    for root in roots:
        if not root:
            continue
        base = os.path.join(root, "CELSYS")
        patterns.extend(
            [
                os.path.join(base, "CLIPStudioPaint.exe"),
                os.path.join(base, "*", "CLIPStudioPaint.exe"),
                os.path.join(base, "*", "*", "CLIPStudioPaint.exe"),
            ]
        )
    return patterns


def find_app_path() -> Optional[str]:
    """Where CLIP STUDIO PAINT is installed, if it can be located."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg

        key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\CLIPStudioPaint.exe"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                    if value and os.path.exists(value):
                        return str(value)
            except OSError:
                continue
    except ImportError:
        pass

    import glob

    for pattern in candidate_install_paths():
        for candidate in glob.glob(pattern):
            return candidate
    return None


# -- document discovery ---------------------------------------------------


def window_titles(pid: int, timeout: float = 5.0) -> List[str]:
    """Visible top-level window titles for a process, focused window first."""
    _require_windows()
    foreground = _user32.GetForegroundWindow()
    found: List[Tuple[Any, str]] = []

    def _collect(hwnd, _lparam):
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        text = buffer.value.strip()
        if text:
            found.append((hwnd, text))
        return True

    # The callback must stay referenced for the duration of the enumeration.
    callback = _WNDENUMPROC(_collect)
    _user32.EnumWindows(callback, 0)

    # The focused window is the active canvas, so let it win any tie.
    found.sort(key=lambda item: item[0] != foreground)
    return [text for _, text in found]


def open_document_paths(pid: int, extensions: Sequence[str], timeout: float = 5.0) -> List[str]:
    """Not supported on Windows: there is no built-in equivalent of lsof."""
    return []


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
            except RuntimeError as exc:
                notes.append("window_title failed: {}".format(exc))
                continue
            document = pick_document_from_titles(titles, extensions, ignore_titles)
            if document:
                return document
            if not titles:
                notes.append("window_title found no windows (is a canvas open?)")

        elif strategy == "open_files":
            notes.append(
                "open_files is not available on Windows; window_title needs no "
                "permission here, so it is not needed"
            )

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
