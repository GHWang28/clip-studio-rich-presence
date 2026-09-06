import json
import tempfile
import unittest
from pathlib import Path

from csprpc import config as config_module
from csprpc.config import DEFAULT_CLIENT_ID, ConfigError


class ClientIdTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.json"

    def tearDown(self):
        self.dir.cleanup()

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_ships_with_a_working_application_id(self):
        self.assertTrue(DEFAULT_CLIENT_ID.isdigit())
        self.assertEqual(config_module.DEFAULTS["client_id"], DEFAULT_CLIENT_ID)

    def test_defaults_need_no_setup(self):
        # Nothing about a stock config should require the user's attention.
        self.assertEqual(config_module.validate(config_module.load(self.path)), [])

    def test_missing_config_uses_the_default(self):
        self.assertEqual(config_module.load(self.path)["client_id"], DEFAULT_CLIENT_ID)

    def test_old_config_with_a_blank_id_is_migrated(self):
        # Written by a version that shipped before the application ID existed.
        self.write({"client_id": "", "poll_interval_seconds": 3.0})
        loaded = config_module.load(self.path)
        self.assertEqual(loaded["client_id"], DEFAULT_CLIENT_ID)
        self.assertEqual(loaded["poll_interval_seconds"], 3.0)  # rest survives

    def test_whitespace_only_id_is_migrated(self):
        self.write({"client_id": "   "})
        self.assertEqual(config_module.load(self.path)["client_id"], DEFAULT_CLIENT_ID)

    def test_a_custom_application_id_is_respected(self):
        self.write({"client_id": "999888777666555444"})
        self.assertEqual(config_module.load(self.path)["client_id"], "999888777666555444")

    def test_setting_a_custom_id(self):
        cfg = config_module.load(self.path)
        config_module.set_dotted(cfg, "client_id", "999888777666555444")
        self.assertEqual(cfg["client_id"], "999888777666555444")

    def test_blanking_the_id_restores_the_default(self):
        cfg = config_module.load(self.path)
        config_module.set_dotted(cfg, "client_id", "   ")
        self.assertEqual(cfg["client_id"], DEFAULT_CLIENT_ID)

    def test_a_non_numeric_id_is_flagged(self):
        cfg = config_module.load(self.path)
        cfg["client_id"] = "not-an-id"
        problems = config_module.validate(cfg)
        self.assertTrue(any("numeric Application ID" in p for p in problems))

    def test_an_empty_id_is_flagged_with_a_recovery_hint(self):
        cfg = config_module.load(self.path)
        cfg["client_id"] = ""
        problems = config_module.validate(cfg)
        self.assertTrue(any(DEFAULT_CLIENT_ID in p for p in problems))


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_written_config_reloads_identically(self):
        cfg = config_module.load(self.path)
        config_module.save(cfg, self.path)
        self.assertEqual(config_module.load(self.path), cfg)

    def test_ensure_exists_creates_then_leaves_alone(self):
        import os

        os.environ["CSPRPC_HOME"] = str(self.path.parent)
        try:
            created_path, created = config_module.ensure_exists()
            self.assertTrue(created)
            self.assertEqual(
                json.loads(created_path.read_text())["client_id"], DEFAULT_CLIENT_ID
            )
            _, created_again = config_module.ensure_exists()
            self.assertFalse(created_again)
        finally:
            os.environ.pop("CSPRPC_HOME", None)

    def test_unknown_setting_is_rejected(self):
        cfg = config_module.load(self.path)
        with self.assertRaises(ConfigError):
            config_module.set_dotted(cfg, "client_idd", "1")


if __name__ == "__main__":
    unittest.main()
