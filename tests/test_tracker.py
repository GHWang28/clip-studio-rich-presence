import tempfile
import unittest
from pathlib import Path

from csprpc.tracker import Tracker, humanize, humanize_precise


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "stats.json"
        self.clock = FakeClock()
        self.tracker = Tracker(self.path, save_interval=60.0, clock=self.clock)

    def tearDown(self):
        self.dir.cleanup()

    def tick(self, seconds, active=True, key="Sketch.clip"):
        self.clock.advance(seconds)
        return self.tracker.tick(active, key, max_delta=30.0)

    def test_first_tick_credits_nothing(self):
        self.assertEqual(self.tracker.tick(True, "Sketch.clip", 30.0), 0.0)
        self.assertEqual(self.tracker.session_seconds, 0.0)

    def test_accrues_only_while_active(self):
        self.tick(0)  # establish the baseline
        self.tick(10, active=True)
        self.tick(10, active=False)
        self.tick(5, active=True)

        self.assertAlmostEqual(self.tracker.session_seconds, 15.0)
        self.assertAlmostEqual(self.tracker.session_file_seconds("Sketch.clip"), 15.0)
        self.assertAlmostEqual(self.tracker.today_seconds(), 15.0)

    def test_long_gap_is_clamped(self):
        self.tick(0)
        credited = self.tick(3600)  # machine slept for an hour
        self.assertAlmostEqual(credited, 30.0)
        self.assertAlmostEqual(self.tracker.session_seconds, 30.0)

    def test_time_is_split_per_file(self):
        self.tick(0, key="A.clip")
        self.tick(10, key="A.clip")
        self.tick(20, key="B.clip")

        self.assertAlmostEqual(self.tracker.session_file_seconds("A.clip"), 10.0)
        self.assertAlmostEqual(self.tracker.session_file_seconds("B.clip"), 20.0)
        self.assertAlmostEqual(self.tracker.session_seconds, 30.0)

    def test_time_without_a_document_counts_toward_the_day_only(self):
        self.tick(0, key=None)
        self.tick(10, key=None)
        self.assertAlmostEqual(self.tracker.today_seconds(), 10.0)
        self.assertEqual(self.tracker.session_files, {})

    def test_totals_persist_and_reload(self):
        self.tick(0)
        self.tick(25)
        self.tracker.save(force=True)

        reloaded = Tracker(self.path, clock=FakeClock())
        self.assertAlmostEqual(reloaded.lifetime_file_seconds("Sketch.clip"), 25.0)
        self.assertAlmostEqual(reloaded.today_seconds(), 25.0)
        # A new session starts its own clock even though lifetime carries over.
        self.assertEqual(reloaded.session_seconds, 0.0)
        self.assertEqual(reloaded.session_file_seconds("Sketch.clip"), 0.0)

    def test_lifetime_accumulates_across_sessions(self):
        self.tick(0)
        self.tick(25)
        self.tracker.save(force=True)

        second = Tracker(self.path, clock=FakeClock())
        second.tick(True, "Sketch.clip", 30.0)
        second.clock.advance(15)
        second.tick(True, "Sketch.clip", 30.0)
        second.save(force=True)

        third = Tracker(self.path, clock=FakeClock())
        self.assertAlmostEqual(third.lifetime_file_seconds("Sketch.clip"), 40.0)

    def test_corrupt_stats_file_is_recovered(self):
        self.path.write_text("{not json", encoding="utf-8")
        tracker = Tracker(self.path, clock=FakeClock())
        self.assertEqual(tracker.data["files"], {})
        self.assertTrue(self.path.with_suffix(".corrupt.json").exists())

    def test_remembers_the_full_path(self):
        self.tracker.note_path("Sketch.clip", "/Users/me/Art/Sketch.clip")
        self.assertEqual(self.tracker.data["files"]["Sketch.clip"]["path"], "/Users/me/Art/Sketch.clip")


class HumanizeTest(unittest.TestCase):
    def test_humanize(self):
        self.assertEqual(humanize(0), "0s")
        self.assertEqual(humanize(45), "45s")
        self.assertEqual(humanize(90), "1m")
        self.assertEqual(humanize(3600), "1h 0m")
        self.assertEqual(humanize(7830), "2h 10m")
        self.assertEqual(humanize(-5), "0s")

    def test_humanize_precise(self):
        self.assertEqual(humanize_precise(0), "0:00:00")
        self.assertEqual(humanize_precise(3661), "1:01:01")


if __name__ == "__main__":
    unittest.main()
