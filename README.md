# CLIP STUDIO PAINT → Discord Rich Presence

Shows what you are drawing on your Discord profile, and keeps a private log of
how long you spent in each file.

```
CLIP STUDIO PAINT
Portrait_final.clip *
2h 14m today
⏱ 47:12 elapsed
```

Open the app, and it starts. A small window lets you reword what Discord shows,
decide how much it says about your files, and check what is being detected.
Hide it and it keeps running in the background; close it and your presence
stops.

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
| `csprpc.exe` | Windows 10/11 |
| `csprpc-macos-arm64.tar.gz` | Apple Silicon Macs (M1 and later) |
| `csprpc-macos-x86_64.tar.gz` | Intel Macs |

One file per machine. Double-click it and the window opens.

On Windows, SmartScreen shows "Windows protected your PC" the first time —
choose **More info → Run anyway**.

On macOS, extract it **in Terminal**, then double-click `csprpc.app`:

```sh
tar -xzf csprpc-macos-arm64.tar.gz
open csprpc.app
```

Unarchiving in Finder instead of with `tar` is worth avoiding. The app is only
ad-hoc signed — Apple notarization needs a paid developer account — so if macOS
marks it as quarantined it refuses to open it, with a dialog claiming it cannot
check the file for malware. Extracting with `tar` never applies that mark, while
Finder does. If you hit the dialog anyway, clear the mark by hand:

```sh
xattr -dr com.apple.quarantine csprpc.app    # or: xattr -cr csprpc.app
```

Running [from source](#option-b--run-from-source) sidesteps all of this, since
nothing is downloaded.

### Option B — run from source

Needs Python 3.8+ (preinstalled on macOS; on Windows get it from
[python.org](https://www.python.org/downloads/) or the Microsoft Store).

```sh
git clone https://github.com/GHWang28/clip-studio-rich-presence.git
cd clip-studio-rich-presence
./csprpc.sh             # macOS — opens the window
csprpc.cmd              # Windows — opens the window
```

Add a subcommand to get the command line instead, e.g. `./csprpc.sh doctor`.
Throughout this README, `csprpc` means whichever of these you are using:
`./csprpc.sh` (macOS source), `csprpc.cmd` (Windows source), or the downloaded
app.

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

### 2. Open the app

Double-click it, and the presence starts immediately. The window has four tabs:

| Tab | What it is for |
| --- | --- |
| **Status** | What Discord is showing right now, plus today's, this session's and your all-time totals |
| **Presence** | The wording of each line, the tooltips, and which clock Discord counts up from |
| **Behaviour** | Privacy, how often to check, when to count you as away |
| **Diagnostics** | The same checks the `doctor` command runs, in a panel |

If something is not working, start on **Diagnostics** and press **Run checks**.
It tests permissions, whether CLIP STUDIO PAINT is detected, whether Discord is
reachable and whether the handshake is accepted, and says how to fix whatever is
not working. It is most useful with CLIP STUDIO PAINT open and a canvas loaded.

### 3. Hide it, or quit it

- **Hide** minimises the window. Everything keeps running, and your presence
  stays up. Click the icon in the Dock or taskbar to bring it back.
- **Quit**, or closing the window, stops the presence. Discord clears your
  activity and your tracked time is saved on the way out.

Ticking **Start at login** launches the app minimised whenever you log in. On
macOS that installs a launch agent; on Windows it registers a Task Scheduler
task. Neither one relaunches the app after you quit, so quitting stays quit
until your next login.

> **macOS only: where you keep this project matters for the launch agent.** A
> launch agent does not inherit the folder access your terminal has been
> granted, so if the project lives in `~/Desktop`, `~/Documents` or
> `~/Downloads`, macOS blocks it from reading its own code and it will not
> start. **Start at login** detects this and tells you; the fix is to move the
> project somewhere unprotected (`~/csprpc` works) or grant Full Disk Access to
> your `python3`. Opening the app yourself is unaffected either way, and Windows
> has no equivalent restriction. Downloaded builds only hit this if you keep the
> app in one of those folders.

## Commands

Everything most people need is in the window. The command line is still there
for scripting and for running from source — add a subcommand to the app or to
`./csprpc.sh`:

| Command | What it does |
| --- | --- |
| *(none)* | Open the window |
| `gui --hidden` | Open the window minimised, which is what starting at login does |
| `run` | Watch CLIP STUDIO PAINT and update Discord, with no window |
| `run --dry-run` | Track time and print the presence without contacting Discord |
| `watch` | Print what is detected each poll, for tuning detection |
| `doctor` | Diagnose permissions, detection and the Discord connection |
| `stats` | Show time per file and per day |
| `config show` / `config edit` / `config set` | Inspect and change settings |
| `service install` / `uninstall` / `status` | Run automatically at login |

`watch` is the one to reach for if the wrong thing shows up on your profile: it
prints the detected document, whether the app is frontmost, and your idle time
every few seconds, without sending anything to Discord.

The downloaded builds are windowed applications, so on Windows they have no
console to print to. Run from source if you want the command line.

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
cross-compiled from macOS, which is why this goes through CI. To publish, bump
the version in both `pyproject.toml` and `csprpc/__init__.py`, push that to
`main`, then tag it:

```sh
python3 packaging/check_version.py v0.2.1   # optional: catch a half-bump early
git tag -a v0.2.1 -m "csprpc v0.2.1"
git push origin v0.2.1
```

The tag must start with `v`; anything else builds but never publishes. CI runs
that same version check itself and fails the build if the tag disagrees with
either file, so a mismatched release cannot ship.

That runs the tests, builds for Windows, Apple Silicon and Intel, smoke tests
each binary, and attaches them all to a GitHub release it creates for the tag.
Pushing the tag is the whole trigger — don't create the release by hand on the
Releases page, or you will do the workflow's job for it (and if you leave the
"Choose a tag" box empty there, GitHub rejects it with *"tag name can't be
blank"*).

You can also trigger a build without tagging from the Actions tab ("Run
workflow"), which skips the release step and leaves the binaries as downloadable
artifacts for 30 days.
