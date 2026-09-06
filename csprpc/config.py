"""Configuration loading, merging and persistence.

The config lives in a JSON file so that the tool keeps working on the Python
that ships with macOS (3.9), which has no ``tomllib``.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_DIR_NAME = "ClipStudioRichPresence"

# The Discord application that gives the presence its "CLIP STUDIO PAINT" name
# and its artwork. It is the same for every install, so there is nothing to
# register; override client_id only if you want to use your own application.
DEFAULT_CLIENT_ID = "1350211065446924348"

DEFAULTS: Dict[str, Any] = {
    "client_id": DEFAULT_CLIENT_ID,
    # How often to look at CLIP STUDIO PAINT, in seconds.
    "poll_interval_seconds": 5.0,
    # No keyboard/mouse input for this long counts as "idle": the timer pauses
    # and the presence switches to its idle wording.
    "idle_timeout_seconds": 300,
    # Only accrue time while CLIP STUDIO PAINT is the frontmost application.
    "require_frontmost": False,
    # Remove the Discord presence entirely while idle, instead of showing
    # the idle wording.
    "clear_presence_when_idle": False,
    # Keep showing a presence for this long after CLIP STUDIO PAINT quits,
    # so a quick restart does not flicker the status off and on.
    "linger_seconds": 0,
    "process_match": {
        # bundle_ids is macOS only; name_contains is matched against the
        # executable path on both platforms and covers CLIPStudioPaint.exe.
        "bundle_ids": ["jp.co.celsys.clipstudiopaint"],
        "name_contains": ["clipstudiopaint", "clip studio paint"],
    },
    "document": {
        # Tried in order until one yields a document name.
        #   window_title -> reads the canvas window title. Needs Accessibility
        #                   on macOS; needs no permission on Windows. Recent
        #                   Windows builds leave the canvas name out of the
        #                   title, so this often finds nothing there.
        #   open_files   -> inspects files the process has open. On macOS that
        #                   is lsof. On Windows it is CELSYS's ownership file,
        #                   then memory-mapped artwork as a fallback.
        "strategies": ["window_title", "open_files"],
        "extensions": [
            ".clip",
            ".psd",
            ".psb",
            ".png",
            ".jpg",
            ".jpeg",
            ".tif",
            ".tiff",
            ".bmp",
            ".webp",
            ".cmc",
            ".lip",
        ],
        # Window titles that are chrome rather than documents.
        "ignore_titles": [
            "clip studio paint",
            "clip studio",
            "preferences",
            "settings",
            "tool property",
            "sub tool",
            "sub tool detail",
            "layer",
            "layer property",
            "navigator",
            "color wheel",
            "color set",
            "color slider",
            "timeline",
            "history",
            "material",
            "quick access",
            "auto action",
            "animation cells",
            "search",
            "item bank",
        ],
    },
    "privacy": {
        # Turn off to never send document names to Discord.
        "show_file_name": True,
        # Show "Sketch" instead of "Sketch.clip".
        "hide_extension": False,
        # Used in place of the name when show_file_name is false.
        "redacted_name": "a drawing",
    },
    "presence": {
        # Which clock Discord counts up from:
        #   file | session | today | none
        "elapsed": "file",
        "large_image": "csp",
        "large_text": "CLIP STUDIO PAINT",
        "small_image_active": "brush",
        "small_image_idle": "idle",
        "small_text_active": "Drawing",
        "small_text_idle": "Away",
        # Up to two: [{"label": "...", "url": "https://..."}]
        "buttons": [],
        "templates": {
            "working": {
                "details": "{doc}{modified}",
                "state": "{today_time} today",
            },
            "idle": {
                "details": "{doc}{modified}",
                "state": "Away \u00b7 {today_time} today",
            },
            "no_document": {
                "details": "No canvas open",
                "state": "{today_time} today",
            },
        },
    },
    "stats": {
        # How often the on-disk totals are flushed.
        "save_interval_seconds": 60,
    },
    "ui": {
        # Window colours only; never sent to Discord.
        "dark_mode": False,
    },
}

# Every leaf setting, as a dotted path -> the type it should be parsed as.
# Used by `csprpc config set` to coerce command line strings.
_SCALAR_TYPES: Dict[str, type] = {
    "client_id": str,
    "poll_interval_seconds": float,
    "idle_timeout_seconds": float,
    "require_frontmost": bool,
    "clear_presence_when_idle": bool,
    "linger_seconds": float,
    "privacy.show_file_name": bool,
    "privacy.hide_extension": bool,
    "privacy.redacted_name": str,
    "presence.elapsed": str,
    "presence.large_image": str,
    "presence.large_text": str,
    "presence.small_image_active": str,
    "presence.small_image_idle": str,
    "presence.small_text_active": str,
    "presence.small_text_idle": str,
    "presence.templates.working.details": str,
    "presence.templates.working.state": str,
    "presence.templates.idle.details": str,
    "presence.templates.idle.state": str,
    "presence.templates.no_document.details": str,
    "presence.templates.no_document.state": str,
    "stats.save_interval_seconds": float,
    "ui.dark_mode": bool,
}

VALID_ELAPSED = ("file", "session", "today", "none")
VALID_STRATEGIES = ("window_title", "open_files")


def data_dir() -> Path:
    """Directory holding the config and the accumulated statistics."""
    override = os.environ.get("CSPRPC_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / APP_DIR_NAME
    return Path.home() / ".local" / "share" / APP_DIR_NAME


def config_path() -> Path:
    return data_dir() / "config.json"


def stats_path() -> Path:
    return data_dir() / "stats.json"


def log_path() -> Path:
    return data_dir() / "csprpc.log"


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read the config, filling in defaults for anything absent."""
    path = path or config_path()
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError("could not read {}: {}".format(path, exc))
    if not isinstance(raw, dict):
        raise ConfigError("{} must contain a JSON object".format(path))
    merged = _deep_merge(DEFAULTS, raw)
    # Configs written before an application ID shipped hold an empty string,
    # which would otherwise shadow the default.
    if not str(merged.get("client_id", "")).strip():
        merged["client_id"] = DEFAULT_CLIENT_ID
    return merged


