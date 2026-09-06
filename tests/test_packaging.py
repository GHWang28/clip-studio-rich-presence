import importlib.util
import tempfile
import unittest
from pathlib import Path

PACKAGING = Path(__file__).resolve().parent.parent / "packaging"


def _load(name: str):
    path = PACKAGING / name
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class VersionInfoTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load("write_version_info.py")

    def test_pads_to_four_integers(self):
        self.assertEqual(self.mod.version_tuple("0.3.2"), (0, 3, 2, 0))

    def test_strips_a_prerelease_suffix(self):
        self.assertEqual(self.mod.version_tuple("1.2.3rc1"), (1, 2, 3, 0))

    def test_render_embeds_the_version(self):
        text = self.mod.render("0.3.2")
        self.assertIn("filevers=(0, 3, 2, 0)", text)
        self.assertIn("FileVersion', '0.3.2'", text)
        self.assertIn("OriginalFilename', 'csprpc.exe'", text)

    def test_writer_matches_the_declared_version(self):
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "info.txt"
            self.assertEqual(self.mod.main([str(out)]), 0)
            text = out.read_text(encoding="utf-8")
            self.assertIn("csprpc.exe", text)
            self.assertIn("FileVersion", text)


if __name__ == "__main__":
    unittest.main()
