"""Accumulating and persisting how long was spent in which file."""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1


def _empty_store() -> Dict[str, Any]:
    return {"version": SCHEMA_VERSION, "files": {}, "days": {}}


def humanize(seconds: float) -> str:
    """Render a duration the way a person would say it."""
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return "{}h {}m".format(hours, minutes)
    if minutes:
        return "{}m".format(minutes)
    return "{}s".format(secs)


def humanize_precise(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return "{:d}:{:02d}:{:02d}".format(hours, minutes, secs)


class Tracker:
    """Tracks time for the current session and merges it into lifetime totals.

    Time only accrues while ``tick`` is called with ``active=True``, so idle
    periods and time spent in other apps simply never get counted.
    """

    def __init__(
        self,
        path: Path,
        save_interval: float = 60.0,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.path = path
        self.save_interval = save_interval
        self.clock = clock or time.monotonic
        self.data = self._load()
        self.session_seconds = 0.0
        self.session_files: Dict[str, float] = {}
        self.session_started = time.time()
        self._last_mono: Optional[float] = None
        self._last_save_mono = self.clock()
        self._dirty = False

    # -- persistence -------------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return _empty_store()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt stats file must never stop the presence from working.
            backup = self.path.with_suffix(".corrupt.json")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return _empty_store()
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            return _empty_store()
        data.setdefault("files", {})
        data.setdefault("days", {})
        return data

    def save(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
            self._last_save_mono = self.clock()
        except OSError:
            # Losing a stats flush is not worth crashing the presence over.
            pass

    def maybe_save(self) -> None:
        if self._dirty and self.clock() - self._last_save_mono >= self.save_interval:
            self.save()

    # -- accrual -----------------------------------------------------------

    def tick(self, active: bool, document_key: Optional[str], max_delta: float) -> float:
        """Advance the clocks and return the seconds credited by this call."""
        now_mono = self.clock()
        previous = self._last_mono
        self._last_mono = now_mono

        if previous is None:
            return 0.0

        delta = now_mono - previous
        if delta <= 0:
            return 0.0
        # A long gap means the machine slept or the loop stalled; do not credit
        # the user with hours of drawing they did not do.
        delta = min(delta, max_delta)

        if not active:
            return 0.0

        self.session_seconds += delta

        day = self.data["days"].setdefault(
            self.today_key(), {"total_seconds": 0.0, "files": {}}
        )
        day["total_seconds"] = day.get("total_seconds", 0.0) + delta

        if document_key:
            self.session_files[document_key] = self.session_files.get(document_key, 0.0) + delta

            entry = self.data["files"].setdefault(
                document_key, {"total_seconds": 0.0, "first_seen": time.time()}
            )
            entry["total_seconds"] = entry.get("total_seconds", 0.0) + delta
            entry["last_seen"] = time.time()

            day["files"][document_key] = day["files"].get(document_key, 0.0) + delta

        self._dirty = True
        return delta

    def note_path(self, document_key: str, path: Optional[str]) -> None:
        """Remember the last known full path for a document."""
        if not path:
            return
        entry = self.data["files"].setdefault(
            document_key, {"total_seconds": 0.0, "first_seen": time.time()}
        )
        if entry.get("path") != path:
            entry["path"] = path
            self._dirty = True

    # -- queries -----------------------------------------------------------

    @staticmethod
    def today_key(when: Optional[float] = None) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(when))

    def session_file_seconds(self, document_key: Optional[str]) -> float:
        if not document_key:
            return 0.0
        return self.session_files.get(document_key, 0.0)

    def lifetime_file_seconds(self, document_key: Optional[str]) -> float:
        if not document_key:
            return 0.0
        return float(self.data["files"].get(document_key, {}).get("total_seconds", 0.0))

    def today_seconds(self) -> float:
        return float(self.data["days"].get(self.today_key(), {}).get("total_seconds", 0.0))

    def total_seconds(self) -> float:
        return sum(float(d.get("total_seconds", 0.0)) for d in self.data["days"].values())

    def files_by_time(self, limit: Optional[int] = None) -> List[Tuple[str, Dict[str, Any]]]:
        items = sorted(
            self.data["files"].items(),
            key=lambda kv: float(kv[1].get("total_seconds", 0.0)),
            reverse=True,
        )
        return items[:limit] if limit else items

    def days_by_date(self, limit: Optional[int] = None) -> List[Tuple[str, Dict[str, Any]]]:
        items = sorted(self.data["days"].items(), reverse=True)
        return items[:limit] if limit else items

    def today_file_count(self) -> int:
        """How many distinct files have accrued time today."""
        day = self.data["days"].get(self.today_key(), {})
        return len(day.get("files") or {})

    def top_file_today(self) -> Optional[str]:
        """The file with the most time today, if any."""
        files = (self.data["days"].get(self.today_key(), {}) or {}).get("files") or {}
        if not files:
            return None
        return max(files.items(), key=lambda kv: float(kv[1]))[0]

    def drawing_streak(self) -> int:
        """Consecutive local days with tracked time.

        Today counts if any time has been banked; otherwise the run is the
        one ending yesterday, so the streak does not drop to zero at midnight
        before you start drawing.
        """
        days = self.data.get("days") or {}
        cursor = date.today()
        if self.today_seconds() <= 0:
            cursor -= timedelta(days=1)
        streak = 0
        while float((days.get(cursor.isoformat()) or {}).get("total_seconds", 0.0)) > 0:
            streak += 1
            cursor -= timedelta(days=1)
        return streak