def save(config: Dict[str, Any], path: Optional[Path] = None) -> Path:
    """Write the config, creating the parent directory if needed."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def get_dotted(config: Dict[str, Any], dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError("unknown setting: {}".format(dotted))
        node = node[part]
    return node


def set_dotted(config: Dict[str, Any], dotted: str, raw_value: Any) -> Any:
    """Set a scalar setting from a string, or from an already-typed value.

    The command line always supplies strings; the graphical interface hands
    over real bools and numbers straight from its widgets.
    """
    if dotted not in _SCALAR_TYPES:
        raise ConfigError(
            "{} is not settable from the command line; edit the config file "
            "directly with `csprpc config edit`".format(dotted)
        )
    value = _coerce(dotted, raw_value, _SCALAR_TYPES[dotted])
    if dotted == "client_id" and not str(value).strip():
        # Blanking it would silently fall back anyway; be explicit about it.
        value = DEFAULT_CLIENT_ID
    parts = dotted.split(".")
    node = config
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    validate(config)
    return value


def _coerce(dotted: str, raw: Any, kind: type) -> Any:
    if kind is bool:
        if isinstance(raw, bool):
            return raw
        lowered = str(raw).strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigError("{} expects true or false, got {!r}".format(dotted, raw))
    if kind is float:
        if isinstance(raw, bool):
            raise ConfigError("{} expects a number, got {!r}".format(dotted, raw))
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise ConfigError("{} expects a number, got {!r}".format(dotted, raw))
    return raw if isinstance(raw, str) else str(raw)


def validate(config: Dict[str, Any]) -> List[str]:
    """Return a list of human readable problems; empty means the config is sane.

    Raises ConfigError only for values that would crash the run loop.
    """
    problems: List[str] = []

    client_id = str(config.get("client_id", "")).strip()
    if not client_id:
        problems.append(
            "client_id is empty - restore the built-in one with "
            "`csprpc config set client_id {}`".format(DEFAULT_CLIENT_ID)
        )
    elif not client_id.isdigit():
        problems.append("client_id should be the numeric Application ID, got {!r}".format(client_id))

    poll = config.get("poll_interval_seconds", 5.0)
    if not isinstance(poll, (int, float)) or poll <= 0:
        raise ConfigError("poll_interval_seconds must be a positive number")
    if poll < 1:
        problems.append("poll_interval_seconds below 1 wastes CPU for no visible benefit")

    elapsed = config["presence"].get("elapsed")
    if elapsed not in VALID_ELAPSED:
        raise ConfigError(
            "presence.elapsed must be one of {}, got {!r}".format(", ".join(VALID_ELAPSED), elapsed)
        )

    strategies = config["document"].get("strategies") or []
    if not isinstance(strategies, list):
        raise ConfigError("document.strategies must be a list")
    unknown = [s for s in strategies if s not in VALID_STRATEGIES]
    if unknown:
        raise ConfigError(
            "unknown document.strategies entries: {} (valid: {})".format(
                ", ".join(map(str, unknown)), ", ".join(VALID_STRATEGIES)
            )
        )
    if not strategies:
        problems.append("document.strategies is empty, so no file name will ever be detected")

    buttons = config["presence"].get("buttons") or []
    if len(buttons) > 2:
        problems.append("Discord shows at most 2 buttons; extras are ignored")
    for button in buttons:
        if not isinstance(button, dict) or "label" not in button or "url" not in button:
            raise ConfigError('each presence.buttons entry needs a "label" and a "url"')

    return problems


def ensure_exists() -> Tuple[Path, bool]:
    """Create the config file with defaults if it is missing.

    Returns the path and whether it was just created.
    """
    path = config_path()
    if path.exists():
        return path, False
    save(copy.deepcopy(DEFAULTS), path)
    return path, True


class ConfigError(Exception):
    """Raised for configuration that cannot be used."""
