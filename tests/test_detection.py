import unittest

from csprpc import config as config_module
from csprpc.model import clean_title, pick_document_from_titles

EXTENSIONS = config_module.DEFAULTS["document"]["extensions"]
IGNORE = config_module.DEFAULTS["document"]["ignore_titles"]


class CleanTitleTest(unittest.TestCase):
    def test_plain_title(self):
        self.assertEqual(clean_title("Sketch.clip"), ("Sketch.clip", False))

    def test_strips_macos_edited_marker(self):
        self.assertEqual(clean_title("Sketch.clip \u2014 Edited"), ("Sketch.clip", True))
        self.assertEqual(clean_title("Sketch.clip - Edited"), ("Sketch.clip", True))

    def test_strips_asterisk_marker(self):
        self.assertEqual(clean_title("*Sketch.clip"), ("Sketch.clip", True))
        self.assertEqual(clean_title("Sketch.clip*"), ("Sketch.clip", True))

    def test_strips_app_name_suffix(self):
        self.assertEqual(clean_title("Sketch.clip - CLIP STUDIO PAINT"), ("Sketch.clip", False))
        self.assertEqual(
            clean_title("Sketch.clip \u2014 CLIP STUDIO PAINT"), ("Sketch.clip", False)
        )

    def test_strips_a_versioned_app_suffix(self):
        self.assertEqual(
            clean_title("Sketch.clip - CLIP STUDIO PAINT 3.0.4"), ("Sketch.clip", False)
        )
        self.assertEqual(
            clean_title("Sketch.clip - CLIP STUDIO PAINT EX 4.0"), ("Sketch.clip", False)
        )

    def test_does_not_eat_a_bare_app_title(self):
        self.assertEqual(clean_title("CLIP STUDIO PAINT"), ("CLIP STUDIO PAINT", False))

    def test_whitespace_only(self):
        self.assertEqual(clean_title("   "), ("", False))


class PickDocumentTest(unittest.TestCase):
    def pick(self, titles):
        return pick_document_from_titles(titles, EXTENSIONS, IGNORE)

    def test_prefers_a_title_with_a_known_extension(self):
        document = self.pick(["Tool property", "Layer", "Portrait.clip", "Navigator"])
        self.assertIsNotNone(document)
        self.assertEqual(document.name, "Portrait.clip")
        self.assertEqual(document.source, "window_title")

    def test_ignores_palette_windows(self):
        self.assertIsNone(self.pick(["Layer", "Navigator", "Sub Tool Detail", "CLIP STUDIO PAINT"]))

    def test_ignores_a_versioned_app_title(self):
        # Recent Windows builds title the frame this way and never mention
        # the canvas. That must not become the "document".
        self.assertIsNone(self.pick(["CLIP STUDIO PAINT 3.0.4", "Layer"]))

    def test_ignore_list_is_case_insensitive(self):
        self.assertIsNone(self.pick(["LAYER", "navigator"]))

    def test_falls_back_to_first_unknown_title(self):
        document = self.pick(["Layer", "Untitled"])
        self.assertIsNotNone(document)
        self.assertEqual(document.name, "Untitled")

    def test_carries_modified_flag_through(self):
        document = self.pick(["*Portrait.clip"])
        self.assertTrue(document.modified)
        self.assertEqual(document.name, "Portrait.clip")

    def test_no_windows(self):
        self.assertIsNone(self.pick([]))


if __name__ == "__main__":
    unittest.main()
