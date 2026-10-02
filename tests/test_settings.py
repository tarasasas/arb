import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arb import config, engine, settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = Path(self.dir.name) / ".env"
        self.env.write_text("# my keys\nKALSHI_API_KEY_ID=abc\nKALSHI_PRIVATE_KEY_PATH=kalshi.key\n"
                            "FAST_ALLOW_TOO_GOOD=1\n# FAST_ALLOW_AUTO_MATCHED=0\nAUTO_TRADE_MAX_TRADE=25\n", encoding="utf-8")
        keys = ["FAST_ALLOW_TOO_GOOD", "AUTO_TRADE_MAX_TRADE", "AUTO_TRADE_MIN_ROI", "TRADE_ORDER", "TRADE_LEGS_TOGETHER",
                "FAST_ALLOW_PLAYER_PROPS", "AUTO_TRADE_MAX_MISSES"]
        self.saved = {k: getattr(config, k) for k in keys}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        self.dir.cleanup()

    def test_changes_apply_now_and_land_in_env_without_touching_other_lines(self):
        with mock.patch.dict("os.environ", {}):
            settings.update({"FAST_ALLOW_TOO_GOOD": False, "AUTO_TRADE_MAX_TRADE": 40, "AUTO_TRADE_MIN_ROI": 1.5,
                             "TRADE_ORDER": "thinner_first"}, self.env)
        self.assertEqual((config.FAST_ALLOW_TOO_GOOD, config.AUTO_TRADE_MAX_TRADE, config.AUTO_TRADE_MIN_ROI,
                          config.TRADE_ORDER, config.TRADE_LEGS_TOGETHER), (False, 40.0, 0.015, "thinner_first", False))
        self.assertEqual(self.env.read_text(encoding="utf-8"),
                         "# my keys\nKALSHI_API_KEY_ID=abc\nKALSHI_PRIVATE_KEY_PATH=kalshi.key\nFAST_ALLOW_TOO_GOOD=0\n"
                         "# FAST_ALLOW_AUTO_MATCHED=0\nAUTO_TRADE_MAX_TRADE=40\n\n# Set from the dashboard (Settings)\n"
                         "AUTO_TRADE_MIN_ROI=1.5\nTRADE_ORDER=thinner_first\n")
        shown = {i["key"]: i["value"] for g in settings.current()["groups"] for i in g["items"]}
        self.assertEqual((shown["AUTO_TRADE_MIN_ROI"], shown["FAST_ALLOW_TOO_GOOD"]), (1.5, False))

    def test_a_bad_value_changes_nothing(self):
        before = self.env.read_text(encoding="utf-8")
        for bad in ({"AUTO_TRADE_MAX_TRADE": -5}, {"AUTO_TRADE_MAX_TRADE": "lots"}, {"TRADE_ORDER": "random"},
                    {"AUTO_TRADE_MAX_MISSES": 1.5}, {"KALSHI_API_KEY_ID": "x"},
                    {"FAST_ALLOW_PLAYER_PROPS": False, "AUTO_TRADE_MAX_TRADE": "x"}):
            with self.assertRaises(ValueError):
                settings.update(bad, self.env)
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)
        self.assertTrue(config.FAST_ALLOW_PLAYER_PROPS)

    def test_no_env_file_yet(self):
        self.env.unlink()
        with mock.patch.dict("os.environ", {}):
            settings.update({"FAST_ALLOW_PLAYER_PROPS": False}, self.env)
        self.assertEqual(self.env.read_text(encoding="utf-8"), "# Set from the dashboard (Settings)\nFAST_ALLOW_PLAYER_PROPS=0\n")

    def test_the_toggle_is_what_fast_check_reads(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        row = {"warnings": ["Player prop: if X doesn't play..."], "closes": "2026-10-03T12:00:00+00:00"}
        with mock.patch.dict("os.environ", {}):
            settings.update({"FAST_ALLOW_PLAYER_PROPS": False}, self.env)
            self.assertFalse(engine.fast_check(row, now)["ok"])
            settings.update({"FAST_ALLOW_PLAYER_PROPS": True}, self.env)
            self.assertTrue(engine.fast_check(row, now)["ok"])

    def test_every_setting_exists_in_config(self):
        for key in settings.SPEC:
            self.assertTrue(hasattr(config, key), key)


if __name__ == "__main__":
    unittest.main()
