"""Turning an observation into a Discord activity, and the main run loop."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from csprpc import system
from csprpc.discord_ipc import (
    MIN_UPDATE_INTERVAL,
    DiscordClosed,
    DiscordIPC,
    DiscordNotRunning,
    IPCError,
)
from csprpc.tracker import Tracker, humanize

log = logging.getLogger("csprpc")

# Discord rejects details/state outside this range.
_MAX_FIELD = 128
_MIN_FIELD = 2

# The elapsed clock's start drifts while time is paused; ignore small moves so
# we are not burning rate limit budget on a one second difference.
_TIMESTAMP_TOLERANCE = 10.0

_RECONNECT_MIN = 5.0
_RECONNECT_MAX = 60.0


class _SafeDict(dict):
    """Leaves unknown placeholders visible instead of raising KeyError."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass
class Snapshot:
    """Everything the presence layer needs to describe the current moment."""

    observation: system.Observation
    active: bool
    kind: str  # working | idle | no_document
    document_key: Optional[str]


def evaluate(config: Dict[str, Any], observation: system.Observation) -> Snapshot:
    """Decide whether time should accrue and which wording applies."""
    idle_timeout = float(config.get("idle_timeout_seconds", 300))
    require_frontmost = bool(config.get("require_frontmost", False))

    is_idle = idle_timeout > 0 and observation.idle_seconds >= idle_timeout
    focused = observation.frontmost or not require_frontmost
    active = observation.running and focused and not is_idle

    if not observation.running:
        kind = "not_running"
    elif is_idle or not focused:
        kind = "idle"
    elif observation.document is None:
        kind = "no_document"
    else:
        kind = "working"

    document_key = observation.document.name if observation.document else None
    return Snapshot(observation=observation, active=active, kind=kind, document_key=document_key)


def template_values(
    config: Dict[str, Any],
    tracker: Tracker,
    snapshot: Snapshot,
) -> Dict[str, str]:
    """The placeholders available inside presence templates."""
    privacy = config.get("privacy", {})
    document = snapshot.observation.document

    if document is None:
        doc_name = "Untitled"
        stem, ext = "Untitled", ""
    elif not privacy.get("show_file_name", True):
        doc_name = str(privacy.get("redacted_name", "a drawing"))
        stem, ext = doc_name, ""
    else:
        doc_name = document.display_name(hide_extension=bool(privacy.get("hide_extension", False)))
        base, _, extension = document.name.rpartition(".")
        stem = base or document.name
        ext = extension if base else ""

    key = snapshot.document_key
    return _SafeDict(
        doc=doc_name,
        file=doc_name,
        stem=stem,
        ext=ext,
        modified=" *" if (document and document.modified) else "",
        file_time=humanize(tracker.session_file_seconds(key)),
        file_time_total=humanize(tracker.lifetime_file_seconds(key)),
        session_time=humanize(tracker.session_seconds),
        today_time=humanize(tracker.today_seconds()),
        total_time=humanize(tracker.total_seconds()),
        app="CLIP STUDIO PAINT",
    )


def _render(template: str, values: Dict[str, str]) -> Optional[str]:
    try:
        text = str(template).format_map(values).strip()
    except (ValueError, IndexError):
        # A malformed template should degrade, not take the daemon down.
        return None
    if len(text) < _MIN_FIELD:
        return None
    return text[:_MAX_FIELD]


def _elapsed_start(config: Dict[str, Any], tracker: Tracker, snapshot: Snapshot) -> Optional[int]:
    mode = config.get("presence", {}).get("elapsed", "file")
    now = time.time()
    if mode == "none":
        return None
    if mode == "session":
        accrued = tracker.session_seconds
    elif mode == "today":
        accrued = tracker.today_seconds()
    else:
        accrued = tracker.session_file_seconds(snapshot.document_key)
    # Discord counts up from this instant, so backdate it by the time already
    # banked. Paused time is excluded because it was never banked.
    return int(now - accrued)


