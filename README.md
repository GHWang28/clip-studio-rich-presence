# CLIP STUDIO PAINT → Discord Rich Presence

Shows what you are drawing on your Discord profile, and keeps a private log of
how long you spent in each file.

```
CLIP STUDIO PAINT
Portrait_final.clip *
2h 14m today
⏱ 47:12 elapsed
```

Runs on **macOS and Windows**, on the Python each already has, with **no
dependencies** — no `pip install`, no Node, no Homebrew.

## Requirements

- macOS, or Windows 10/11
- The **Discord desktop app**, running. The browser version has no local IPC
  endpoint, so Rich Presence cannot work there.
- CLIP STUDIO PAINT

## Install

### Option A — download a ready-made build

Grab the file for your machine from the
[latest release](https://github.com/GHWang28/clip-studio-rich-presence/releases/latest).
Nothing else to install; Python is bundled inside.

| File | For |
| --- | --- |
| `csprpc.exe` | Windows — the one you run |
| `csprpcw.exe` | Windows — same thing without a console window, used by `service install` |
| `csprpc-macos-arm64` | Apple Silicon Macs (M1 and later) |
| `csprpc-macos-x86_64` | Intel Macs |

Downloaded binaries are unsigned, so each OS will object once:

```sh
# macOS: make it runnable and clear the download quarantine
chmod +x csprpc-macos-arm64
xattr -d com.apple.quarantine csprpc-macos-arm64
```

On Windows, SmartScreen shows "Windows protected your PC" the first time —
choose **More info → Run anyway**.

Keep both `.exe` files in the same folder if you want `service install` to work.

### Option B — run from source

Needs Python 3.8+ (preinstalled on macOS; on Windows get it from
[python.org](https://www.python.org/downloads/) or the Microsoft Store).

```sh
git clone https://github.com/GHWang28/clip-studio-rich-presence.git
cd clip-studio-rich-presence
./csprpc.sh doctor      # macOS
csprpc.cmd doctor       # Windows
```

Throughout this README, `csprpc` means whichever of these you are using:
`./csprpc.sh` (macOS source), `csprpc.cmd` (Windows source), or the downloaded
executable.

## Setup

There is no Discord account setup to do. Rich Presence always displays as
*"Playing «application name»"*, and the application that supplies the
"CLIP STUDIO PAINT" label and the artwork is built in, so the only thing left
is a permission on macOS.

### 1. Grant Accessibility access — macOS only

Reading the canvas window title is how the file name is detected, and macOS
gates that behind a privacy permission. **On Windows there is nothing to grant;
skip this step.**

Open **System Settings → Privacy & Security → Accessibility** and enable
whichever app you launch this from — Terminal, iTerm, or your editor. Check
**Privacy & Security → Automation** too and allow that app to control
**System Events**.

If you would rather not grant this, see [Running without Accessibility](#running-without-accessibility-macos).

### 2. Check everything

```sh
./csprpc.sh doctor
```

This verifies each piece separately — permissions, whether CLIP STUDIO PAINT is
detected, whether Discord is reachable, and whether the handshake is accepted —
and tells you how to fix whatever is not working. Run it with CLIP STUDIO PAINT
open and a canvas loaded for the most useful output.

### 3. Run it

```sh
./csprpc.sh run
```

Leave it running while you draw. Press `Ctrl-C` to stop; your presence is
cleared and your tracked time is saved on the way out.

To start it automatically at login instead:

```sh
./csprpc.sh service install
```

On macOS that installs a launch agent; on Windows it registers a Task Scheduler
task that runs at logon under `pythonw.exe`, so no console window appears.

> **macOS only: where you keep this project matters for the launch agent.** A
> launch agent does not inherit the folder access your terminal has been
> granted, so if the project lives in `~/Desktop`, `~/Documents` or
> `~/Downloads`, macOS blocks it from reading its own code and it will not
> start. `service install` detects this and tells you; the fix is to move the
> project somewhere unprotected (`~/csprpc` works) or grant Full Disk Access to
> your `python3`. Running `./csprpc.sh run` in a terminal is unaffected either
> way, and Windows has no equivalent restriction.

## Commands

| Command | What it does |
| --- | --- |
| `run` | Watch CLIP STUDIO PAINT and update Discord |
| `run --dry-run` | Track time and print the presence without contacting Discord |
| `watch` | Print what is detected each poll, for tuning detection |
| `doctor` | Diagnose permissions, detection and the Discord connection |
| `stats` | Show time per file and per day |
| `config show` / `config edit` / `config set` | Inspect and change settings |
| `service install` / `uninstall` / `status` | Run automatically at login |

`watch` is the one to reach for if the wrong thing shows up on your profile: it
prints the detected document, whether the app is frontmost, and your idle time
every few seconds, without sending anything to Discord.

## How time is tracked

Time accrues only while CLIP STUDIO PAINT is running and you are actually at the
machine. After `idle_timeout_seconds` (5 minutes by default) with no keyboard or
mouse input, the clock pauses and the presence switches to its "Away" wording.
Time also stops if the machine sleeps.

Totals are kept per file and per day in a local `stats.json`, which never leaves
your machine. Discord only ever receives the two short lines of text you see in
your presence.

| Platform | Config and stats live in |
| --- | --- |
| macOS | `~/Library/Application Support/ClipStudioRichPresence/` |
| Windows | `%APPDATA%\ClipStudioRichPresence\` |

```sh
./csprpc.sh stats
```

```
Today   2:14:31
Total  61:48:02

Time per file
  Portrait_final.clip     12:31:44
  cover_rough.clip         6:02:10
```

## Configuration

`config edit` opens the JSON config. The settings you are most likely to touch:

| Setting | Default | Meaning |
| --- | --- | --- |
| `client_id` | built-in | Discord Application ID; see [using your own application](#using-your-own-discord-application) |
| `poll_interval_seconds` | `5` | How often to check CLIP STUDIO PAINT |
| `idle_timeout_seconds` | `300` | Input silence before the clock pauses |
| `require_frontmost` | `false` | Only count time when CSP is the active app |
| `clear_presence_when_idle` | `false` | Hide the presence entirely while away |
| `presence.elapsed` | `"file"` | What Discord's timer counts: `file`, `session`, `today` or `none` |
| `privacy.show_file_name` | `true` | Send the file name at all |
| `privacy.hide_extension` | `false` | Show `Portrait` instead of `Portrait.clip` |

The same config file works on both platforms; the settings that do not apply to
your OS are simply ignored.

Discord only accepts one presence update every 15 seconds, so changes are
coalesced and the newest one wins. Lowering `poll_interval_seconds` below that
makes detection snappier but will not make your profile update faster.

### Wording

`presence.templates` controls the two lines Discord shows, for each of the three
states (`working`, `idle`, `no_document`):

```json
"working": {
  "details": "{doc}{modified}",
  "state": "{today_time} today"
}
```

Available placeholders:

| Placeholder | Example |
| --- | --- |
| `{doc}` | `Portrait_final.clip` |
| `{stem}` / `{ext}` | `Portrait_final` / `clip` |
| `{modified}` | ` *` when there are unsaved changes |
| `{file_time}` | time on this file this session |
| `{file_time_total}` | time on this file ever |
| `{session_time}` | time since the tool started |
| `{today_time}` / `{total_time}` | time today / all time |

### Privacy

If you want the presence without broadcasting what you are working on, set
`privacy.show_file_name` to `false`. Discord then sees only the placeholder from
`privacy.redacted_name` (`a drawing`), while your local stats still record the
real names.

### Using your own Discord application

`client_id` ships pointing at a built-in application, which is what makes the
presence read *"Playing CLIP STUDIO PAINT"* and supplies its icons. Replace it
only if you want a different name shown or your own artwork:

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. Name it exactly what you want Discord to display.
3. Copy the **Application ID** from **General Information**.
4. Under **Rich Presence → Art Assets**, upload images named `csp` (large icon),
   `brush` (badge while drawing) and `idle` (badge while away). Rename these in
   the config's `assets` block if you prefer other names; any you skip are just
   not shown.

```sh
csprpc config set client_id 123456789012345678
```

Setting it to an empty string restores the built-in one.

## How it works

Every poll the tool takes one snapshot of the system, using only what the
operating system already provides:

| Question | macOS | Windows |
| --- | --- | --- |
| Is it running? | `ps`, confirmed against the bundle ID | `CreateToolhelp32Snapshot` |
| Are you in it? | `lsappinfo front` | `GetForegroundWindow` |
| Are you there? | `ioreg` HID idle time | `GetLastInputInfo` |
| Which file? | window title via System Events | `EnumWindows` + `GetWindowTextW` |
| Permission needed | **Accessibility** | **none** |

That snapshot becomes a Discord activity, which is written to Discord's local
IPC endpoint as length-prefixed JSON frames. The conversation is byte-for-byte
identical on both platforms; only the transport differs — a unix domain socket
at `$TMPDIR/discord-ipc-0` on macOS, a named pipe at `\\.\pipe\discord-ipc-0` on
Windows. That protocol is small enough to implement directly, which is why there
are no dependencies.

Platform code lives in `csprpc/macos.py` and `csprpc/windows.py`, both
implementing the same small interface and selected by `csprpc/system.py`.
Everything above that — time tracking, presence building, the CLI — is shared.

### Running without Accessibility (macOS)

If you skip the Accessibility permission on macOS, the `open_files` fallback
takes over: it asks `lsof` which artwork files the process currently has open.
It needs no permission, but it is less reliable — it can miss a file that CLIP
STUDIO PAINT is not holding open, and it cannot tell which canvas is in front
when several are open. The presence still works, it just may show nothing where
a file name would go.

This fallback does not exist on Windows, and is not needed there, because
reading window titles requires no permission in the first place.

Set `document.strategies` to reorder or disable either approach.

## Troubleshooting

**Nothing shows on my profile.** In Discord, check **Settings → Activity
Privacy → Display current activity as a status message** is on. Then run
`doctor`.

**It says "Invalid Client ID".** Only possible if you changed `client_id`. It
must be the *Application ID* from your application's General Information page,
not the public key or a bot token. Reset it by setting it to an empty string.

**The file name is missing but everything else works.** On macOS, Accessibility
is not granted, or no canvas is open. `doctor` prints the window titles it can
see, which shows which of the two it is.

**A palette name shows instead of my file.** Add the offending title to
`document.ignore_titles` in the config. `watch` shows what is being picked up.

**The icons are missing.** The text still works without them. If you switched to
your own application, its Art Assets names must match the `assets` names in the
config exactly, and freshly uploaded assets take a few minutes to propagate.

**The background service will not start.** Check `service status` and the log it
points at. On macOS the usual cause is the protected-folder problem described in
[step 3](#3-run-it), and the launch agent runs `python3` directly so it needs its
own Accessibility grant separate from your terminal's. On Windows, confirm
`python` is on `PATH` for your user account.

## Development

```sh
python3 -m unittest discover -s tests -t .     # macOS
py -3 -m unittest discover -s tests -t .       # Windows
```

The tests include a fake Discord that speaks the real IPC protocol over a unix
socket, so the client is exercised end to end without needing Discord running.
Those particular tests skip themselves on Windows and in sandboxes where a unix
socket cannot be bound. `tests/test_windows_live.py` is the mirror image: it
skips everywhere except Windows, where it is the only thing that actually
executes the ctypes prototypes in `csprpc/windows.py`.

### Releasing

CI runs the suite on both macOS and Windows for every push, so the Win32 code
is exercised on a real Windows machine even if you only own a Mac.

Builds are produced by PyInstaller on each platform — a Windows `.exe` cannot be
cross-compiled from macOS, which is why this goes through CI. To publish:

```sh
git tag v0.2.0
git push origin v0.2.0
```

That runs the tests, builds for Windows, Apple Silicon and Intel, smoke tests
each binary, and attaches them all to a GitHub release. You can also trigger a
build without tagging from the Actions tab ("Run workflow"), which leaves the
binaries as downloadable artifacts for 30 days.
