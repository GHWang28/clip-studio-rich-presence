import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from csprpc import config as config_module
from csprpc import presence as presence_module
from csprpc.model import DocumentInfo, Observation, ProcessInfo
from csprpc.presence import (
    PresenceDaemon,
    activities_equivalent,
    build_activity,
    evaluate,
)
from csprpc.tracker import Tracker


def make_config(**overrides):
    cfg = copy.deepcopy(config_module.DEFAULTS)
    cfg["client_id"] = "123456789"
    for key, value in overrides.items():
        cfg[key] = value
    return cfg


def make_observation(
    running=True,
    document="Sketch.clip",
    modified=False,
    frontmost=True,
    idle=0.0,
):
    return Observation(
        running=running,
        process=ProcessInfo(pid=42, executable="/Applications/CLIP STUDIO PAINT.app/Contents/MacOS/CLIPStudioPaint")
        if running
        else None,
        frontmost=frontmost,
        idle_seconds=idle,
        document=DocumentInfo(name=document, modified=modified) if document else None,
    )


class EvaluateTest(unittest.TestCase):
    def test_working_when_drawing(self):
        snapshot = evaluate(make_config(), make_observation())
        self.assertEqual(snapshot.kind, "working")
        self.assertTrue(snapshot.active)
        self.assertEqual(snapshot.document_key, "Sketch.clip")

    def test_not_running(self):
        snapshot = evaluate(make_config(), make_observation(running=False))
        self.assertEqual(snapshot.kind, "not_running")
        self.assertFalse(snapshot.active)

    def test_no_document(self):
        snapshot = evaluate(make_config(), make_observation(document=None))
        self.assertEqual(snapshot.kind, "no_document")
        self.assertTrue(snapshot.active)  # still drawing time, just unlabelled

    def test_idle_after_the_timeout(self):
        cfg = make_config(idle_timeout_seconds=300)
        snapshot = evaluate(cfg, make_observation(idle=301))
        self.assertEqual(snapshot.kind, "idle")
        self.assertFalse(snapshot.active)

    def test_still_active_just_below_the_timeout(self):
        cfg = make_config(idle_timeout_seconds=300)
        self.assertTrue(evaluate(cfg, make_observation(idle=299)).active)

    def test_background_counts_when_frontmost_not_required(self):
        cfg = make_config(require_frontmost=False)
        self.assertTrue(evaluate(cfg, make_observation(frontmost=False)).active)

    def test_background_pauses_when_frontmost_required(self):
        cfg = make_config(require_frontmost=True)
        snapshot = evaluate(cfg, make_observation(frontmost=False))
        self.assertFalse(snapshot.active)
        self.assertEqual(snapshot.kind, "idle")


class BuildActivityTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.tracker = Tracker(Path(self.dir.name) / "stats.json")

    def tearDown(self):
        self.dir.cleanup()

    def build(self, cfg, observation):
        return build_activity(cfg, self.tracker, evaluate(cfg, observation))

    def test_shows_the_file_name(self):
        activity = self.build(make_config(), make_observation())
        self.assertEqual(activity["details"], "Sketch.clip")
        self.assertIn("today", activity["state"])
        self.assertEqual(activity["assets"]["large_image"], "csp")
        self.assertEqual(activity["assets"]["small_image"], "brush")

    def test_marks_unsaved_changes(self):
        activity = self.build(make_config(), make_observation(modified=True))
        self.assertEqual(activity["details"], "Sketch.clip *")

    def test_idle_uses_the_idle_wording_and_icon(self):
        activity = self.build(make_config(), make_observation(idle=999))
        self.assertTrue(activity["state"].startswith("Away"))
        self.assertEqual(activity["assets"]["small_image"], "idle")

    def test_nothing_when_not_running(self):
        self.assertIsNone(self.build(make_config(), make_observation(running=False)))

    def test_nothing_when_idle_and_configured_to_hide(self):
        cfg = make_config(clear_presence_when_idle=True)
        self.assertIsNone(self.build(cfg, make_observation(idle=999)))

    def test_privacy_hides_the_name(self):
        cfg = make_config()
        cfg["privacy"]["show_file_name"] = False
        activity = self.build(cfg, make_observation())
        self.assertEqual(activity["details"], "a drawing")
        self.assertNotIn("Sketch", str(activity))

    def test_privacy_hides_only_the_extension(self):
        cfg = make_config()
        cfg["privacy"]["hide_extension"] = True
        self.assertEqual(self.build(cfg, make_observation())["details"], "Sketch")

    def test_no_document_template(self):
        activity = self.build(make_config(), make_observation(document=None))
        self.assertEqual(activity["details"], "No canvas open")

    def test_elapsed_none_omits_timestamps(self):
        cfg = make_config()
        cfg["presence"]["elapsed"] = "none"
        self.assertNotIn("timestamps", self.build(cfg, make_observation()))

    def test_elapsed_backdates_by_time_already_banked(self):
        self.tracker.tick(True, "Sketch.clip", 30.0)
        self.tracker.session_files["Sketch.clip"] = 600.0
        activity = self.build(make_config(), make_observation())
        self.assertAlmostEqual(activity["timestamps"]["start"], time.time() - 600, delta=2)

    def test_long_names_are_truncated_to_discord_limits(self):
        activity = self.build(make_config(), make_observation(document="x" * 400 + ".clip"))
        self.assertEqual(len(activity["details"]), 128)

    def test_unknown_placeholder_does_not_crash(self):
        cfg = make_config()
        cfg["presence"]["templates"]["working"]["state"] = "{nope} and {today_time}"
        activity = self.build(cfg, make_observation())
        self.assertIn("{nope}", activity["state"])

    def test_buttons_are_capped_at_two(self):
        cfg = make_config()
        cfg["presence"]["buttons"] = [
            {"label": "One", "url": "https://example.com/1"},
            {"label": "Two", "url": "https://example.com/2"},
            {"label": "Three", "url": "https://example.com/3"},
        ]
        self.assertEqual(len(self.build(cfg, make_observation())["buttons"]), 2)


class ActivitiesEquivalentTest(unittest.TestCase):
    def test_identical(self):
        a = {"details": "x", "timestamps": {"start": 100}}
        self.assertTrue(activities_equivalent(a, dict(a)))

    def test_small_timestamp_drift_is_ignored(self):
        a = {"details": "x", "timestamps": {"start": 100}}
        b = {"details": "x", "timestamps": {"start": 105}}
        self.assertTrue(activities_equivalent(a, b))

    def test_large_timestamp_drift_counts_as_a_change(self):
        a = {"details": "x", "timestamps": {"start": 100}}
        b = {"details": "x", "timestamps": {"start": 400}}
        self.assertFalse(activities_equivalent(a, b))

    def test_different_details(self):
        self.assertFalse(activities_equivalent({"details": "a"}, {"details": "b"}))

    def test_none_handling(self):
        self.assertTrue(activities_equivalent(None, None))
        self.assertFalse(activities_equivalent(None, {"details": "a"}))


class FakeIPC:
    created = []

    def __init__(self, client_id, timeout=5.0):
        self.client_id = client_id
        self.connected = False
        self.sent = []
        self.user = {"username": "tester"}
        self.endpoint = "/tmp/fake-discord-ipc-0"
        FakeIPC.created.append(self)

    def connect(self):
        self.connected = True

    def pump(self):
        pass

    def set_activity(self, activity, pid=None):
        self.sent.append(activity)

    def clear_activity(self, pid=None):
        self.set_activity(None)

    def close(self, notify=True):
        self.connected = False


