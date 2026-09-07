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
from csprpc.presence import PresenceDaemon, session_vibe
from csprpc.tracker import Tracker, humanize

WINDOW_TITLE = "CLIP STUDIO PAINT Rich Presence"

# token, what it means, an example of the filled-in text.
PLACEHOLDER_HELP = (
    ("{doc}", "The canvas file name", "Summer.clip"),
    ("{stem}", "The file name without an extension", "Summer"),
    ("{ext}", "The extension only, without the dot", "clip"),
    ("{modified}", "A mark when the canvas has unsaved changes", " *"),
    ("{app}", "Always CLIP STUDIO PAINT", "CLIP STUDIO PAINT"),
    ("{file_time}", "Time on this file since you launched the app", "47m"),
    ("{file_time_total}", "Time on this file across every session", "12h 31m"),
    ("{session_time}", "Time drawing since you launched the app", "1h 4m"),
    ("{today_time}", "Time drawing today", "2h 14m"),
    ("{total_time}", "Time drawing across every day", "61h 48m"),
    ("{vibe}", "A flavour line from how long this session has run", "in the zone"),
    ("{streak}", "Consecutive days with drawing time", "3"),
    ("{files_today}", "How many files you have drawn on today", "2"),
    ("{top_today}", "The file with the most time today", "Summer.clip"),
    ("{weekday}", "Today's weekday", "Sunday"),
    ("{idle}", "Time since the last keyboard or mouse input", "12s"),
    ("{focus}", "Whether CLIP STUDIO PAINT is in front", "in front"),
    ("{strokes}", "Pen or mouse presses on this canvas this session", "142"),
    ("{session_strokes}", "Pen or mouse presses since you launched the app", "210"),
)

# What the wording boxes accept, shown to the user verbatim.
PLACEHOLDERS = tuple(token for token, _meaning, _example in PLACEHOLDER_HELP)

