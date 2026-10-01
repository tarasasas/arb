"""Warm start: the matched markets and the near-arb list, saved so a restart tracks arbs in seconds.

Matching every market on both sites takes a few minutes, but the matches barely change between
restarts. The scanner saves them here (cache/warm.pkl, git-ignored) and, on start, scans the saved
matches right away while fresh market lists load in the background. Only the matching is reused:
every price is fetched live before anything is shown, and a market that has closed since simply
comes back unpriced and drops out. A cache older than MAX_AGE_SECS, or from another version of the
code, is ignored.
"""

import pickle
import time

from . import config

PATH = config.PROJECT_ROOT / "cache" / "warm.pkl"
VERSION = 2
MAX_AGE_SECS = 12 * 3600


def save(data, path=PATH):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        pickle.dump({"version": VERSION, "time": time.time(), **data}, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)


def load(path=PATH, max_age=MAX_AGE_SECS):
    """The saved data, or None if there's none usable."""
    try:
        with open(path, "rb") as f:
            d = pickle.load(f)
    except Exception:              # missing, half-written, or saved by an older version of the code
        return None
    if not isinstance(d, dict) or d.get("version") != VERSION or time.time() - d.get("time", 0) > max_age:
        return None
    return d
