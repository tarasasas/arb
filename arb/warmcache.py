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
VERSION = 6            # bump whenever a saved class gains or loses a field (5: pay_cache, quoted_at; 6: chunks)
MAX_AGE_SECS = 12 * 3600
CHUNK = 250            # items per piece: each piece is one short uninterrupted step, not one 1.4s step


def _write(p, value):
    """Pickle `value` in pieces, letting other threads run between them. One Pickler keeps its memo
    across pieces, so objects shared between pieces are still saved once and restored as one."""
    if isinstance(value, list) and len(value) > CHUNK:
        p.dump(("L", (len(value) + CHUNK - 1) // CHUNK))
        for i in range(0, len(value), CHUNK):
            p.dump(value[i:i + CHUNK])
            time.sleep(0)
    elif isinstance(value, dict) and len(value) > CHUNK:
        items = list(value.items())
        p.dump(("D", (len(items) + CHUNK - 1) // CHUNK))
        for i in range(0, len(items), CHUNK):
            p.dump(items[i:i + CHUNK])
            time.sleep(0)
    elif isinstance(value, tuple) and any(isinstance(v, (list, dict)) and len(v) > CHUNK for v in value):
        p.dump(("T", len(value)))
        for v in value:
            _write(p, v)
    else:
        p.dump(("V", value))
        time.sleep(0)


def _read(u):
    kind, n = u.load()
    if kind == "L":
        return [x for _ in range(n) for x in u.load()]
    if kind == "D":
        return dict(kv for _ in range(n) for kv in u.load())
    if kind == "T":
        return tuple(_read(u) for _ in range(n))
    return n


def save(data, path=PATH):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        p = pickle.Pickler(f, protocol=pickle.HIGHEST_PROTOCOL)
        p.dump({"version": VERSION, "time": time.time(), "keys": list(data)})
        for k in data:
            _write(p, data[k])
    tmp.replace(path)


def load(path=PATH, max_age=MAX_AGE_SECS):
    """The saved data, or None if there's none usable."""
    try:
        with open(path, "rb") as f:
            u = pickle.Unpickler(f)
            head = u.load()
            if (not isinstance(head, dict) or head.get("version") != VERSION
                    or time.time() - head.get("time", 0) > max_age):
                return None
            d = {"version": head["version"], "time": head["time"]}
            for k in head.get("keys") or []:
                d[k] = _read(u)
    except Exception:              # missing, half-written, or saved by an older version of the code
        return None
    return d
