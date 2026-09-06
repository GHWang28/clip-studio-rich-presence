import sys
import unittest

from csprpc import system
from csprpc.model import ProcessInfo, filter_document_paths

BACKEND_API = (
    "observe",
    "find_process",
    "frontmost_pid",
    "idle_seconds",
    "window_titles",
    "open_document_paths",
    "detect_document",
    "find_app_path",
    "list_processes",
    "run_command",
)


class BackendSelectionTest(unittest.TestCase):
    def test_picks_the_backend_for_this_platform(self):
        if sys.platform == "darwin":
            self.assertEqual(system.PLATFORM, "macOS")
            self.assertEqual(system.backend.__name__, "csprpc.macos")
        elif sys.platform == "win32":
            self.assertEqual(system.PLATFORM, "Windows")
            self.assertEqual(system.backend.__name__, "csprpc.windows")
        else:
            self.assertFalse(system.IS_SUPPORTED)

    def test_supported_platforms_are_flagged(self):
        self.assertEqual(system.IS_SUPPORTED, sys.platform in ("darwin", "win32"))

    def test_whole_backend_api_is_exposed(self):
        for name in BACKEND_API:
            self.assertTrue(callable(getattr(system, name)), name)

    def test_both_backends_implement_the_same_api(self):
        from csprpc import macos, windows

        for name in BACKEND_API:
            self.assertTrue(callable(getattr(macos, name, None)), "macos." + name)
            self.assertTrue(callable(getattr(windows, name, None)), "windows." + name)

    def test_permission_metadata_matches_the_platform(self):
        if sys.platform == "darwin":
            self.assertTrue(system.NEEDS_WINDOW_PERMISSION)
            self.assertEqual(system.WINDOW_PERMISSION_NAME, "Accessibility")
            self.assertTrue(system.WINDOW_PERMISSION_HINT)
        elif sys.platform == "win32":
            self.assertFalse(system.NEEDS_WINDOW_PERMISSION)


class AppNameTest(unittest.TestCase):
    def test_macos_bundle(self):
        info = ProcessInfo(1, "/Applications/CLIP STUDIO PAINT.app/Contents/MacOS/CLIPStudioPaint")
        self.assertEqual(info.app_name, "CLIP STUDIO PAINT")

    def test_windows_executable(self):
        info = ProcessInfo(1, r"C:\Program Files\CELSYS\CLIPStudioPaint.exe")
        self.assertEqual(info.app_name, "CLIPStudioPaint")

    def test_bare_name(self):
        self.assertEqual(ProcessInfo(1, "CLIPStudioPaint.exe").app_name, "CLIPStudioPaint")


class FilterDocumentPathsTest(unittest.TestCase):
    def test_keeps_only_matching_extensions(self):
        paths = ["/a/Portrait.clip", "/a/notes.txt", "/a/scan.psd"]
        kept = filter_document_paths(paths, [".clip", ".psd"], [])
        self.assertEqual(sorted(kept), ["/a/Portrait.clip", "/a/scan.psd"])

    def test_applies_excludes(self):
        paths = ["/Library/Application Support/x.clip", "/Users/me/Art/y.clip"]
        kept = filter_document_paths(paths, [".clip"], ["/library/application support/"])
        self.assertEqual(kept, ["/Users/me/Art/y.clip"])

    def test_excludes_match_windows_separators(self):
        paths = [r"C:\Program Files\CELSYS\template.clip", r"C:\Users\me\Art\real.clip"]
        kept = filter_document_paths(paths, [".clip"], ["/celsys/"])
        self.assertEqual(kept, [r"C:\Users\me\Art\real.clip"])

    def test_deduplicates(self):
        kept = filter_document_paths(["/a/x.clip", "/a/x.clip"], [".clip"], [])
        self.assertEqual(kept, ["/a/x.clip"])


if __name__ == "__main__":
    unittest.main()
