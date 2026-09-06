"""The window you get when you launch the app.

tkinter ships with Python, so this adds nothing to install. The daemon runs on
a worker thread and hands its snapshots back through a queue; Tk widgets are
only ever touched from the main thread, which is the one rule tkinter enforces.

Closing the window stops the presence: the daemon's own shutdown clears the
activity in Discord. Hiding it just minimises, leaving everything running.
"""

from __future__ import annotations

import copy
import io
import queue
import threading
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from csprpc import config as config_module
from csprpc.presence import PresenceDaemon
from csprpc.tracker import Tracker, humanize

WINDOW_TITLE = "CLIP STUDIO PAINT Rich Presence"

# What the wording boxes accept, shown to the user verbatim.
PLACEHOLDERS = (
    "{doc}", "{stem}", "{ext}", "{modified}", "{app}",
    "{file_time}", "{file_time_total}", "{session_time}",
    "{today_time}", "{total_time}",
)


class Field:
    """One editable setting, identified by its dotted path in the config."""

    def __init__(self, key: str, label: str, kind: str = "text",
                 choices: Tuple[str, ...] = (), hint: str = ""):
        self.key = key
        self.label = label
        self.kind = kind
        self.choices = choices
        self.hint = hint


PRESENCE_FIELDS: List[Tuple[str, Tuple[Field, ...]]] = [
    ("While drawing", (
        Field("presence.templates.working.details", "Top line"),
        Field("presence.templates.working.state", "Second line"),
    )),
    ("While away", (
        Field("presence.templates.idle.details", "Top line"),
        Field("presence.templates.idle.state", "Second line"),
    )),
    ("With no canvas open", (
        Field("presence.templates.no_document.details", "Top line"),
        Field("presence.templates.no_document.state", "Second line"),
    )),
    ("Labels and clock", (
        Field("presence.large_text", "Tooltip on the big icon"),
        Field("presence.small_text_active", "Badge while drawing"),
        Field("presence.small_text_idle", "Badge while away"),
        Field("presence.elapsed", "Count time up from", "choice",
              config_module.VALID_ELAPSED,
              hint="file: this drawing  session: since launch  today: since midnight"),
    )),
]

BEHAVIOUR_FIELDS: List[Tuple[str, Tuple[Field, ...]]] = [
    ("Privacy", (
        Field("privacy.show_file_name", "Send the file name to Discord", "bool"),
        Field("privacy.hide_extension", "Drop the .clip extension", "bool"),
        Field("privacy.redacted_name", "Shown instead of the name"),
    )),
    ("Timing", (
        Field("poll_interval_seconds", "Check every (seconds)", "number"),
        Field("idle_timeout_seconds", "Count as away after (seconds)", "number"),
        Field("linger_seconds", "Keep the presence after quitting (seconds)", "number"),
    )),
    ("Counting", (
        Field("require_frontmost", "Only count time while CSP is in front", "bool"),
        Field("clear_presence_when_idle", "Hide the presence entirely while away", "bool"),
    )),
]


STATUS_TEXT = {
    "working": "Drawing",
    "idle": "Away",
    "no_document": "No canvas open",
    "not_running": "CLIP STUDIO PAINT is not running",
}


def get_dotted(config: Dict[str, Any], key: str) -> Any:
    node: Any = config
    for part in key.split("."):
        if not isinstance(node, dict):
            return ""
        node = node.get(part)
    return "" if node is None else node


def all_fields() -> Tuple[Field, ...]:
    """Every editable field across both settings tabs."""
    groups = PRESENCE_FIELDS + BEHAVIOUR_FIELDS
    return tuple(field for _, fields in groups for field in fields)