class DaemonTest(unittest.TestCase):
    def setUp(self):
        FakeIPC.created = []
        self.dir = tempfile.TemporaryDirectory()
        self.tracker = Tracker(Path(self.dir.name) / "stats.json")
        self.cfg = make_config()
        self.observation = make_observation()

        patcher = mock.patch.object(presence_module, "DiscordIPC", FakeIPC)
        patcher.start()
        self.addCleanup(patcher.stop)

        observe = mock.patch.object(
            presence_module.system, "observe", side_effect=lambda cfg: self.observation
        )
        observe.start()
        self.addCleanup(observe.stop)

        self.daemon = PresenceDaemon(self.cfg, self.tracker)

    def tearDown(self):
        self.dir.cleanup()

    @property
    def ipc(self):
        return FakeIPC.created[-1]

    def test_first_poll_connects_and_sends(self):
        self.daemon.step()
        self.assertEqual(len(FakeIPC.created), 1)
        self.assertTrue(self.ipc.connected)
        self.assertEqual(len(self.ipc.sent), 1)
        self.assertEqual(self.ipc.sent[0]["details"], "Sketch.clip")

    def test_first_update_is_not_delayed_by_the_rate_limiter(self):
        # time.monotonic() starts near zero at process start, so a naive
        # "last sent at 0.0" would swallow the first 15 seconds of presence.
        self.assertIsNone(self.daemon._last_send_mono)
        self.daemon.step()
        self.assertEqual(len(self.ipc.sent), 1)
        self.assertIsNotNone(self.daemon._last_send_mono)

    def test_reconnect_sends_immediately_rather_than_waiting(self):
        self.daemon.step()
        self.daemon._handle_disconnect(RuntimeError("Discord restarted"))
        self.daemon._next_connect_mono = 0
        self.daemon.step()
        self.assertEqual(len(FakeIPC.created), 2)
        self.assertEqual(len(self.ipc.sent), 1)

    def test_unchanged_state_does_not_resend(self):
        self.daemon.step()
        self.daemon._last_send_mono -= 60  # pretend the rate limit window passed
        self.daemon.step()
        self.assertEqual(len(self.ipc.sent), 1)

    def test_rapid_changes_are_coalesced_into_one_update(self):
        self.daemon.step()
        for name in ("A.clip", "B.clip", "C.clip"):
            self.observation = make_observation(document=name)
            self.daemon.step()
        # Only the initial send got through; the rest are inside the 15s window.
        self.assertEqual(len(self.ipc.sent), 1)

        self.daemon._last_send_mono -= 60
        self.daemon.step()
        self.assertEqual(len(self.ipc.sent), 2)
        self.assertEqual(self.ipc.sent[1]["details"], "C.clip")

    def test_quitting_the_app_clears_the_presence(self):
        self.daemon.step()
        self.observation = make_observation(running=False)
        self.daemon.step()
        self.assertIsNone(self.ipc.sent[-1])
        self.assertFalse(self.ipc.connected)

    def test_reconnect_after_the_app_returns(self):
        self.daemon.step()
        self.observation = make_observation(running=False)
        self.daemon.step()

        self.observation = make_observation(document="Later.clip")
        self.daemon._next_connect_mono = 0
        self.daemon.step()
        self.assertEqual(len(FakeIPC.created), 2)
        self.assertEqual(self.ipc.sent[-1]["details"], "Later.clip")

    def test_dry_run_never_touches_discord(self):
        daemon = PresenceDaemon(self.cfg, self.tracker, dry_run=True)
        daemon.step()
        self.assertEqual(FakeIPC.created, [])

    def test_time_accrues_across_polls(self):
        clock_values = iter([0.0, 10.0, 10.0, 25.0, 25.0])
        tracker = Tracker(Path(self.dir.name) / "s2.json", clock=lambda: next(clock_values))
        daemon = PresenceDaemon(self.cfg, tracker, dry_run=True)
        daemon.step()
        daemon.step()
        daemon.step()
        self.assertGreater(tracker.session_file_seconds("Sketch.clip"), 0)

    def test_a_failing_poll_does_not_kill_the_loop(self):
        with mock.patch.object(presence_module.system, "observe", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.daemon.step()
        # run_forever swallows it; step itself is allowed to raise.
        self.daemon.stopped = True
        self.daemon.run_forever()


if __name__ == "__main__":
    unittest.main()
