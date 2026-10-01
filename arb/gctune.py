"""Keep Python's own housekeeping from pausing the scanner at the wrong moment.

Two things stall every thread at once, including the stream re-check and a trade in flight:
  - The garbage collector's full passes walk every object the app holds (the matched markets are
    hundreds of thousands of objects): measured 155ms typical, 560ms worst, about every 8 seconds.
  - Only one thread runs Python at a time; by default a busy thread keeps it for up to 5ms.

So, as latency-sensitive Python services do: automatic full passes are made rare, after each big
reload the surviving objects are frozen (gc.freeze), which takes them out of every later pass, and a
full collection runs at most every GC_FULL_EVERY_SECS, never while a trade is in flight. Threads swap
every GIL_SWITCH_SECS.
"""

import gc
import sys
import threading
import time

from . import config

_lock = threading.Lock()
_last_full = time.monotonic()


def setup():
    sys.setswitchinterval(config.GIL_SWITCH_SECS)
    young, middle, _ = gc.get_threshold()
    # Automatic full passes only rarely (every GC_GEN2_THRESHOLD middle-generation passes instead of 10):
    # a market reload creates hundreds of thousands of objects, and each automatic full pass over them
    # paused everything for 100-340ms. settle() does the full pass on its own schedule instead.
    gc.set_threshold(young, middle, config.GC_GEN2_THRESHOLD)
    gc.freeze()                         # everything imported so far is permanent


def settle(busy=lambda: False):
    """Call after a big reload (market lists, matches). Freezes what's alive now; once every
    GC_FULL_EVERY_SECS, first collects the garbage from earlier reloads (one pause, while idle).
    Returns the pause in ms (0 if none)."""
    global _last_full
    with _lock:
        pause = 0.0
        if time.monotonic() - _last_full >= config.GC_FULL_EVERY_SECS and not busy():
            t = time.perf_counter()
            gc.unfreeze()
            gc.collect()
            pause = (time.perf_counter() - t) * 1000
            _last_full = time.monotonic()
        gc.freeze()
        return pause
