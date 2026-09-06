"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from csprpc import __version__, config as config_module, system
from csprpc.discord_ipc import DiscordClosed, DiscordIPC, DiscordNotRunning, find_endpoint
from csprpc.presence import PresenceDaemon, Snapshot, evaluate
from csprpc.tracker import Tracker, humanize, humanize_precise

LAUNCH_AGENT_LABEL = "com.csprpc.presence"
SCHEDULED_TASK_NAME = "CSPRPC Presence"

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"

OK = "\u2713"
BAD = "\u2717"
WARN = "!"
# Windows cmd.exe is often cp1252, which cannot encode the ticks above.
OK_PLAIN = "OK"
BAD_PLAIN = "X"


def _status_mark(ok: Optional[bool]) -> str:
    """A tick, cross or warning that this stdout can actually print."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        OK.encode(encoding)
        ticks = True
    except (LookupError, UnicodeEncodeError):
        ticks = False
    if ok is True:
        return OK if ticks else OK_PLAIN
    if ok is None:
        return WARN
    return BAD if ticks else BAD_PLAIN


log = logging.getLogger("csprpc")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "verbose", False))

    if args.command is None:
        # Double-clicked, or run bare: open the window.
        args.command = "gui"
        args.handler = cmd_gui
        args.hidden = False

    if not system.IS_SUPPORTED and args.command != "config":
        print(
            "csprpc supports macOS and Windows; this is {}.".format(sys.platform),
            file=sys.stderr,
        )
        return 2

    try:
        return args.handler(args)
    except config_module.ConfigError as exc:
        print("Configuration error: {}".format(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csprpc",
        description="Show what you are drawing in CLIP STUDIO PAINT as Discord Rich Presence.",
    )
    parser.add_argument("--version", action="version", version="csprpc {}".format(__version__))
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug detail")
    # Not required: launching with no arguments, as double-clicking does,
    # opens the window.
    sub = parser.add_subparsers(dest="command")

    gui_cmd = sub.add_parser("gui", help="open the window (the default)")
    gui_cmd.add_argument(
        "--hidden", action="store_true", help="start minimised, for launching at login"
    )
    gui_cmd.set_defaults(handler=cmd_gui)

    run_cmd = sub.add_parser("run", help="watch CLIP STUDIO PAINT and update Discord")
    run_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="track time and print the presence without contacting Discord",
    )
    run_cmd.add_argument(
        "--once", action="store_true", help="do a single poll and exit (useful for testing)"
    )
    run_cmd.set_defaults(handler=cmd_run)

    watch_cmd = sub.add_parser(
        "watch", help="print what is detected each poll, without touching Discord"
    )
    watch_cmd.add_argument("--interval", type=float, default=None, help="override poll interval")
    watch_cmd.set_defaults(handler=cmd_watch)

    doctor_cmd = sub.add_parser("doctor", help="check permissions, Discord and detection")
    doctor_cmd.set_defaults(handler=cmd_doctor)

    stats_cmd = sub.add_parser("stats", help="show tracked time")
    stats_cmd.add_argument("--limit", type=int, default=15, help="rows per section")
    stats_cmd.add_argument("--json", action="store_true", help="print the raw stats as JSON")
    stats_cmd.set_defaults(handler=cmd_stats)

    config_cmd = sub.add_parser("config", help="inspect or change settings")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("path", help="print the config file path").set_defaults(
        handler=cmd_config_path
    )
    config_sub.add_parser("show", help="print the effective config").set_defaults(
        handler=cmd_config_show
    )
    config_sub.add_parser("edit", help="open the config in your editor").set_defaults(
        handler=cmd_config_edit
    )
    set_cmd = config_sub.add_parser("set", help="change one setting")
    set_cmd.add_argument("key", help="dotted setting name, e.g. presence.elapsed")
    set_cmd.add_argument("value")
    set_cmd.set_defaults(handler=cmd_config_set)

    service_cmd = sub.add_parser("service", help="run automatically at login")
    service_sub = service_cmd.add_subparsers(dest="service_command", required=True)
    service_sub.add_parser("install", help="install and start the background service").set_defaults(
        handler=cmd_service_install
    )
    service_sub.add_parser("uninstall", help="stop and remove the background service").set_defaults(
        handler=cmd_service_uninstall
    )
    service_sub.add_parser("status", help="show background service status").set_defaults(
        handler=cmd_service_status
    )

    return parser


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def _load_config_or_report(strict: bool = True) -> Dict[str, Any]:
    path, created = config_module.ensure_exists()
    if created:
        print("Created a default config at {}".format(path), file=sys.stderr)
    cfg = config_module.load(path)
    problems = config_module.validate(cfg)
    for problem in problems:
        print("{} {}".format(WARN, problem), file=sys.stderr)
    if strict and not str(cfg.get("client_id", "")).strip():
        raise config_module.ConfigError(
            "a Discord Application ID is required before the presence can be shown"
        )
    return cfg


def _make_tracker(cfg: Dict[str, Any]) -> Tracker:
    return Tracker(
        config_module.stats_path(),
        save_interval=float(cfg.get("stats", {}).get("save_interval_seconds", 60)),
    )


# -- window ---------------------------------------------------------------


def cmd_gui(args: argparse.Namespace) -> int:
    # Imported lazily so the rest of the CLI still works without Tk.
    from csprpc import gui

    return gui.launch(start_hidden=bool(getattr(args, "hidden", False)))


# -- run ------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_config_or_report(strict=not args.dry_run)
    tracker = _make_tracker(cfg)
    daemon = PresenceDaemon(cfg, tracker, dry_run=args.dry_run)

    if args.once:
        snapshot = daemon.step()
        _print_snapshot(snapshot, tracker)
        daemon.shutdown()
        return 0

    def handle_signal(signum: int, _frame: Any) -> None:
        log.info("received signal %s, shutting down", signum)
        daemon.stop()

    signal.signal(signal.SIGINT, handle_signal)
    # Windows has no SIGTERM delivery for console apps; SIGBREAK is the
    # equivalent, and Task Scheduler uses it to stop a task.
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handle_signal)
            except (ValueError, OSError):
                pass

    daemon.run_forever()
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = _load_config_or_report(strict=False)
    interval = args.interval or float(cfg.get("poll_interval_seconds", 5.0))
    tracker = _make_tracker(cfg)
    max_delta = interval * 3 + 5.0

    print("Watching every {}s. Press Ctrl-C to stop.\n".format(interval))
    seen_notes = set()
    try:
        while True:
            observation = system.observe(cfg)
            snapshot = evaluate(cfg, observation)
            tracker.tick(snapshot.active, snapshot.document_key, max_delta)
            _print_snapshot(snapshot, tracker)
            for note in observation.notes:
                if note not in seen_notes:
                    seen_notes.add(note)
                    print("    note: {}".format(note))
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
    finally:
        tracker.save(force=True)
    return 0


def _print_snapshot(snapshot: Snapshot, tracker: Tracker) -> None:
    observation = snapshot.observation
    if not observation.running:
        print(
            "{}  CLIP STUDIO PAINT is not running".format(time.strftime("%H:%M:%S")),
            flush=True,
        )
        return
    document = observation.document
    # Flushed so the output is still useful when redirected to a log file.
    print(
        "{}  {:<12} {:<32} front={:<5} idle={:>5.0f}s  file={:>8}  today={:>8}".format(
            time.strftime("%H:%M:%S"),
            snapshot.kind,
            (document.name if document else "-")[:32],
            str(observation.frontmost).lower(),
            observation.idle_seconds,
            humanize(tracker.session_file_seconds(snapshot.document_key)),
            humanize(tracker.today_seconds()),
        ),
        flush=True,
    )


# -- doctor ---------------------------------------------------------------


def _os_description() -> str:
    if IS_MACOS:
        return "macOS {}".format(platform.mac_ver()[0] or platform.platform())
    if IS_WINDOWS:
        return "Windows {}".format(platform.win32_ver()[0] or platform.platform())
    return platform.platform()


def _discord_install_hint() -> Tuple[Optional[bool], str]:
    """Whether the Discord desktop app looks installed, and how to say so."""
    if IS_MACOS:
        path = Path("/Applications/Discord.app")
        if path.exists():
            return True, "Discord.app installed"
        return None, "Discord.app not in /Applications (fine if installed elsewhere)"
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local and (Path(local) / "Discord").exists():
            return True, "Discord installed in %LOCALAPPDATA%"
        return None, "Discord not found in %LOCALAPPDATA% (fine if installed elsewhere)"
    return None, "unknown platform"


def cmd_doctor(_args: argparse.Namespace) -> int:
    failures = 0

    def report(ok: Optional[bool], message: str, hint: str = "") -> None:
        nonlocal failures
        print("{} {}".format(_status_mark(ok), message))
        if hint:
            print("    -> {}".format(hint))
        if ok is False:
            failures += 1

    print("System")
    report(system.IS_SUPPORTED, _os_description())
    report(sys.version_info >= (3, 8), "Python {}".format(platform.python_version()))

    print("\nConfiguration")
    path, created = config_module.ensure_exists()
    report(True, "config at {}{}".format(path, " (just created)" if created else ""))
    try:
        cfg = config_module.load(path)
        problems = config_module.validate(cfg)
    except config_module.ConfigError as exc:
        report(False, "config is unusable: {}".format(exc))
        return 1
    if problems:
        for problem in problems:
            report(None, problem)
    else:
        report(True, "settings look valid")

    print("\nCLIP STUDIO PAINT")
    installed = system.find_app_path()
    report(
        bool(installed) or None,
        "installed at {}".format(installed) if installed else "no installation found",
        "" if installed else "install it, or adjust process_match in the config",
    )
    process = system.find_process(cfg.get("process_match", {}))
    if process:
        detail = " bundle {}".format(process.bundle_id) if process.bundle_id else ""
        report(True, "running as pid {} ({}){}".format(process.pid, process.app_name, detail))
    else:
        report(None, "not running right now", "start it, then run `csprpc doctor` again")

    print("\nPermissions and detection")
    probe_pid = process.pid if process else _probe_pid()
    if probe_pid is None:
        report(None, "could not find a process to test window titles against")
    else:
        try:
            titles = system.window_titles(probe_pid)
            if system.NEEDS_WINDOW_PERMISSION:
                message = "{} access granted ({} window(s) visible)".format(
                    system.WINDOW_PERMISSION_NAME, len(titles)
                )
            else:
                message = "window titles readable ({} visible, no permission needed)".format(
                    len(titles)
                )
            report(True, message)
            if process and titles:
                for title in titles[:8]:
                    print("      window: {!r}".format(title))
        except system.PermissionDenied:
            report(
                False,
                "{} access denied, so window titles are unavailable".format(
                    system.WINDOW_PERMISSION_NAME or "window"
                ),
                system.WINDOW_PERMISSION_HINT,
            )
        except RuntimeError as exc:
            report(False, "window title probe failed: {}".format(exc))

    if IS_MACOS:
        report(
            shutil.which("lsof") is not None,
            "lsof available for the open_files fallback",
        )
        if process:
            paths = system.open_document_paths(process.pid, cfg["document"].get("extensions", []))
            report(
                bool(paths) or None,
                "open_files sees {} artwork file(s)".format(len(paths)) if paths else
                "open_files sees no artwork files held open",
            )
            for candidate in paths[:5]:
                print("      file: {}".format(candidate))

    if process:
        document = system.detect_document(process.pid, cfg.get("document", {}))
        report(
            bool(document) or None,
            "detected document: {}".format(document.name) if document else
            "no document detected (open a canvas and retry)",
        )

    print("\nDiscord")
    installed_ok, installed_message = _discord_install_hint()
    report(installed_ok, installed_message)

    endpoint = find_endpoint()
    report(
        endpoint is not None,
        "IPC endpoint at {}".format(endpoint) if endpoint else "no IPC endpoint found",
        "" if endpoint else "start the Discord desktop app (the browser version cannot work)",
    )

    client_id = str(cfg.get("client_id", "")).strip()
    if endpoint and client_id:
        ipc = DiscordIPC(client_id)
        try:
            ipc.connect()
            user = ipc.user or {}
            report(
                True,
                "handshake succeeded as {}".format(
                    user.get("username") or user.get("id") or "unknown user"
                ),
            )
            ipc.close()
        except DiscordNotRunning as exc:
            report(False, "could not connect: {}".format(exc))
        except DiscordClosed as exc:
            report(
                False,
                "Discord rejected the handshake: {}".format(exc),
                "check that client_id matches the Application ID of your Discord app",
            )
    elif endpoint:
        report(None, "skipped handshake test because client_id is not set")

    print(
        "\n{}".format(
            "All good." if failures == 0 else "{} problem(s) need attention.".format(failures)
        )
    )
    return 1 if failures else 0


def _probe_pid() -> Optional[int]:
    """A process to test window title reading against, preferring one with windows."""
    front = system.frontmost_pid()
    if front is not None:
        return front
    for pid, executable in system.list_processes():
        lowered = executable.lower().replace("\\", "/")
        if lowered.endswith("/finder.app/contents/macos/finder") or lowered.endswith(
            "/explorer.exe"
        ):
            return pid
    return None


# -- stats ----------------------------------------------------------------


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = _load_config_or_report(strict=False)
    tracker = _make_tracker(cfg)

    if args.json:
        print(json.dumps(tracker.data, indent=2, sort_keys=True))
        return 0

    if not tracker.data["days"] and not tracker.data["files"]:
        print("No time tracked yet. Run `csprpc run` while you draw.")
        return 0

    print("Today   {}".format(humanize_precise(tracker.today_seconds())))
    print("Total   {}".format(humanize_precise(tracker.total_seconds())))

    files = tracker.files_by_time(args.limit)
    if files:
        print("\nTime per file")
        width = min(48, max(len(name) for name, _ in files))
        for name, entry in files:
            print(
                "  {:<{width}}  {:>10}".format(
                    name[:width], humanize_precise(entry.get("total_seconds", 0.0)), width=width
                )
            )

    days = tracker.days_by_date(args.limit)
    if days:
        print("\nTime per day")
        for date, entry in days:
            top = sorted(
                entry.get("files", {}).items(), key=lambda kv: kv[1], reverse=True
            )
            suffix = "  ({})".format(top[0][0]) if top else ""
            print(
                "  {}  {:>10}{}".format(
                    date, humanize_precise(entry.get("total_seconds", 0.0)), suffix
                )
            )
    return 0


# -- config ---------------------------------------------------------------


def cmd_config_path(_args: argparse.Namespace) -> int:
    print(config_module.config_path())
    return 0


def cmd_config_show(_args: argparse.Namespace) -> int:
    config_module.ensure_exists()
    print(json.dumps(config_module.load(), indent=2))
    return 0


def cmd_config_edit(_args: argparse.Namespace) -> int:
    path, _ = config_module.ensure_exists()
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if editor:
        return subprocess.call([editor, str(path)])
    if IS_WINDOWS:
        return subprocess.call(["notepad.exe", str(path)])
    return subprocess.call(["open", "-t", str(path)])


def cmd_config_set(args: argparse.Namespace) -> int:
    path, _ = config_module.ensure_exists()
    cfg = config_module.load(path)
    value = config_module.set_dotted(cfg, args.key, args.value)
    config_module.save(cfg, path)
    print("{} = {}".format(args.key, json.dumps(value)))
    return 0


# -- background service ---------------------------------------------------


def _is_frozen() -> bool:
    """True when running from a PyInstaller bundle rather than source."""
    return bool(getattr(sys, "frozen", False))


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _app_location() -> Path:
    """The directory the user keeps this in.

    A bundle unpacks its source to a temporary directory, so there the
    executable's own folder is the meaningful location.
    """
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return _project_root()


def _background_launcher() -> str:
    """Executable to start at login.

    A frozen build is already windowed, so it needs no console-less sibling.
    From source on Windows, pythonw.exe keeps a console from flashing up.
    """
    executable = Path(sys.executable)
    if IS_WINDOWS and not _is_frozen():
        candidate = executable.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(executable)


def _service_argv() -> List[str]:
    """Command line that starts the app at login, minimised.

    A frozen build is its own entry point; `-m csprpc` only works from source.
    """
    if _is_frozen():
        return [_background_launcher(), "gui", "--hidden"]
    return [_background_launcher(), "-m", "csprpc", "gui", "--hidden"]


def service_installed() -> bool:
    """Whether a login item exists. The GUI checkbox reads this."""
    if IS_WINDOWS:
        code, _, _ = _schtasks(["/Query", "/TN", SCHEDULED_TASK_NAME])
        return code == 0
    return _agent_path().exists()


# A launch agent runs without the privileges your terminal has been granted,
# so it cannot read files inside these folders unless it is given Full Disk
# Access. A project living in one of them fails to even import.
_PROTECTED_HOME_DIRS = ("Desktop", "Documents", "Downloads")


def _protected_location(path: Path) -> Optional[str]:
    """The protected home folder containing path, if any. macOS only."""
    if not IS_MACOS:
        return None
    try:
        relative = path.resolve().relative_to(Path.home())
    except ValueError:
        return None
    first = relative.parts[0] if relative.parts else ""
    return first if first in _PROTECTED_HOME_DIRS else None


def _agent_startup_error(log_file: Path, timeout: float = 6.0) -> Optional[str]:
    """Watch the fresh log for a startup failure. None means it came up fine."""
    deadline = time.monotonic() + timeout
    fatal = ("No module named", "Traceback (most recent call last)", "Permission denied")
    while time.monotonic() < deadline:
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "watching for CLIP STUDIO PAINT" in text:
            return None
        for marker in fatal:
            if marker in text:
                return text.strip()
        time.sleep(0.25)
    return None


def _reset_log(log_file: Path) -> None:
    """Start from an empty log so the health check reads this run only."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_file.write_text("", encoding="utf-8")
    except OSError:
        pass