def build_activity(
    config: Dict[str, Any],
    tracker: Tracker,
    snapshot: Snapshot,
) -> Optional[Dict[str, Any]]:
    """The activity payload to send, or None to show nothing at all."""
    if snapshot.kind == "not_running":
        return None
    if snapshot.kind == "idle" and config.get("clear_presence_when_idle", False):
        return None

    presence = config.get("presence", {})
    templates = presence.get("templates", {})
    template_key = snapshot.kind if snapshot.kind in templates else "working"
    template = templates.get(template_key, {})

    values = template_values(config, tracker, snapshot)
    activity: Dict[str, Any] = {"type": 0}

    details = _render(template.get("details", ""), values)
    if details:
        activity["details"] = details
    state = _render(template.get("state", ""), values)
    if state:
        activity["state"] = state

    start = _elapsed_start(config, tracker, snapshot)
    if start is not None:
        activity["timestamps"] = {"start": start}

    assets: Dict[str, str] = {}
    if presence.get("large_image"):
        assets["large_image"] = str(presence["large_image"])
    if presence.get("large_text"):
        assets["large_text"] = str(presence["large_text"])[:_MAX_FIELD]
    working = snapshot.kind == "working" or snapshot.kind == "no_document"
    small_image = presence.get("small_image_active" if working else "small_image_idle")
    small_text = presence.get("small_text_active" if working else "small_text_idle")
    if small_image:
        assets["small_image"] = str(small_image)
    if small_text:
        assets["small_text"] = str(small_text)[:_MAX_FIELD]
    if assets:
        activity["assets"] = assets

    buttons = [
        {"label": str(b["label"])[:32], "url": str(b["url"])}
        for b in (presence.get("buttons") or [])[:2]
        if isinstance(b, dict) and b.get("label") and b.get("url")
    ]
    if buttons:
        activity["buttons"] = buttons

    return activity


