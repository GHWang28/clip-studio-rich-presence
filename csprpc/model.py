"""Platform-neutral types and document-name parsing.

Both the macOS and Windows backends produce these types, so everything above
this layer (time tracking, presence building, the CLI) is platform agnostic.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

# macOS appends this to a document window with unsaved changes; some CLIP
# STUDIO PAINT builds use a plain asterisk instead.
_MODIFIED_SUFFIXES = (" \u2014 Edited", " - Edited", " \u2014 edited", " - edited")

# Windows titles the canvas window "<file> - CLIP STUDIO PAINT".
_APP_TITLE_SUFFIXES = (
    " - CLIP STUDIO PAINT",
    " \u2014 CLIP STUDIO PAINT",
    " - Clip Studio Paint",
    " \u2014 Clip Studio Paint",
    " - CLIP STUDIO PAINT EX",
    " - CLIP STUDIO PAINT PRO",
    " - CLIP STUDIO PAINT DEBUT",
)


class PermissionDenied(Exception):
    """The operating system has not granted the access a strategy needs."""


@dataclass
class ProcessInfo:
    pid: int
    executable: str
    bundle_id: Optional[str] = None  # macOS only

    @property
    def app_name(self) -> str:
        match = re.search(r"/([^/]+)\.app/", self.executable)
        if match:
            return match.group(1)
        base = os.path.basename(self.executable.replace("\\", "/"))
        stem, _, extension = base.rpartition(".")
        return stem if stem and extension.lower() == "exe" else base


@dataclass
class DocumentInfo:
    name: str
    modified: bool = False
    path: Optional[str] = None
    source: str = "unknown"

    def display_name(self, hide_extension: bool = False) -> str:
        if hide_extension:
            stem, _, ext = self.name.rpartition(".")
            if stem and len(ext) <= 5:
                return stem
        return self.name


@dataclass
class Observation:
    """A single snapshot of what CLIP STUDIO PAINT is doing."""

    running: bool = False
    process: Optional[ProcessInfo] = None
    frontmost: bool = False
    idle_seconds: float = 0.0
    document: Optional[DocumentInfo] = None
    notes: List[str] = field(default_factory=list)


def clean_title(title: str) -> "tuple":
    """Strip decoration from a window title, reporting the unsaved marker."""
    text = title.strip()
    modified = False

    for suffix in _APP_TITLE_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)].strip()
            break

    for suffix in _MODIFIED_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            modified = True
            break

    if text.startswith("*"):
        text = text[1:].strip()
        modified = True
    if text.endswith("*"):
        text = text[:-1].strip()
        modified = True

    return text, modified


def pick_document_from_titles(
    titles: Sequence[str],
    extensions: Sequence[str],
    ignore_titles: Sequence[str],
) -> Optional[DocumentInfo]:
    """Choose the title that looks most like an open canvas.

    Titles are considered in the order given, so a backend that puts the
    focused window first will have its choice preferred among equals.
    """
    ignore = {t.strip().lower() for t in ignore_titles}
    suffixes = tuple(e.lower() for e in extensions)

    best: Optional[DocumentInfo] = None
    for raw in titles:
        text, modified = clean_title(raw)
        if not text or text.lower() in ignore:
            continue
        candidate = DocumentInfo(name=text, modified=modified, source="window_title")
        if text.lower().endswith(suffixes):
            return candidate  # A known extension is as certain as we get.
        if best is None:
            best = candidate
    return best


def filter_document_paths(paths: Sequence[str], extensions: Sequence[str],
                          excludes: Sequence[str]) -> List[str]:
    """Keep only plausible artwork files, newest first."""
    suffixes = tuple(e.lower() for e in extensions)
    seen = set()
    candidates = []
    for path in paths:
        lowered = path.lower().replace("\\", "/")
        if not lowered.endswith(suffixes):
            continue
        if any(token in lowered for token in excludes):
            continue
        if path in seen:
            continue
        seen.add(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        candidates.append((mtime, path))
    candidates.sort(reverse=True)
    return [path for _, path in candidates]
