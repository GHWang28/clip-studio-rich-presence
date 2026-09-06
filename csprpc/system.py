"""Picks the backend for the current operating system.

Everything above this module talks to ``system`` rather than to ``macos`` or
``windows`` directly, so the tracking, presence and CLI layers stay platform
agnostic.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Optional

from csprpc.model import (  # noqa: F401 - re-exported for callers
    DocumentInfo,
    Observation,
    PermissionDenied,
    ProcessInfo,
    clean_title,
    pick_document_from_titles,
)


def _load_backend() -> Optional[Any]:
    if sys.platform == "darwin":
        from csprpc import macos

        return macos
    if sys.platform == "win32":
        from csprpc import windows

        return windows
    return None


backend = _load_backend()
IS_SUPPORTED = backend is not None
PLATFORM = backend.PLATFORM if backend else sys.platform


def _unsupported(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError(
        "csprpc supports macOS and Windows; this is {}".format(sys.platform)
    )


def _bind(name: str) -> Callable[..., Any]:
    return getattr(backend, name) if backend else _unsupported


observe = _bind("observe")
find_process = _bind("find_process")
frontmost_pid = _bind("frontmost_pid")
idle_seconds = _bind("idle_seconds")
window_titles = _bind("window_titles")
open_document_paths = _bind("open_document_paths")
detect_document = _bind("detect_document")
find_app_path = _bind("find_app_path")
list_processes = _bind("list_processes")
run_command = _bind("run_command")

NEEDS_WINDOW_PERMISSION = getattr(backend, "NEEDS_WINDOW_PERMISSION", False)
WINDOW_PERMISSION_NAME = getattr(backend, "WINDOW_PERMISSION_NAME", None)
WINDOW_PERMISSION_HINT = getattr(backend, "WINDOW_PERMISSION_HINT", "")