def activities_equivalent(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    """Compare payloads, tolerating tiny drift in the elapsed start time."""
    if a is None or b is None:
        return a is b or (a is None and b is None)

    a_start = (a.get("timestamps") or {}).get("start")
    b_start = (b.get("timestamps") or {}).get("start")
    a_rest = {k: v for k, v in a.items() if k != "timestamps"}
    b_rest = {k: v for k, v in b.items() if k != "timestamps"}
    if a_rest != b_rest:
        return False
    if a_start is None or b_start is None:
        return a_start == b_start
    return abs(a_start - b_start) <= _TIMESTAMP_TOLERANCE


class PresenceDaemon:
    """Polls CLIP STUDIO PAINT and keeps the Discord presence in step."""

    def __init__(
        self,
        config: Dict[str, Any],
        tracker: Tracker,
        dry_run: bool = False,
        on_update: Optional[Callable[[Snapshot, Optional[Dict[str, Any]]], None]] = None,
    ):
        self.config = config
        self.tracker = tracker
        self.dry_run = dry_run
        self.on_update = on_update
        self.poll_interval = float(config.get("poll_interval_seconds", 5.0))
        self.max_delta = self.poll_interval * 3 + 5.0

        self._ipc: Optional[DiscordIPC] = None
        self._last_sent: Optional[Dict[str, Any]] = None
        # None means "nothing sent yet", which must not be confused with time
        # zero: time.monotonic() starts near zero at process start on macOS.
        self._last_send_mono: Optional[float] = None
        self._pending: Optional[Dict[str, Any]] = None
        self._has_pending = False
        self._next_connect_mono = 0.0
        self._reconnect_delay = _RECONNECT_MIN
        self._stop_running_since: Optional[float] = None
        self._seen_notes: set = set()
        self.stopped = False

    # -- one iteration -----------------------------------------------------

    def step(self) -> Snapshot:
        observation = system.observe(self.config)
        snapshot = evaluate(self.config, observation)

        self.tracker.tick(snapshot.active, snapshot.document_key, self.max_delta)
        if observation.document and observation.document.path:
            self.tracker.note_path(observation.document.name, observation.document.path)
        self.tracker.maybe_save()

        for note in observation.notes:
            if note not in self._seen_notes:
                self._seen_notes.add(note)
                log.warning("%s", note)

        target = build_activity(self.config, self.tracker, snapshot)
        target = self._apply_linger(snapshot, target)

        if self.on_update:
            self.on_update(snapshot, target)

        if not self.dry_run:
            self._sync(target)

        return snapshot

    def _apply_linger(
        self, snapshot: Snapshot, target: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Hold the last presence briefly across a quick app restart."""
        linger = float(self.config.get("linger_seconds", 0))
        if snapshot.kind != "not_running":
            self._stop_running_since = None
            return target
        if linger <= 0 or self._last_sent is None:
            return None
        if self._stop_running_since is None:
            self._stop_running_since = time.monotonic()
        if time.monotonic() - self._stop_running_since < linger:
            return self._last_sent
        return None

    def _sync(self, target: Optional[Dict[str, Any]]) -> None:
        """Reconcile Discord with the target activity, respecting rate limits."""
        if target is None:
            if self._ipc is not None and self._last_sent is not None:
                try:
                    self._ipc.clear_activity()
                    log.info("cleared presence")
                except IPCError as exc:
                    log.info("could not clear presence: %s", exc)
                self._last_sent = None
            self._disconnect()
            self._has_pending = False
            return

        if not self._connect_if_needed():
            self._pending, self._has_pending = target, True
            return

        assert self._ipc is not None
        try:
            self._ipc.pump()
        except DiscordClosed as exc:
            self._handle_disconnect(exc)
            self._pending, self._has_pending = target, True
            return

        if not activities_equivalent(target, self._last_sent):
            self._pending, self._has_pending = target, True

        if not self._has_pending:
            return
        if (
            self._last_send_mono is not None
            and time.monotonic() - self._last_send_mono < MIN_UPDATE_INTERVAL
        ):
            return  # Coalesce; the newest pending activity wins.

        payload = self._pending
        try:
            self._ipc.set_activity(payload)
        except DiscordClosed as exc:
            self._handle_disconnect(exc)
            return
        except IPCError as exc:
            log.warning("presence update rejected: %s", exc)
            self._has_pending = False
            return

        self._last_sent = payload
        self._last_send_mono = time.monotonic()
        self._has_pending = False
        log.info(
            "presence: %s | %s",
            (payload or {}).get("details", "-"),
            (payload or {}).get("state", "-"),
        )

    # -- connection --------------------------------------------------------

    def _connect_if_needed(self) -> bool:
        if self._ipc is not None and self._ipc.connected:
            return True
        if time.monotonic() < self._next_connect_mono:
            return False

        client_id = str(self.config.get("client_id", "")).strip()
        if not client_id:
            self._schedule_reconnect()
            return False

        ipc = DiscordIPC(client_id)
        try:
            ipc.connect()
        except DiscordNotRunning as exc:
            log.debug("Discord not available: %s", exc)
            self._schedule_reconnect()
            return False
        except DiscordClosed as exc:
            log.warning("Discord refused the connection: %s", exc)
            self._schedule_reconnect()
            return False

        self._ipc = ipc
        self._reconnect_delay = _RECONNECT_MIN
        # A fresh connection has no presence, so anything we cached is gone
        # and the next update should go out immediately.
        self._last_sent = None
        self._last_send_mono = None
        user = ipc.user or {}
        log.info(
            "connected to Discord as %s via %s",
            user.get("username", "unknown user"),
            ipc.endpoint,
        )
        return True

    def _handle_disconnect(self, exc: Exception) -> None:
        log.warning("lost Discord connection: %s", exc)
        self._disconnect()
        self._schedule_reconnect()

    def _disconnect(self) -> None:
        if self._ipc is not None:
            self._ipc.close()
            self._ipc = None

    def _schedule_reconnect(self) -> None:
        self._next_connect_mono = time.monotonic() + self._reconnect_delay
        self._reconnect_delay = min(self._reconnect_delay * 2, _RECONNECT_MAX)

    # -- lifecycle ---------------------------------------------------------

    def run_forever(self) -> None:
        log.info("watching for CLIP STUDIO PAINT (poll every %ss)", self.poll_interval)
        try:
            while not self.stopped:
                try:
                    self.step()
                except Exception:  # noqa: BLE001 - a bad poll must not kill the daemon
                    log.exception("poll failed; continuing")
                # Wake up promptly on stop rather than sleeping the full interval.
                deadline = time.monotonic() + self.poll_interval
                while not self.stopped and time.monotonic() < deadline:
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        finally:
            self.shutdown()

    def stop(self) -> None:
        self.stopped = True

    def shutdown(self) -> None:
        self.tracker.save(force=True)
        if self._ipc is not None and not self.dry_run:
            try:
                self._ipc.clear_activity()
            except IPCError:
                pass
        self._disconnect()
        log.info("stopped; %s tracked this session", humanize(self.tracker.session_seconds))
