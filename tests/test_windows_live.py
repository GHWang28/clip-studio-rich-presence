"""Win32 calls exercised against a real Windows machine.

These skip everywhere else. They are the only place the ctypes structure
definitions and function prototypes in csprpc/windows.py actually execute, so
CI on a Windows runner is what proves them correct.
"""

import copy
import os
import sys
import unittest

from csprpc import config as config_module
from csprpc import discord_ipc, windows

# Two months, comfortably past anything sane but below the 49.7 day tick wrap
# producing a bogus huge value.
_ABSURD_IDLE = 60 * 60 * 24 * 60


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class LiveWin32Test(unittest.TestCase):
    def test_list_processes_returns_real_processes(self):
        processes = windows.list_processes()
        self.assertGreater(len(processes), 5)
        for pid, executable in processes:
            self.assertIsInstance(pid, int)
            self.assertIsInstance(executable, str)
        names = " ".join(name.lower() for _, name in processes)
        self.assertIn(".exe", names)

    def test_finds_its_own_process(self):
        pids = {pid for pid, _ in windows.list_processes()}
        self.assertIn(os.getpid(), pids)

    def test_idle_seconds_is_plausible(self):
        value = windows.idle_seconds()
        self.assertIsInstance(value, float)
        self.assertGreaterEqual(value, 0.0)
        self.assertLess(value, _ABSURD_IDLE)

    def test_frontmost_pid_does_not_crash(self):
        # A CI runner may have no interactive desktop, so None is acceptable.
        pid = windows.frontmost_pid()
        self.assertTrue(pid is None or isinstance(pid, int))

    def test_window_titles_returns_strings(self):
        titles = windows.window_titles(os.getpid())
        self.assertIsInstance(titles, list)
        for title in titles:
            self.assertIsInstance(title, str)

    def test_enumerating_windows_for_many_processes_is_safe(self):
        # Exercises EnumWindows and GetWindowTextW against whatever is running,
        # including processes that vanish mid-enumeration.
        for pid, _ in windows.list_processes()[:25]:
            windows.window_titles(pid)

    def test_find_app_path_does_not_crash(self):
        result = windows.find_app_path()
        self.assertTrue(result is None or isinstance(result, str))

    def test_full_observation(self):
        cfg = copy.deepcopy(config_module.DEFAULTS)
        observation = windows.observe(cfg)
        # CLIP STUDIO PAINT is not installed on a CI runner.
        self.assertFalse(observation.running)
        self.assertIsNone(observation.document)

    def test_observation_of_a_stand_in_process(self):
        cfg = copy.deepcopy(config_module.DEFAULTS)
        cfg["process_match"] = {"bundle_ids": [], "name_contains": ["explorer.exe"]}
        observation = windows.observe(cfg)
        if not observation.running:
            self.skipTest("explorer.exe is not running on this runner")
        self.assertIsNotNone(observation.process)
        self.assertGreaterEqual(observation.idle_seconds, 0.0)

    def test_endpoint_discovery_uses_named_pipes(self):
        # No Discord on a runner, so this must simply return None, not raise.
        self.assertIsNone(discord_ipc.find_endpoint())

    def test_config_lands_in_appdata(self):
        os.environ.pop("CSPRPC_HOME", None)
        self.assertIn("ClipStudioRichPresence", str(config_module.data_dir()))


if __name__ == "__main__":
    unittest.main()