def cmd_service_install(args: argparse.Namespace) -> int:
    if IS_WINDOWS:
        return _windows_service_install(args)
    return _launchd_service_install(args)


def cmd_service_uninstall(args: argparse.Namespace) -> int:
    if IS_WINDOWS:
        return _windows_service_uninstall(args)
    return _launchd_service_uninstall(args)


def cmd_service_status(args: argparse.Namespace) -> int:
    if IS_WINDOWS:
        return _windows_service_status(args)
    return _launchd_service_status(args)


# -- background service: macOS launchd ------------------------------------


def _agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "{}.plist".format(LAUNCH_AGENT_LABEL)


def _domain() -> str:
    return "gui/{}".format(os.getuid())


def _launchctl(argv: List[str], allow_failure: bool = False) -> Any:
    code, out, err = system.run_command(["launchctl"] + argv, timeout=10.0)
    if code != 0 and not allow_failure:
        raise RuntimeError("launchctl {} failed: {}".format(" ".join(argv), err.strip()))
    return code, out, err


def _launchd_service_install(_args: argparse.Namespace) -> int:
    config_module.ensure_exists()
    plist_path = _agent_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = config_module.log_path()
    _reset_log(log_file)

    plist: Dict[str, Any] = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": _service_argv(),
        "RunAtLoad": True,
        # Deliberately not KeepAlive: closing the window is how you stop the
        # presence, and launchd would otherwise reopen it immediately.
        "KeepAlive": False,
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
    }
    environment: Dict[str, str] = {}
    if not _is_frozen():
        # A bundle carries its own code; only a source checkout needs these.
        environment["PYTHONPATH"] = str(_project_root())
        plist["WorkingDirectory"] = str(_project_root())
    if os.environ.get("CSPRPC_HOME"):
        environment["CSPRPC_HOME"] = os.environ["CSPRPC_HOME"]
    if environment:
        plist["EnvironmentVariables"] = environment

    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle)

    _launchctl(["bootout", _domain(), str(plist_path)], allow_failure=True)
    code, _, err = _launchctl(["bootstrap", _domain(), str(plist_path)], allow_failure=True)
    if code != 0:
        code, _, err = _launchctl(["load", "-w", str(plist_path)], allow_failure=True)
    if code != 0:
        print("Installed {} but launchctl failed: {}".format(plist_path, err.strip()))
        return 1

    startup_error = _agent_startup_error(log_file)
    if startup_error:
        protected = _protected_location(_app_location())
        print("Installed {}, but the agent failed to start:\n".format(plist_path))
        print("  {}\n".format(startup_error.splitlines()[-1]))
        if protected:
            print(
                "This lives in ~/{}, which macOS protects. A launch agent does not\n"
                "inherit your terminal's access to that folder, so it cannot read the\n"
                "code. Fix it either way:\n".format(protected)
            )
            print(
                "  - Move it somewhere unprotected, then reinstall:\n"
                "      mv {} ~/csprpc && cd ~/csprpc && ./csprpc.sh service install\n".format(
                    _app_location()
                )
            )
            print(
                "  - Or grant Full Disk Access to {}\n"
                "    in System Settings > Privacy & Security > Full Disk Access.".format(
                    _service_argv()[0]
                )
            )
        else:
            print("Full log: {}".format(log_file))
        print("\nRunning `csprpc run` in a terminal works regardless.")
        return 1

    print("Installed and started {}".format(plist_path))
    print("Logs: {}".format(log_file))
    print(
        "\nNote: the launch agent runs outside your terminal, so grant Accessibility\n"
        "access to {}\n"
        "in System Settings > Privacy & Security > Accessibility.".format(_service_argv()[0])
    )
    return 0


