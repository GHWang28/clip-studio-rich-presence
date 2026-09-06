import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from csprpc import cli
from csprpc.cli import _agent_startup_error, _protected_location


class ServiceCommandTest(unittest.TestCase):
    """How the background service is told to start itself."""

    def test_from_source_it_runs_the_module(self):
        with mock.patch.object(cli, "_is_frozen", return_value=False):
            argv = cli._service_argv()
        self.assertEqual(argv[1:], ["-m", "csprpc", "gui", "--hidden"])
        self.assertTrue(argv[0])

    def test_frozen_it_runs_itself(self):
        # `-m csprpc` is meaningless inside a bundle: the exe is the entry point.
        with mock.patch.object(cli, "_is_frozen", return_value=True):
            argv = cli._service_argv()
        self.assertEqual(argv[1:], ["gui", "--hidden"])
        self.assertNotIn("-m", argv)

    def test_login_starts_the_window_minimised(self):
        # Otherwise logging in would pop the window open every time.
        with mock.patch.object(cli, "_is_frozen", return_value=True):
            self.assertIn("--hidden", cli._service_argv())

    def test_app_location_from_source_is_the_project(self):
        with mock.patch.object(cli, "_is_frozen", return_value=False):
            self.assertTrue((cli._app_location() / "csprpc").is_dir())

    def test_app_location_frozen_is_beside_the_executable(self):
        with mock.patch.object(cli, "_is_frozen", return_value=True):
            self.assertEqual(cli._app_location(), Path(sys.executable).resolve().parent)

    def test_windows_frozen_uses_itself(self):
        # The single build is already windowed, so there is no quiet sibling
        # to prefer any more.
        exe = Path(r"C:\Apps\csprpc.exe")
        with mock.patch.object(cli, "_is_frozen", return_value=True), \
             mock.patch.object(cli, "IS_WINDOWS", True), \
             mock.patch.object(cli.sys, "executable", str(exe)), \
             mock.patch.object(Path, "exists", lambda self: True):
            self.assertEqual(cli._background_launcher(), str(exe))

    def test_windows_from_source_prefers_pythonw(self):
        exe = Path(r"C:\Python\python.exe")
        with mock.patch.object(cli, "_is_frozen", return_value=False), \
             mock.patch.object(cli, "IS_WINDOWS", True), \
             mock.patch.object(cli.sys, "executable", str(exe)), \
             mock.patch.object(Path, "exists", lambda self: self.name == "pythonw.exe"):
            self.assertEqual(cli._background_launcher(), str(exe.with_name("pythonw.exe")))

    def test_falls_back_when_pythonw_is_absent(self):
        exe = Path(r"C:\Python\python.exe")
        with mock.patch.object(cli, "_is_frozen", return_value=False), \
             mock.patch.object(cli, "IS_WINDOWS", True), \
             mock.patch.object(cli.sys, "executable", str(exe)), \
             mock.patch.object(Path, "exists", lambda self: False):
            self.assertEqual(cli._background_launcher(), str(exe))


@unittest.skipUnless(sys.platform == "darwin", "TCC-protected folders are macOS-only")
class ProtectedLocationTest(unittest.TestCase):
    def test_desktop_is_protected(self):
        self.assertEqual(_protected_location(Path.home() / "Desktop" / "code" / "app"), "Desktop")

    def test_documents_and_downloads_are_protected(self):
        self.assertEqual(_protected_location(Path.home() / "Documents" / "x"), "Documents")
        self.assertEqual(_protected_location(Path.home() / "Downloads" / "x"), "Downloads")

    def test_ordinary_home_folder_is_fine(self):
        self.assertIsNone(_protected_location(Path.home() / "code" / "app"))

    def test_outside_home_is_fine(self):
        self.assertIsNone(_protected_location(Path("/opt/csprpc")))

    def test_folder_merely_named_like_one_is_fine(self):
        self.assertIsNone(_protected_location(Path.home() / "work" / "Desktop"))


@unittest.skipIf(sys.platform == "darwin", "covered by ProtectedLocationTest")
class ProtectedLocationElsewhereTest(unittest.TestCase):
    def test_the_check_is_inert(self):
        # Only macOS gates these folders, so nothing should be reported and the
        # service installer must not warn about them.
        self.assertIsNone(_protected_location(Path.home() / "Desktop" / "app"))
        self.assertIsNone(_protected_location(Path.home() / "Documents" / "app"))


class AgentStartupErrorTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = Path(self.dir.name) / "csprpc.log"

    def tearDown(self):
        self.dir.cleanup()

    def test_healthy_startup_reports_no_error(self):
        self.log.write_text("12:00:00 INFO watching for CLIP STUDIO PAINT (poll every 5.0s)\n")
        self.assertIsNone(_agent_startup_error(self.log, timeout=1.0))

    def test_missing_module_is_reported(self):
        # This is what a launch agent that cannot read the project prints.
        self.log.write_text("/usr/bin/python3: No module named csprpc\n")
        error = _agent_startup_error(self.log, timeout=1.0)
        self.assertIsNotNone(error)
        self.assertIn("No module named csprpc", error)

    def test_traceback_is_reported(self):
        self.log.write_text("Traceback (most recent call last):\n  ...\nValueError: bad\n")
        self.assertIn("ValueError", _agent_startup_error(self.log, timeout=1.0))

    def test_permission_denied_is_reported(self):
        self.log.write_text("Permission denied: /Users/me/Desktop/app\n")
        self.assertIn("Permission denied", _agent_startup_error(self.log, timeout=1.0))

    def test_silent_log_is_not_treated_as_failure(self):
        self.log.write_text("")
        self.assertIsNone(_agent_startup_error(self.log, timeout=0.5))

    def test_missing_log_is_not_treated_as_failure(self):
        self.assertIsNone(_agent_startup_error(self.log, timeout=0.5))


if __name__ == "__main__":
    unittest.main()
