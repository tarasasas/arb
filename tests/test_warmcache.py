import pickle
import tempfile
import time
import unittest
from pathlib import Path

from arb import warmcache
from arb.model import Contract


class WarmCacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "cache" / "warm.pkl"

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip_with_contracts(self):
        c = Contract("kalshi", "K1", "NBA:a", ("margin", "FG"), ">", 3.5, "t")
        warmcache.save({"sports_cat": ([c], {}), "hot_groups": {("NBA:a", c.var): {("kalshi", "K1")}}}, self.path)
        d = warmcache.load(self.path)
        self.assertEqual(d["sports_cat"][0][0].market_id, "K1")
        self.assertEqual(d["hot_groups"], {("NBA:a", c.var): {("kalshi", "K1")}})

    def test_missing_old_or_other_version_is_ignored(self):
        self.assertIsNone(warmcache.load(self.path))
        warmcache.save({"x": 1}, self.path)
        self.assertIsNone(warmcache.load(self.path, max_age=-1))
        with open(self.path, "wb") as f:
            pickle.dump({"version": warmcache.VERSION + 1, "time": time.time()}, f)
        self.assertIsNone(warmcache.load(self.path))
        self.path.write_bytes(b"not a pickle")
        self.assertIsNone(warmcache.load(self.path))