def _launchd_service_uninstall(_args: argparse.Namespace) -> int:
    plist_path = _agent_path()
    _launchctl(["bootout", _domain(), str(plist_path)], allow_failure=True)
    _launchctl(["unload", str(plist_path)], allow_failure=True)
    if plist_path.exists():
        plist_path.unlink()
        print("Removed {}".format(plist_path))
    else:
        print("No launch agent installed.")
    return 0


def _launchd_service_status(_args: argparse.Namespace) -> int:
    plist_path = _agent_path()
    if not plist_path.exists():
        print("Not installed. Run `csprpc service install`.")
        return 1
    print("Plist: {}".format(plist_path))
    code, out, err = _launchctl(
        ["print", "{}/{}".format(_domain(), LAUNCH_AGENT_LABEL)], allow_failure=True
    )
    if code != 0:
        print("Installed but not loaded ({})".format(err.strip() or code))
        return 1
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith(("state =", "pid =", "last exit code =")):
            print("  {}".format(stripped))
    print("Logs: {}".format(config_module.log_path()))
    return 0


# -- background service: Windows Task Scheduler ---------------------------


def _task_script_path() -> Path:
    return config_module.data_dir() / "run-agent.cmd"


def _write_task_script(log_file: Path) -> Path:
    """A wrapper batch file: schtasks cannot set a working directory itself."""
    script = _task_script_path()
    script.parent.mkdir(parents=True, exist_ok=True)
    lines = ["@echo off"]
    if not _is_frozen():
        # A bundle carries its own code; only a source checkout needs these.
        root = _project_root()
        lines.append('cd /d "{}"'.format(root))
        lines.append('set "PYTHONPATH={}"'.format(root))
    if os.environ.get("CSPRPC_HOME"):
        lines.append('set "CSPRPC_HOME={}"'.format(os.environ["CSPRPC_HOME"]))
    argv = _service_argv()
    command = " ".join(['"{}"'.format(argv[0])] + argv[1:])
    lines.append('{} >> "{}" 2>&1'.format(command, log_file))
    script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return script


