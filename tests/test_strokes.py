import unittest

from csprpc.strokes import StrokeCounter


class StrokeCounterTest(unittest.TestCase):
    def test_counts_a_press_while_drawing(self):
        counter = StrokeCounter()
        counter.sample(False, True)
        counter.sample(True, True)
        self.assertEqual(counter.totals(), (1, 1))

    def test_holding_the_button_is_one_stroke(self):
        counter = StrokeCounter()
        counter.sample(True, True)
        counter.sample(True, True)
        counter.sample(True, True)
        self.assertEqual(counter.file_count, 1)

    def test_ignores_presses_when_csp_is_not_in_front(self):
        counter = StrokeCounter()
        counter.sample(False, False)
        counter.sample(True, False)
        self.assertEqual(counter.session_count, 0)

    def test_switching_files_resets_only_the_canvas_count(self):
        counter = StrokeCounter()
        counter.set_document("A.clip")
        counter.sample(False, True)
        counter.sample(True, True)
        counter.sample(False, True)
        counter.set_document("B.clip")
        self.assertEqual(counter.file_count, 0)
        self.assertEqual(counter.session_count, 1)

    def test_same_file_keeps_the_canvas_count(self):
        counter = StrokeCounter()
        counter.set_document("A.clip")
        counter.sample(False, True)
        counter.sample(True, True)
        counter.set_document("A.clip")
        self.assertEqual(counter.file_count, 1)


if __name__ == "__main__":
    unittest.main()
