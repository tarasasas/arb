import threading
import time
import unittest

from arb.http import LanePool, RateLimitedClient, is_priority, priority


class PriorityLaneTests(unittest.TestCase):
    def test_priority_requests_go_ahead_of_background_ones(self):
        c = RateLimitedClient("https://example.com", rps=50)      # one slot every 20 ms
        order, stop = [], time.monotonic() + 0.6

        def background():
            while time.monotonic() < stop:
                c._wait_turn()
                order.append(("bg", time.monotonic()))

        threads = [threading.Thread(target=background) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.1)                                          # background queue is busy now
        t0 = time.monotonic()
        with priority():
            for _ in range(5):
                c._wait_turn()
        took = time.monotonic() - t0
        for t in threads:
            t.join()
        # 5 priority slots, plus at most the slots background threads had already reserved (4).
        self.assertLess(took, 0.02 * (5 + 4) + 0.05)

    def test_pool_workers_keep_the_lane(self):
        with priority(), LanePool(2) as pool:
            self.assertEqual(list(pool.map(lambda _: is_priority(), range(4))), [True] * 4)
        with LanePool(2) as pool:
            self.assertEqual(list(pool.map(lambda _: is_priority(), range(4))), [False] * 4)


class FairShareTests(unittest.TestCase):
    def test_background_still_gets_slots_under_constant_priority_load(self):
        c = RateLimitedClient("https://example.com", rps=100)
        stop = time.monotonic() + 0.5
        counts = {"pri": 0, "bg": 0}

        def worker(lane):
            def run():
                while time.monotonic() < stop:
                    if lane == "pri":
                        with priority():
                            c._wait_turn()
                    else:
                        c._wait_turn()
                    counts[lane] += 1
            return run
        threads = [threading.Thread(target=worker("pri")) for _ in range(4)] + [threading.Thread(target=worker("bg"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        total = counts["pri"] + counts["bg"]
        self.assertGreater(counts["bg"], total * 0.15)       # ~1 in 4
        self.assertGreater(counts["pri"], total * 0.6)
