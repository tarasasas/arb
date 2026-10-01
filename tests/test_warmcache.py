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

    def test_big_parts_are_saved_in_pieces_and_restored_whole(self):
        cs = [Contract("kalshi", f"K{i}", "G", ("total", "FG"), ">", 5.5, "t") for i in range(2500)]
        src = {("kalshi", f"K{i}"): {"i": i} for i in range(2500)}
        shared = {"names": {"a"}}
        cs[0].rules = cs[2400].rules = "same text"
        for c in cs:
            c.pay_cache[("yes", "p")] = 1.0
        warmcache.save({"sports_cat": (cs, src), "pairs_cat": ([], {}), "x": [shared, shared]}, self.path)
        d = warmcache.load(self.path)
        got, gsrc = d["sports_cat"]
        self.assertEqual([c.market_id for c in got], [c.market_id for c in cs])
        self.assertEqual(gsrc, src)
        self.assertEqual(got[1].pay_cache, {})                 # rebuilt on demand, not saved
        self.assertIs(d["x"][0], d["x"][1])                     # shared objects stay shared
        self.assertEqual(d["pairs_cat"], ([], {}))

    def test_missing_old_or_other_version_is_ignored(self):
        self.assertIsNone(warmcache.load(self.path))
        warmcache.save({"x": 1}, self.path)
        self.assertIsNone(warmcache.load(self.path, max_age=-1))
        with open(self.path, "wb") as f:
            pickle.dump({"version": warmcache.VERSION + 1, "time": time.time()}, f)
        self.assertIsNone(warmcache.load(self.path))
        self.path.write_bytes(b"not a pickle")
        self.assertIsNone(warmcache.load(self.path))
