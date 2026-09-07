"""Windows backend: observing CLIP STUDIO PAINT through the Win32 API.

Everything here goes through ``ctypes`` against DLLs that are part of Windows,
so there is nothing to install. Unlike macOS, reading another application's window titles needs no privacy
permission. Recent CLIP STUDIO PAINT builds leave the canvas name out of
every Win32 title, so we also look at CELSYS's live ownership file and at
artwork files the process has mapped.

This module imports cleanly on any platform; the Win32 entry points are only
bound when actually running on Windows, and calling them elsewhere raises.
"""

from __future__ import annotations

import os
import re
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

# Install and cache paths, not the user's canvas.
_OPEN_FILE_EXCLUDES = (
    "/windows/",
    "/program files/",
    "/program files (x86)/",
    "/appdata/local/",
    "/appdata/roaming/celsys",
    "/temp/",
    "/fonts/",
)

# CELSYS writes the canvas currently open in PAINT here. Recent builds keep
# the file name out of the window title, so this is the reliable source.
_OWNERSHIP_RELATIVE = os.path.join("CELSYS", "promenade", "ownership", "owner.txt")

# A drive path or UNC path at the end of an ownership line. The fields before
# it are GUIDs, and the path itself contains colons (``C:\...``).
_OWNERSHIP_PATH_RE = re.compile(r"(?:(?<=:)|^)(?:[A-Za-z]:\\|\\\\)[^\r\n]+$")

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
    _user32.EnumChildWindows.restype = wintypes.BOOL
    _user32.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
    _user32.GetLastInputInfo.restype = wintypes.BOOL
    _user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    _user32.GetAsyncKeyState.restype = wintypes.SHORT
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _VK_LBUTTON = 0x01

    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _psapi.GetMappedFileNameW.restype = wintypes.DWORD
    _psapi.GetMappedFileNameW.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPWSTR, wintypes.DWORD
    ]

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    _kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    _kernel32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        ctypes.POINTER(MEMORY_BASIC_INFORMATION),
        ctypes.c_size_t,
    ]
    _kernel32.QueryDosDeviceW.restype = wintypes.DWORD
    _kernel32.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]

    PROCESS_VM_READ = 0x0010
    PROCESS_QUERY_INFORMATION = 0x0400
    MEM_COMMIT = 0x1000
    MEM_MAPPED = 0x40000


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


def find_processes(match: Dict[str, Any]) -> List[ProcessInfo]:
    """Every running CLIP STUDIO PAINT process, main executable first."""
    wanted_names = [n.lower() for n in match.get("name_contains", [])]
    main: List[ProcessInfo] = []
    helpers: List[ProcessInfo] = []
    for pid, executable in list_processes():
        lowered = executable.lower().replace("\\", "/")
        if not any(name in lowered for name in wanted_names):
            continue
        info = ProcessInfo(pid=pid, executable=executable)
        if "helper" in lowered or "crashpad" in lowered or "subprocess" in lowered:
            helpers.append(info)
        else:
            main.append(info)
    return main or helpers


def find_process(match: Dict[str, Any]) -> Optional[ProcessInfo]:
    """Locate the running CLIP STUDIO PAINT process, if there is one."""
    found = find_processes(match)
    return found[0] if found else None


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


def pointer_is_down() -> bool:
    """True while the pen or left mouse button is held.

    Tablets usually report contact as the left button. No hook is installed.
    """
    _require_windows()
    return bool(_user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000)


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


