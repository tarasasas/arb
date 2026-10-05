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
                "FAST_ALLOW_PLAYER_PROPS", "AUTO_TRADE_MAX_MISSES", "AUTO_TRADE_LONG_DAYS", "AUTO_TRADE_LONG_MIN_ROI"]
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

    def test_long_dated_settings_are_what_fast_check_reads(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 10, 5, tzinfo=timezone.utc)
        row = {"warnings": [], "payout": 1.0, "edge_per_contract": 0.04, "decided": (now + timedelta(days=60)).isoformat()}
        with mock.patch.dict("os.environ", {}):
            settings.update({"AUTO_TRADE_LONG_DAYS": 90, "AUTO_TRADE_LONG_MIN_ROI": 4}, self.env)
            self.assertEqual(config.AUTO_TRADE_LONG_MIN_ROI, 0.04)
            self.assertTrue(engine.fast_check(row, now)["ok"])                  # 4.17%
            settings.update({"AUTO_TRADE_LONG_MIN_ROI": 5}, self.env)
            self.assertFalse(engine.fast_check(row, now)["ok"])
            settings.update({"AUTO_TRADE_LONG_MIN_ROI": 4, "AUTO_TRADE_LONG_DAYS": 0}, self.env)
            self.assertFalse(engine.fast_check(row, now)["ok"])                 # 0 days = off

    def test_every_setting_exists_in_config(self):
        for key in settings.SPEC:
            self.assertTrue(hasattr(config, key), key)


class EVProfileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = Path(self.dir.name) / ".env"
        self.saved = {k: getattr(config, k) for k in settings.SPEC if k.startswith("EV_BOT_")}

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(config, k, v)
        self.dir.cleanup()

    def test_defaults_are_normal(self):
        self.assertEqual(settings.ev_profile(), "normal")
        for k, v in settings.EV_PROFILES["normal"].items():           # the profile is the shipped defaults
            self.assertEqual(settings.SPEC[k][3], v, k)

    def test_aggressive_sets_its_bundle_and_shows_custom_once_edited(self):
        with mock.patch.dict("os.environ", {}):
            settings.update({"EV_BOT_PROFILE": "aggressive"}, self.env)
            self.assertEqual((config.EV_BOT_MIN_EDGE, config.EV_BOT_KELLY, config.EV_BOT_PER_GAME, config.EV_BOT_MAX_BET),
                             (0.01, 0.5, 3, 25.0))
            self.assertEqual(settings.ev_profile(), "aggressive")
            text = self.env.read_text(encoding="utf-8")
            self.assertIn("EV_BOT_MIN_EDGE=1\n", text)
            self.assertIn("EV_BOT_PER_GAME=3\n", text)
            settings.update({"EV_BOT_MAX_BET": 15}, self.env)
            self.assertEqual(settings.ev_profile(), "custom")
            settings.update({"EV_BOT_PROFILE": "normal"}, self.env)
            self.assertEqual((settings.ev_profile(), config.EV_BOT_MAX_BET), ("normal", 10.0))

    def test_a_value_set_alongside_the_profile_wins(self):
        with mock.patch.dict("os.environ", {}):
            settings.update({"EV_BOT_PROFILE": "aggressive", "EV_BOT_MAX_BET": 12}, self.env)
        self.assertEqual((config.EV_BOT_MAX_BET, config.EV_BOT_KELLY), (12.0, 0.5))

    def test_the_bankroll_is_never_part_of_a_profile(self):
        for values in settings.EV_PROFILES.values():
            self.assertNotIn("EV_BOT_BANKROLL", values)
            self.assertNotIn("EV_BOT_PAPER", values)


if __name__ == "__main__":
    unittest.main()
