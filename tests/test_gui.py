"""Tests for the window's logic, without needing a display.

The widget code needs a real screen, so CI cannot exercise it. Everything
that makes a decision is therefore kept out of the widgets, in module-level
functions, and that is what is tested here.
"""

import copy
import unittest

from csprpc import config as config_module
from csprpc import gui
from csprpc.config import ConfigError


class ImportSafetyTest(unittest.TestCase):
    def test_importing_does_not_need_tk(self):
        # tkinter is imported inside the window, so `csprpc.gui` must import
        # even on a Python built without Tk.
        self.assertTrue(hasattr(gui, "launch"))
        self.assertTrue(hasattr(gui, "PresenceWindow"))


class FieldSpecTest(unittest.TestCase):
    def test_every_field_is_a_real_setting(self):
        # A typo here would silently do nothing until someone pressed Save.
        cfg = copy.deepcopy(config_module.DEFAULTS)
        for field in gui.all_fields():
            with self.subTest(field.key):
                config_module.set_dotted(cfg, field.key, gui.get_dotted(cfg, field.key))

    def test_no_field_is_declared_twice(self):
        keys = [field.key for field in gui.all_fields()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_wording_of_all_three_states_is_editable(self):
        keys = {field.key for field in gui.all_fields()}
        for state in ("working", "idle", "no_document"):
            for part in ("details", "state"):
                self.assertIn("presence.templates.{}.{}".format(state, part), keys)

    def test_choice_fields_offer_only_valid_values(self):
        for field in gui.all_fields():
            if field.kind == "choice":
                with self.subTest(field.key):
                    self.assertTrue(field.choices)
                    cfg = copy.deepcopy(config_module.DEFAULTS)
                    for choice in field.choices:
                        config_module.set_dotted(cfg, field.key, choice)
                        self.assertEqual(config_module.validate(cfg), [])


class GetDottedTest(unittest.TestCase):
    def test_reads_a_nested_value(self):
        cfg = {"presence": {"templates": {"working": {"details": "hi"}}}}
        self.assertEqual(gui.get_dotted(cfg, "presence.templates.working.details"), "hi")

    def test_missing_key_reads_as_empty(self):
        self.assertEqual(gui.get_dotted({}, "presence.large_text"), "")

    def test_a_non_dict_on_the_way_down_reads_as_empty(self):
        self.assertEqual(gui.get_dotted({"presence": 3}, "presence.large_text"), "")

    def test_none_reads_as_empty_rather_than_none(self):
        # Feeding None into a Tk StringVar would render as "None".
        self.assertEqual(gui.get_dotted({"a": None}, "a"), "")


class ApplyValuesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(config_module.DEFAULTS)

    def test_saving_the_wording(self):
        pending, problems = gui.apply_values(
            self.cfg, {"presence.templates.working.details": "Painting {doc}"})
        self.assertEqual(problems, [])
        self.assertEqual(
            pending["presence"]["templates"]["working"]["details"], "Painting {doc}")

    def test_strings_from_widgets_are_coerced(self):
        # Tk hands back strings even for numbers.
        pending, problems = gui.apply_values(self.cfg, {"poll_interval_seconds": "2.5"})
        self.assertEqual(problems, [])
        self.assertEqual(pending["poll_interval_seconds"], 2.5)

    def test_booleans_survive(self):
        pending, _ = gui.apply_values(self.cfg, {"require_frontmost": True})
        self.assertIs(pending["require_frontmost"], True)

    def test_the_original_config_is_not_touched_until_saved(self):
        before = copy.deepcopy(self.cfg)
        gui.apply_values(self.cfg, {"presence.large_text": "Something else"})
        self.assertEqual(self.cfg, before)

    def test_a_bad_number_is_rejected_loudly(self):
        with self.assertRaises(ConfigError):
            gui.apply_values(self.cfg, {"poll_interval_seconds": "not a number"})

    def test_a_value_that_would_break_the_run_loop_raises(self):
        # validate() draws this line itself; Save turns it into a dialog.
        with self.assertRaises(ConfigError):
            gui.apply_values(self.cfg, {"poll_interval_seconds": "0"})

    def test_an_invalid_choice_raises(self):
        with self.assertRaises(ConfigError):
            gui.apply_values(self.cfg, {"presence.elapsed": "yesterday"})

    def test_a_merely_unwise_value_comes_back_as_advice(self):
        # Accepted, but worth telling the user about.
        _, problems = gui.apply_values(self.cfg, {"poll_interval_seconds": "0.5"})
        self.assertTrue(problems)

    def test_a_whole_form_round_trips(self):
        values = {field.key: gui.get_dotted(self.cfg, field.key)
                  for field in gui.all_fields()}
        pending, problems = gui.apply_values(self.cfg, values)
        self.assertEqual(problems, [])
        self.assertEqual(pending, self.cfg)


class StatusTextTest(unittest.TestCase):
    def test_every_snapshot_kind_has_wording(self):
        from csprpc.presence import evaluate
        from csprpc.model import Observation

        # The kinds evaluate() can produce must all be covered, or the header
        # would show a bare internal name.
        for kind in ("working", "idle", "no_document", "not_running"):
            self.assertIn(kind, gui.STATUS_TEXT)
        self.assertEqual(
            evaluate(config_module.DEFAULTS, Observation(running=False)).kind,
            "not_running",
        )

    def test_placeholders_advertised_are_the_ones_that_work(self):
        import tempfile
        from pathlib import Path

        from csprpc.model import Observation
        from csprpc.presence import evaluate, template_values
        from csprpc.tracker import Tracker

        with tempfile.TemporaryDirectory() as directory:
            tracker = Tracker(Path(directory) / "stats.json")
            snapshot = evaluate(config_module.DEFAULTS, Observation(running=False))
            values = template_values(config_module.DEFAULTS, tracker, snapshot)

        for placeholder in gui.PLACEHOLDERS:
            with self.subTest(placeholder):
                name = placeholder.strip("{}")
                self.assertIn(name, values)

    def test_every_placeholder_has_an_explanation(self):
        self.assertEqual(gui.PLACEHOLDERS, tuple(token for token, _, _ in gui.PLACEHOLDER_HELP))
        for token, meaning, example in gui.PLACEHOLDER_HELP:
            with self.subTest(token):
                self.assertTrue(token.startswith("{") and token.endswith("}"))
                self.assertTrue(meaning)
                self.assertTrue(example)

    def test_both_themes_define_the_same_swatches(self):
        self.assertEqual(set(gui.THEMES["light"]), set(gui.THEMES["dark"]))
        for name, palette in gui.THEMES.items():
            with self.subTest(name):
                for key in ("bg", "fg", "muted", "input_bg", "output_bg"):
                    self.assertTrue(palette[key].startswith("#"))


if __name__ == "__main__":
    unittest.main()
