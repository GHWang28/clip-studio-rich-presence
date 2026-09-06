"""Tests for the Windows backend's platform-independent logic.

The ctypes entry points can only run on Windows, but the module must import
everywhere and its pure logic is testable anywhere.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from csprpc import windows
from csprpc.model import DocumentInfo


class ImportSafetyTest(unittest.TestCase):
    def test_module_imports_off_windows(self):
        # A broken guard here would take down `import csprpc.system` on macOS.
        self.assertEqual(windows.PLATFORM, "Windows")
        self.assertFalse(windows.NEEDS_WINDOW_PERMISSION)

    def test_win32_calls_refuse_to_run_off_windows(self):
        if windows.IS_WINDOWS:
            self.skipTest("running on Windows")
        for call in (windows.list_processes, windows.frontmost_pid, windows.idle_seconds):
            with self.assertRaises(RuntimeError):
                call()

    def test_find_app_path_is_safe_off_windows(self):
        if windows.IS_WINDOWS:
            self.skipTest("running on Windows")
        self.assertIsNone(windows.find_app_path())


class IdleTicksTest(unittest.TestCase):
    def test_plain_difference(self):
        self.assertAlmostEqual(windows.idle_from_ticks(10_000, 4_000), 6.0)

    def test_zero_when_input_is_current(self):
        self.assertEqual(windows.idle_from_ticks(10_000, 10_000), 0.0)

    def test_survives_the_49_day_tick_wrap(self):
        # GetTickCount wraps at 2**32 ms; naive subtraction would go negative.
        just_before_wrap = 0xFFFFFFFF - 1_000
        just_after_wrap = 2_000
        self.assertAlmostEqual(
            windows.idle_from_ticks(just_after_wrap, just_before_wrap), 3.001, places=3
        )

    def test_never_negative_across_the_wrap(self):
        for now, last in ((0, 0xFFFFFFFF), (5, 0xFFFFFFF0), (0xFFFFFFFF, 0)):
            self.assertGreaterEqual(windows.idle_from_ticks(now, last), 0.0)


class FindProcessTest(unittest.TestCase):
    MATCH = {"name_contains": ["clipstudiopaint", "clip studio paint"]}

    def find(self, processes):
        with mock.patch.object(windows, "list_processes", return_value=processes):
            return windows.find_process(self.MATCH)

    def test_matches_the_windows_executable(self):
        found = self.find(
            [
                (10, r"C:\Windows\explorer.exe"),
                (20, r"C:\Program Files\CELSYS\CLIP STUDIO 1.5\CLIP STUDIO PAINT\CLIPStudioPaint.exe"),
            ]
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.pid, 20)
        self.assertEqual(found.app_name, "CLIPStudioPaint")

    def test_matches_a_bare_executable_name(self):
        # QueryFullProcessImageName can be refused, leaving only the exe name.
        found = self.find([(30, "CLIPStudioPaint.exe")])
        self.assertIsNotNone(found)
        self.assertEqual(found.pid, 30)

    def test_prefers_the_main_process_over_a_subprocess(self):
        found = self.find(
            [
                (40, r"C:\CELSYS\CLIPStudioPaintSubProcess.exe"),
                (41, r"C:\CELSYS\CLIPStudioPaint.exe"),
            ]
        )
        self.assertEqual(found.pid, 41)

    def test_falls_back_to_a_subprocess_when_alone(self):
        found = self.find([(40, r"C:\CELSYS\CLIPStudioPaintSubProcess.exe")])
        self.assertEqual(found.pid, 40)

    def test_returns_none_when_absent(self):
        self.assertIsNone(self.find([(10, r"C:\Windows\explorer.exe")]))


class DetectDocumentTest(unittest.TestCase):
    CONFIG = {
        "strategies": ["window_title", "open_files"],
        "extensions": [".clip", ".psd"],
        "ignore_titles": ["layer", "navigator", "clip studio paint"],
    }

    def test_reads_the_document_from_the_title(self):
        with mock.patch.object(
            windows, "window_titles", return_value=["Layer", "Portrait.clip - CLIP STUDIO PAINT"]
        ):
            document = windows.detect_document(1, self.CONFIG)
        self.assertEqual(document, DocumentInfo("Portrait.clip", False, None, "window_title"))

    def test_falls_back_to_a_mapped_file(self):
        notes = []
        with mock.patch.object(windows, "window_titles", return_value=["CLIP STUDIO PAINT"]), \
             mock.patch.object(
                 windows, "open_document_paths", return_value=[r"C:\Art\Portrait.clip"]
             ):
            document = windows.detect_document(1, self.CONFIG, notes)
        self.assertEqual(
            document, DocumentInfo("Portrait.clip", False, r"C:\Art\Portrait.clip", "open_files")
        )

    def test_notes_when_neither_strategy_finds_a_canvas(self):
        notes = []
        with mock.patch.object(windows, "window_titles", return_value=["CLIP STUDIO PAINT"]), \
             mock.patch.object(windows, "open_document_paths", return_value=[]):
            self.assertIsNone(windows.detect_document(1, self.CONFIG, notes))
        self.assertTrue(any("chrome" in note for note in notes))
        self.assertTrue(any("open_files" in note for note in notes))


class OwnershipFileTest(unittest.TestCase):
    LINE = (
        "4:742DEA58-ED6B-4402-BC11-20DFC6D08040:16488de86a-2141-dfa1-cd6c-3f661209bb:"
        "ACCACD33-5596-43EA-B34B-92DD925E66C8:C:\\Users\\me\\Art\\Summer.clip"
    )

    def test_reads_the_drive_path_after_the_guids(self):
        self.assertEqual(
            windows.parse_ownership_line(self.LINE),
            r"C:\Users\me\Art\Summer.clip",
        )

    def test_reads_a_unc_path(self):
        self.assertEqual(
            windows.parse_ownership_line(r"4:guid:\\server\share\cover.clip"),
            r"\\server\share\cover.clip",
        )

    def test_ignores_a_line_without_a_path(self):
        self.assertIsNone(windows.parse_ownership_line("4:guid:session:document"))
        self.assertIsNone(windows.parse_ownership_line(""))
        self.assertIsNone(windows.parse_ownership_line("   "))

    def test_read_ownership_paths_skips_missing_files(self):
        self.assertEqual(windows.read_ownership_paths(r"C:\definitely-missing-owner.txt"), [])

    def test_read_ownership_paths_parses_each_line(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "owner.txt"
            path.write_text(self.LINE + "\n4:guid:session:id:D:\\other.psd\n", encoding="utf-8")
            self.assertEqual(
                windows.read_ownership_paths(str(path)),
                [r"C:\Users\me\Art\Summer.clip", r"D:\other.psd"],
            )

    def test_open_document_paths_prefers_the_ownership_file(self):
        with mock.patch.object(windows, "read_ownership_paths", return_value=[r"C:\Art\Summer.clip"]), \
             mock.patch.object(windows, "_mapped_document_paths", return_value=[r"C:\Art\old.clip"]):
            self.assertEqual(
                windows.open_document_paths(1, [".clip"]),
                [r"C:\Art\Summer.clip"],
            )

    def test_open_document_paths_falls_back_to_mapped_files(self):
        with mock.patch.object(windows, "read_ownership_paths", return_value=[]), \
             mock.patch.object(windows, "_mapped_document_paths", return_value=[r"C:\Art\Portrait.clip"]):
            self.assertEqual(
                windows.open_document_paths(1, [".clip"]),
                [r"C:\Art\Portrait.clip"],
            )


class NtToDosTest(unittest.TestCase):
    def test_replaces_the_longest_device_prefix(self):
        drives = {
            r"\Device\HarddiskVolume3": "C:",
            r"\Device\HarddiskVolume3\Users": "Z:",
        }
        self.assertEqual(
            windows.nt_to_dos(r"\Device\HarddiskVolume3\Users\a\art.clip", drives),
            r"Z:\a\art.clip",
        )

    def test_leaves_an_unknown_device_alone(self):
        self.assertEqual(
            windows.nt_to_dos(r"\Device\Mup\share\art.clip", {}),
            r"\Device\Mup\share\art.clip",
        )


class RunCommandTest(unittest.TestCase):
    def test_runs_without_the_windows_only_flag(self):
        # Not `echo`: that is a cmd builtin on Windows, not an executable.
        code, out, _ = windows.run_command([sys.executable, "-c", "print('hello')"])
        self.assertEqual(code, 0)
        self.assertIn("hello", out)

    def test_missing_command_is_reported_not_raised(self):
        code, _, err = windows.run_command(["definitely-not-a-real-command-xyz"])
        self.assertEqual(code, 127)
        self.assertIn("not found", err)


class InstallPathsTest(unittest.TestCase):
    def test_covers_both_program_files_roots(self):
        patterns = windows.candidate_install_paths()
        self.assertTrue(any("Program Files" in p for p in patterns))
        self.assertTrue(all(p.endswith("CLIPStudioPaint.exe") for p in patterns))


if __name__ == "__main__":
    unittest.main()