THEMES = {
    "light": {
        "bg": "#f3f3f3",
        "fg": "#1a1a1a",
        "muted": "#666666",
        "input_bg": "#ffffff",
        "input_fg": "#1a1a1a",
        "select_bg": "#0078d4",
        "select_fg": "#ffffff",
        "border": "#d0d0d0",
        "output_bg": "#ffffff",
        "output_fg": "#1a1a1a",
    },
    "dark": {
        "bg": "#1e1e1e",
        "fg": "#e8e8e8",
        "muted": "#9a9a9a",
        "input_bg": "#2b2b2b",
        "input_fg": "#e8e8e8",
        "select_bg": "#3d6a9a",
        "select_fg": "#ffffff",
        "border": "#3a3a3a",
        "output_bg": "#141414",
        "output_fg": "#e8e8e8",
    },
}


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
        Field(
            "stats.track_strokes",
            "Count strokes this session",
            "bool",
            hint="Each pen or mouse press while CLIP STUDIO PAINT is in front. Palette clicks count too.",
        ),
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
        self.dark_mode = bool((config.get("ui") or {}).get("dark_mode", False))
        self._text_widgets: List[Any] = []

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.minsize(640, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.style = ttk.Style(self.root)
        self._default_theme = self.style.theme_use()

        self._build()
        self._apply_theme()
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

        extras = ttk.LabelFrame(frame, text="Also", padding=12)
        extras.pack(fill="x", pady=(12, 0))
        for row, (key, label) in enumerate([
            ("vibe", "Vibe"),
            ("streak", "Streak"),
            ("files_today", "Files today"),
            ("top_today", "Today's favourite"),
            ("focus", "Focus"),
            ("idle", "Idle"),
            ("strokes", "Strokes"),
        ]):
            ttk.Label(extras, text=label + ":").grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value="-")
            self.info[key] = var
            ttk.Label(extras, textvariable=var).grid(row=row, column=1, sticky="w",
                                                     padx=(12, 0), pady=2)
        return frame

    def _fields_tab(self, parent: Any, groups: List[Tuple[str, Tuple[Field, ...]]],
                    wording: bool = False) -> Any:
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(parent, padding=12)

        buttons = ttk.Frame(frame)
        if wording:
            buttons.pack(side="bottom", fill="x")

        for title, fields in groups:
            box = ttk.LabelFrame(frame, text=title, padding=10)
            box.pack(fill="x", pady=(0, 8))
            box.columnconfigure(1, weight=1)
            for row, field in enumerate(fields):
                self._add_field(box, field, row)

        if wording:
            self._placeholder_legend(frame)
        else:
            buttons.pack(fill="x")
        ttk.Button(buttons, text="Save", command=self.on_save).pack(side="left")
        ttk.Button(buttons, text="Revert", command=self.on_revert).pack(side="left", padx=6)
        return frame

    def _placeholder_legend(self, parent: Any) -> None:
        ttk = self.ttk
        from tkinter import scrolledtext

        box = ttk.LabelFrame(parent, text="What each placeholder means", padding=8)
        box.pack(fill="both", expand=True, pady=(0, 8))
        ttk.Label(
            box,
            text="Type these into the wording boxes above. They only change "
                 "what Discord shows.",
            style="Muted.TLabel",
            wraplength=600,
        ).pack(anchor="w", pady=(0, 6))

        lines = []
        width = max(len(token) for token, _, _ in PLACEHOLDER_HELP)
        for token, meaning, example in PLACEHOLDER_HELP:
            lines.append("{}  {}  e.g. {}".format(token.ljust(width), meaning, example))
        text = scrolledtext.ScrolledText(box, height=8, wrap="word", font=("TkFixedFont", 9))
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        self._text_widgets.append(text)

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
            ttk.Label(box, text=field.hint, style="Muted.TLabel").grid(
                row=row, column=2, sticky="w", padx=(10, 0))

    def _diagnostics_tab(self, parent: Any) -> Any:
        ttk = self.ttk
        from tkinter import scrolledtext

        frame = ttk.Frame(parent, padding=12)
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Run checks", command=self.on_doctor).pack(side="left")
        ttk.Button(bar, text="Show tracked time", command=self.on_stats).pack(side="left", padx=6)
        ttk.Label(bar, text=str(self.path), style="Muted.TLabel").pack(side="right")

        self.output = scrolledtext.ScrolledText(frame, height=14, wrap="word")
        self.output.pack(fill="both", expand=True)
        self.output.insert("1.0", "Run checks to test permissions, detection and Discord.\n")
        self._text_widgets.append(self.output)
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
        self.theme_button = ttk.Button(bar, text="Dark mode", command=self.on_toggle_theme)
        self.theme_button.pack(side="right", padx=6)

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

        streak = self.tracker.drawing_streak()
        files_today = self.tracker.today_file_count()
        self.info["vibe"].set(session_vibe(self.tracker.session_seconds))
        self.info["streak"].set(
            "{} day{}".format(streak, "" if streak == 1 else "s") if streak else "none yet"
        )
        self.info["files_today"].set(str(files_today) if files_today else "none yet")
        self.info["top_today"].set(self.tracker.top_file_today() or "-")
        self.info["focus"].set(
            "in front" if snapshot.observation.frontmost else "in the background"
        )
        self.info["idle"].set(humanize(snapshot.observation.idle_seconds))
        tracking = bool((self.config.get("stats") or {}).get("track_strokes"))
        if not tracking:
            self.info["strokes"].set("off")
        elif self.daemon is not None:
            file_count, session_count = self.daemon.strokes.totals()
            self.info["strokes"].set("{} this canvas · {} this session".format(
                file_count, session_count
            ))
        else:
            self.info["strokes"].set("0")

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

    def on_toggle_theme(self) -> None:
        """Flip the window colours. Discord is not touched."""
        self.dark_mode = not self.dark_mode
        self.config.setdefault("ui", {})["dark_mode"] = self.dark_mode
        try:
            config_module.save(self.config, self.path)
        except OSError:
            pass
        self._apply_theme()

    def _apply_theme(self) -> None:
        """Paint ttk widgets and the diagnostics box for the current mode."""
        palette = THEMES["dark" if self.dark_mode else "light"]
        style = self.style
        if self.dark_mode:
            try:
                style.theme_use("clam")
            except self.tk.TclError:
                pass
        else:
            try:
                style.theme_use(self._default_theme)
            except self.tk.TclError:
                pass

        style.configure(".", background=palette["bg"], foreground=palette["fg"])
        style.configure("TFrame", background=palette["bg"])
        style.configure("TLabel", background=palette["bg"], foreground=palette["fg"])
        style.configure("Muted.TLabel", background=palette["bg"], foreground=palette["muted"])
        style.configure("TButton", background=palette["input_bg"], foreground=palette["fg"])
        style.configure("TCheckbutton", background=palette["bg"], foreground=palette["fg"])
        style.configure("TNotebook", background=palette["bg"], bordercolor=palette["border"])
        style.configure("TNotebook.Tab", background=palette["input_bg"], foreground=palette["fg"])
        style.map("TNotebook.Tab",
                  background=[("selected", palette["bg"])],
                  foreground=[("selected", palette["fg"])])
        style.configure("TLabelframe", background=palette["bg"], foreground=palette["fg"],
                        bordercolor=palette["border"])
        style.configure("TLabelframe.Label", background=palette["bg"], foreground=palette["fg"])
        style.configure("TEntry", fieldbackground=palette["input_bg"],
                        foreground=palette["input_fg"], background=palette["input_bg"])
        style.configure("TCombobox", fieldbackground=palette["input_bg"],
                        foreground=palette["input_fg"], background=palette["input_bg"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", palette["input_bg"])],
                  foreground=[("readonly", palette["input_fg"])])

        self.root.configure(bg=palette["bg"])
        for widget in self._text_widgets:
            was_disabled = str(widget.cget("state")) == "disabled"
            if was_disabled:
                widget.configure(state="normal")
            widget.configure(
                background=palette["output_bg"],
                foreground=palette["output_fg"],
                insertbackground=palette["output_fg"],
                selectbackground=palette["select_bg"],
                selectforeground=palette["select_fg"],
            )
            if was_disabled:
                widget.configure(state="disabled")
        if hasattr(self, "theme_button"):
            self.theme_button.configure(text="Light mode" if self.dark_mode else "Dark mode")

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