def apply_values(config: Dict[str, Any],
                 values: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Fold form values into a copy of the config and validate the result.

    Kept apart from the widgets so it can be tested without a display.
    Raises ConfigError for a value that cannot be coerced at all.
    """
    pending = copy.deepcopy(config)
    for key, value in values.items():
        config_module.set_dotted(pending, key, value)
    return pending, config_module.validate(pending)


class PresenceWindow:
    """The application window, owning the daemon thread for its lifetime."""

    def __init__(self, config: Dict[str, Any], path: Path, start_hidden: bool = False):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.config = config
        self.path = path

        stats = config.get("stats", {})
        self.tracker = Tracker(
            config_module.stats_path(),
            float(stats.get("save_interval_seconds", 60.0)),
        )
        self.queue: "queue.Queue" = queue.Queue()
        self.daemon: Optional[PresenceDaemon] = None
        self.thread: Optional[threading.Thread] = None
        self.vars: Dict[str, Any] = {}
        self._closing = False

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.minsize(560, 460)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build()
        self._start_daemon()
        self.root.after(250, self._drain)

        if start_hidden:
            self.root.iconify()

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk

        self.status_line = tk.StringVar(value="starting...")
        header = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        header.pack(fill="x")
        ttk.Label(header, textvariable=self.status_line,
                  font=("TkDefaultFont", 13, "bold")).pack(anchor="w")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10)
        notebook.add(self._status_tab(notebook), text="Status")
        notebook.add(self._fields_tab(notebook, PRESENCE_FIELDS, wording=True),
                     text="Presence")
        notebook.add(self._fields_tab(notebook, BEHAVIOUR_FIELDS), text="Behaviour")
        notebook.add(self._diagnostics_tab(notebook), text="Diagnostics")

        self._build_bottom_bar()

    def _status_tab(self, parent: Any) -> Any:
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(parent, padding=12)

        preview = ttk.LabelFrame(frame, text="What Discord shows", padding=12)
        preview.pack(fill="x")
        self.preview_app = tk.StringVar(value="CLIP STUDIO PAINT")
        self.preview_details = tk.StringVar(value="-")
        self.preview_state = tk.StringVar(value="-")
        ttk.Label(preview, textvariable=self.preview_app,
                  font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        ttk.Label(preview, textvariable=self.preview_details).pack(anchor="w")
        ttk.Label(preview, textvariable=self.preview_state).pack(anchor="w")

        detail = ttk.LabelFrame(frame, text="Detail", padding=12)
        detail.pack(fill="x", pady=(12, 0))
        self.info = {}
        for row, (key, label) in enumerate([
            ("discord", "Discord"),
            ("document", "Canvas"),
            ("today", "Today"),
            ("session", "This session"),
            ("total", "All time"),
        ]):
            ttk.Label(detail, text=label + ":").grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value="-")
            self.info[key] = var
            ttk.Label(detail, textvariable=var).grid(row=row, column=1, sticky="w",
                                                     padx=(12, 0), pady=2)
        return frame

    def _fields_tab(self, parent: Any, groups: List[Tuple[str, Tuple[Field, ...]]],
                    wording: bool = False) -> Any:
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(parent, padding=12)

        for title, fields in groups:
            box = ttk.LabelFrame(frame, text=title, padding=10)
            box.pack(fill="x", pady=(0, 8))
            box.columnconfigure(1, weight=1)
            for row, field in enumerate(fields):
                self._add_field(box, field, row)

        if wording:
            ttk.Label(frame, text="Available: " + "  ".join(PLACEHOLDERS),
                      foreground="#666", wraplength=520).pack(anchor="w", pady=(0, 6))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Save", command=self.on_save).pack(side="left")
        ttk.Button(buttons, text="Revert", command=self.on_revert).pack(side="left", padx=6)
        return frame

    def _add_field(self, box: Any, field: Field, row: int) -> None:
        tk, ttk = self.tk, self.ttk
        value = get_dotted(self.config, field.key)

        if field.kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(box, text=field.label, variable=var).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=2)
        else:
            ttk.Label(box, text=field.label + ":").grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=str(value))
            if field.kind == "choice":
                widget = ttk.Combobox(box, textvariable=var, values=list(field.choices),
                                      state="readonly", width=12)
            else:
                widget = ttk.Entry(box, textvariable=var)
            widget.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=2)

        self.vars[field.key] = var
        if field.hint:
            ttk.Label(box, text=field.hint, foreground="#666").grid(
                row=row, column=2, sticky="w", padx=(10, 0))

    def _diagnostics_tab(self, parent: Any) -> Any:
        ttk = self.ttk
        from tkinter import scrolledtext

        frame = ttk.Frame(parent, padding=12)
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Run checks", command=self.on_doctor).pack(side="left")
        ttk.Button(bar, text="Show tracked time", command=self.on_stats).pack(side="left", padx=6)
        ttk.Label(bar, text=str(self.path), foreground="#666").pack(side="right")

        self.output = scrolledtext.ScrolledText(frame, height=14, wrap="word")
        self.output.pack(fill="both", expand=True)
        self.output.insert("1.0", "Run checks to test permissions, detection and Discord.\n")
        return frame

    def _build_bottom_bar(self) -> None:
        tk, ttk = self.tk, self.ttk
        bar = ttk.Frame(self.root, padding=(12, 8))
        bar.pack(fill="x")

        from csprpc import cli

        self.at_login = tk.BooleanVar(value=cli.service_installed())
        ttk.Checkbutton(bar, text="Start at login", variable=self.at_login,
                        command=self.on_at_login).pack(side="left")
        ttk.Button(bar, text="Quit", command=self.on_close).pack(side="right")
        ttk.Button(bar, text="Hide", command=self.on_hide).pack(side="right", padx=6)

    # -- the daemon --------------------------------------------------------

    def _start_daemon(self) -> None:
        self.daemon = PresenceDaemon(self.config, self.tracker, on_update=self._on_update)
        self.thread = threading.Thread(target=self._run_daemon, daemon=True)
        self.thread.start()

    def _run_daemon(self) -> None:
        try:
            self.daemon.run_forever()
        except Exception:  # noqa: BLE001 - surface it instead of dying silently
            self.queue.put(("error", traceback.format_exc()))

    def _on_update(self, snapshot: Any, activity: Optional[Dict[str, Any]]) -> None:
        """Called on the worker thread; hand everything to the UI thread."""
        self.queue.put(("update", snapshot, activity))

    def _drain(self) -> None:
        """Apply whatever the worker thread reported, on the UI thread."""
        try:
            while True:
                message = self.queue.get_nowait()
                if message[0] == "update":
                    self._apply(message[1], message[2])
                elif message[0] == "error":
                    self._write_output(message[1])
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(250, self._drain)

    def _apply(self, snapshot: Any, activity: Optional[Dict[str, Any]]) -> None:
        self.status_line.set(STATUS_TEXT.get(snapshot.kind, snapshot.kind))

        if activity:
            self.preview_app.set(get_dotted(self.config, "presence.large_text"))
            self.preview_details.set(activity.get("details", "-"))
            self.preview_state.set(activity.get("state", "-"))
        else:
            self.preview_details.set("(no presence)")
            self.preview_state.set("")

        daemon = self.daemon
        if daemon is not None and daemon.connected:
            user = daemon.discord_user
            self.info["discord"].set("connected as {}".format(user) if user else "connected")
        else:
            self.info["discord"].set("not connected")

        self.info["document"].set(snapshot.document_key or "-")
        self.info["today"].set(humanize(self.tracker.today_seconds()))
        self.info["session"].set(humanize(self.tracker.session_seconds))
        self.info["total"].set(humanize(self.tracker.total_seconds()))

    # -- actions -----------------------------------------------------------

    def on_save(self) -> None:
        from tkinter import messagebox

        values = {key: var.get() for key, var in self.vars.items()}
        try:
            pending, problems = apply_values(self.config, values)
        except config_module.ConfigError as exc:
            messagebox.showerror(WINDOW_TITLE, str(exc))
            return

        if problems:
            messagebox.showerror(WINDOW_TITLE, "\n".join(problems))
            return

        config_module.save(pending, self.path)
        # The daemon reads self.config every poll, so updating in place applies
        # the change without dropping the Discord connection.
        self.config.clear()
        self.config.update(pending)
        if self.daemon is not None:
            self.daemon.poll_interval = float(pending["poll_interval_seconds"])
            self.daemon.max_delta = self.daemon.poll_interval * 3 + 5.0
        self.status_line.set("Saved")

    def on_revert(self) -> None:
        for key, var in self.vars.items():
            value = get_dotted(self.config, key)
            var.set(bool(value) if isinstance(var, self.tk.BooleanVar) else str(value))

    def on_doctor(self) -> None:
        from csprpc import cli

        self._run_capturing(cli.cmd_doctor)

    def on_stats(self) -> None:
        from csprpc import cli

        self._run_capturing(cli.cmd_stats, json=False)

    def _run_capturing(self, command: Any, **namespace: Any) -> None:
        """Run a CLI command and show whatever it printed."""
        import argparse

        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                command(argparse.Namespace(verbose=False, **namespace))
        except Exception:  # noqa: BLE001
            buffer.write("\n" + traceback.format_exc())
        self._write_output(buffer.getvalue())

    def _write_output(self, text: str) -> None:
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)

    def on_at_login(self) -> None:
        from csprpc import cli
        from tkinter import messagebox

        wanted = bool(self.at_login.get())
        command = cli.cmd_service_install if wanted else cli.cmd_service_uninstall
        self._run_capturing(command)
        actual = cli.service_installed()
        self.at_login.set(actual)
        if actual != wanted:
            messagebox.showwarning(
                WINDOW_TITLE,
                "Could not {} the login item. See the Diagnostics tab.".format(
                    "add" if wanted else "remove"),
            )

    def on_hide(self) -> None:
        """Minimise, leaving the presence running."""
        self.root.iconify()

    def on_close(self) -> None:
        """Quit for real, which clears the Discord presence."""
        self._closing = True
        self.status_line.set("stopping...")
        self.root.update_idletasks()
        if self.daemon is not None:
            self.daemon.stop()
        if self.thread is not None:
            self.thread.join(timeout=6.0)  # run_forever clears the presence on exit
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def launch(start_hidden: bool = False) -> int:
    """Open the window. Returns a process exit code."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print(
            "The graphical interface needs tkinter, which is missing from this\n"
            "Python build. Use `csprpc run` instead, or install a Python that\n"
            "includes Tk."
        )
        return 2

    path, _ = config_module.ensure_exists()
    config = config_module.load(path)
    PresenceWindow(config, path, start_hidden=start_hidden).run()
    return 0