def _schtasks(argv: List[str]) -> Tuple[int, str, str]:
    return system.run_command(["schtasks"] + argv, timeout=20.0)


def _windows_service_install(_args: argparse.Namespace) -> int:
    config_module.ensure_exists()
    log_file = config_module.log_path()
    _reset_log(log_file)
    script = _write_task_script(log_file)

    code, _, err = _schtasks(
        [
            "/Create",
            "/TN", SCHEDULED_TASK_NAME,
            "/SC", "ONLOGON",
            "/TR", '"{}"'.format(script),
            "/RL", "LIMITED",
            "/F",
        ]
    )
    if code != 0:
        print("Could not create the scheduled task: {}".format(err.strip() or code))
        return 1

    # ONLOGON does not fire now, so start it explicitly to verify it works.
    run_code, _, run_err = _schtasks(["/Run", "/TN", SCHEDULED_TASK_NAME])
    if run_code != 0:
        print("Created the task but could not start it: {}".format(run_err.strip() or run_code))
        return 1

    startup_error = _agent_startup_error(log_file)
    if startup_error:
        print('Created the task "{}", but it failed to start:\n'.format(SCHEDULED_TASK_NAME))
        print("  {}\n".format(startup_error.splitlines()[-1]))
        print("Full log: {}".format(log_file))
        print("\nRunning `csprpc.cmd run` in a terminal works regardless.")
        return 1

    print('Installed and started the scheduled task "{}"'.format(SCHEDULED_TASK_NAME))
    print("Runs at every logon via {}".format(script))
    print("Logs: {}".format(log_file))
    return 0


def _windows_service_uninstall(_args: argparse.Namespace) -> int:
    code, _, err = _schtasks(["/Delete", "/TN", SCHEDULED_TASK_NAME, "/F"])
    script = _task_script_path()
    if script.exists():
        try:
            script.unlink()
        except OSError:
            pass
    if code != 0:
        print("No scheduled task to remove ({})".format(err.strip() or code))
        return 0
    print('Removed the scheduled task "{}"'.format(SCHEDULED_TASK_NAME))
    return 0


def _windows_service_status(_args: argparse.Namespace) -> int:
    code, out, err = _schtasks(["/Query", "/TN", SCHEDULED_TASK_NAME, "/FO", "LIST"])
    if code != 0:
        print("Not installed. Run `csprpc service install`. ({})".format(err.strip() or code))
        return 1
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith(("TaskName:", "Status:", "Last Run Time:", "Last Result:")):
            print("  {}".format(stripped))
    print("Logs: {}".format(config_module.log_path()))
    return 0