def _window_text(hwnd: Any) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def window_titles(pid: int, timeout: float = 5.0) -> List[str]:
    """Window titles for a process, focused window first.

    Recent CLIP STUDIO PAINT builds leave the top-level title as just
    "CLIP STUDIO PAINT" and put the canvas name on a child window, so
    children are included.
    """
    _require_windows()
    foreground = _user32.GetForegroundWindow()
    top: List[Tuple[Any, str]] = []

    def _collect_top(hwnd, _lparam):
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not _user32.IsWindowVisible(hwnd):
            return True
        top.append((hwnd, _window_text(hwnd)))
        return True

    callback = _WNDENUMPROC(_collect_top)
    _user32.EnumWindows(callback, 0)
    top.sort(key=lambda item: item[0] != foreground)

    titles: List[str] = []
    seen = set()

    def _add(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            titles.append(text)

    for hwnd, text in top:
        _add(text)
        children: List[str] = []

        def _collect_child(child, _lparam, bucket=children):
            child_text = _window_text(child)
            if child_text:
                bucket.append(child_text)
            return True

        child_cb = _WNDENUMPROC(_collect_child)
        _user32.EnumChildWindows(hwnd, child_cb, 0)
        for child_text in children:
            _add(child_text)
    return titles


def nt_to_dos(device_path: str, drives: Optional[Dict[str, str]] = None) -> str:
    """Turn an NT device path into a DOS path using a drive map.

    GetMappedFileNameW returns ``\\Device\\HarddiskVolume3\\Users\\...``.
    ``drives`` maps device prefixes to ``C:``-style roots; omitted on
    Windows, it is read from the system.
    """
    if not device_path:
        return device_path
    mapping = drives if drives is not None else _query_drive_map()
    for device, drive in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
        if device_path.startswith(device):
            rest = device_path[len(device):]
            if not rest or rest.startswith("\\"):
                return drive + rest
    return device_path


def _query_drive_map() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if not IS_WINDOWS:
        return mapping
    buffer = ctypes.create_unicode_buffer(1024)
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = letter + ":"
        if _kernel32.QueryDosDeviceW(drive, buffer, 1024):
            mapping[buffer.value] = drive
    return mapping


def default_ownership_path() -> Optional[str]:
    """Where CLIP STUDIO PAINT records the canvas it currently has open."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, _OWNERSHIP_RELATIVE)


def parse_ownership_line(line: str) -> Optional[str]:
    """Extract a document path from one CELSYS ``owner.txt`` line.

    A typical line is colon-separated GUIDs followed by a Windows path::

        4:<window-class>:<session>:<document>:C:\\Users\\me\\Art\\Summer.clip
    """
    text = (line or "").strip()
    if not text:
        return None
    match = _OWNERSHIP_PATH_RE.search(text)
    return match.group(0).rstrip() if match else None


def read_ownership_paths(path: Optional[str] = None) -> List[str]:
    """Document paths from the CELSYS ownership file, in file order."""
    target = default_ownership_path() if path is None else path
    if not target or not os.path.isfile(target):
        return []
    try:
        with open(target, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return []
    found: List[str] = []
    for line in text.splitlines():
        item = parse_ownership_line(line)
        if item and item not in found:
            found.append(item)
    return found


def _mapped_document_paths(pid: int) -> List[str]:
    """Artwork files mapped into the process. Windows only."""
    if not IS_WINDOWS:
        return []
    rights = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION
    handle = _kernel32.OpenProcess(rights, False, pid)
    if not handle:
        return []
    paths: List[str] = []
    try:
        address = 0
        info = MEMORY_BASIC_INFORMATION()
        while True:
            got = _kernel32.VirtualQueryEx(
                handle, ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)
            )
            if not got:
                break
            base = info.AllocationBase
            if info.State == MEM_COMMIT and info.Type == MEM_MAPPED and base:
                name = ctypes.create_unicode_buffer(32768)
                if _psapi.GetMappedFileNameW(handle, ctypes.c_void_p(base), name, 32768):
                    paths.append(nt_to_dos(name.value))
            region = int(info.RegionSize or 0)
            nxt = address + region
            if nxt <= address:
                break
            address = nxt
    finally:
        _kernel32.CloseHandle(handle)
    return paths


def open_document_paths(pid: int, extensions: Sequence[str], timeout: float = 5.0) -> List[str]:
    """Artwork files CLIP STUDIO PAINT currently has open, newest first.

    Recent builds keep the canvas out of every window title. The ownership
    file CELSYS writes for the open document is the reliable source; mapped
    memory is a fallback. Unsaved canvases are not on disk and cannot be
    found this way.
    """
    owned = filter_document_paths(read_ownership_paths(), extensions, _OPEN_FILE_EXCLUDES)
    if owned:
        return owned
    return filter_document_paths(_mapped_document_paths(pid), extensions, _OPEN_FILE_EXCLUDES)


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
            else:
                notes.append(
                    "window titles are only chrome {}; recent CLIP STUDIO PAINT "
                    "builds do not put the canvas name in the window title".format(
                        [t for t in titles[:6]]
                    )
                )

        elif strategy == "open_files":
            paths = open_document_paths(pid, extensions)
            if paths:
                name = paths[0].replace("\\", "/").rsplit("/", 1)[-1]
                return DocumentInfo(
                    name=name,
                    path=paths[0],
                    source="open_files",
                )
            notes.append("open_files found no artwork files")

    return None


def observe(config: Dict[str, Any]) -> Observation:
    """Take one full snapshot of the system."""
    result = Observation()

    try:
        processes = find_processes(config.get("process_match", {}))
    except RuntimeError as exc:
        result.notes.append(str(exc))
        return result

    if not processes:
        return result

    result.running = True
    result.process = processes[0]
    front = frontmost_pid()
    result.frontmost = front in {item.pid for item in processes}
    result.idle_seconds = idle_seconds()
    document_config = config.get("document", {})
    for process in processes:
        document = detect_document(process.pid, document_config, result.notes)
        if document:
            result.process = process
            result.document = document
            break
    return result
