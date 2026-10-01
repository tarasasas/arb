"""Exchange-neutral contract model and payout math.

Every supported market is a binary contract whose YES side pays $1 when an integer game
quantity x satisfies a predicate:  x > line,  x < line,  or  x == line.

x is one of:
  ("margin", period, team)  -> team's score minus the opponent's score
  ("total", period)         -> combined score
  ("tt", period, team)      -> one team's score

Two positions on the same quantity form an arbitrage when, for every possible final x,
their combined payout is at least some amount P, and they cost less than P after fees.
Because payouts only change at the lines, checking integers around each line is exhaustive.
"""

import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal

YES = "yes"
NO = "no"
CENT = Decimal("0.01")


@dataclass
class Contract:
    exchange: str            # "kalshi" | "polymarket"
    market_id: str           # Kalshi ticker or Polymarket slug
    game_key: str            # shared key of the matched game
    var: tuple               # quantity, see module docstring
    op: str                  # ">", "<", "=="
    line: float
    title: str
    rules: str = ""
    tie_half: bool = False   # moneyline where a tied game pays $0.50 to each side (NFL)
    fee_coef: float = 0.0    # taker coefficient incl. any series multiplier
    close_time: str = ""
    no_draw: bool = False    # margin can't end at 0 (full game in sports decided by OT/extra innings)
    winner: bool = False     # "X wins" market: margin > 0 is a plain win/loss, never a push
    game_label: str = ""     # human-readable, e.g. "ATL vs NO, Oct 5"
    note: str = ""           # "auto" for non-sports pairs matched automatically (not yet verified)
    trade_until: str = ""    # ISO time trading stops (crypto windows); stale quotes after it are ignored
    # Top of book for buying each side: price per contract and size (None if unknown).
    ask: dict = field(default_factory=lambda: {YES: None, NO: None})
    ask_size: dict = field(default_factory=lambda: {YES: None, NO: None})
    # guaranteed_payout() against other contracts: it depends only on terms, never on price.
    pay_cache: dict = field(default_factory=dict, repr=False, compare=False)

    def __getstate__(self):
        d = dict(self.__dict__)
        d.pop("pay_cache", None)          # rebuilt on demand; saving it made the warm-start file slow
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self.__dict__.setdefault("pay_cache", {})

    @property
    def integer_line(self):
        """Spread/total on a whole number, where landing exactly on the line may be a push."""
        return (self.op in (">", "<") and float(self.line).is_integer()
                and not self.tie_half and not self.winner)

    def describe(self, side):
        return f"{side.upper()} · {self.title}"


def yes_payout(c, x):
    """Payout of one YES contract when the quantity ends at integer x.
    A push on an integer line (x == line for > or <) is treated as paying 0 on both sides,
    which is the conservative assumption since push rules differ between exchanges."""
    if c.tie_half and x == 0:
        return 0.5
    if c.op == ">":
        return 1.0 if x > c.line else 0.0
    if c.op == "<":
        return 1.0 if x < c.line else 0.0
    return 1.0 if x == c.line else 0.0


def payout(c, side, x):
    y = yes_payout(c, x)
    if c.integer_line and x == c.line:
        return 0.0
    return y if side == YES else 1.0 - y


def sample_points(contracts, nonnegative):
    pts = set()
    for c in contracts:
        lo, hi = math.floor(c.line), math.ceil(c.line)
        pts.update((lo - 1, lo, lo + 1, hi, hi + 1))
        if c.tie_half:
            pts.update((-1, 0, 1))
    if nonnegative:
        pts = {p for p in pts if p >= 0} | {0}
    return sorted(pts)


def guaranteed_payout(legs):
    """legs: [(Contract, side), ...] all on the same quantity. Returns the minimum total
    payout per contract-set across every possible outcome."""
    nonneg = legs[0][0].var[0] != "margin"
    pts = sample_points([c for c, _ in legs], nonneg)
    if not nonneg and any(c.no_draw for c, _ in legs):
        pts = [p for p in pts if p != 0]
    return min(sum(payout(c, s, x) for c, s in legs) for x in pts)


# ---- fees -----------------------------------------------------------------------------

def fee_per_contract(coef, price):
    """Unrounded taker fee for one contract at `price` (symmetric in p and 1-p)."""
    return coef * price * (1.0 - price)


def _exact_fee(fills, coef):
    """Exact decimal sum of coef x q x p x (1-p); floats like 6.255 must not become 6.2549999."""
    c = Decimal(str(coef))
    return sum((c * Decimal(str(q)) * Decimal(str(p)) * (1 - Decimal(str(p))) for p, q in fills), Decimal(0))


def kalshi_fee(fills, coef):
    """fills: [(price, qty)]. Kalshi rounds the order's balance change (cost + fee) up to the cent
    (docs.kalshi.com, Fee Rounding), so on sub-cent prices like 12.3c the rounding lands in the fee.
    Returned as the fee on top of the exact cost."""
    cost = sum((Decimal(str(p)) * Decimal(str(q)) for p, q in fills), Decimal(0))
    total = (cost + _exact_fee(fills, coef)).quantize(CENT, rounding=ROUND_CEILING)
    return float(total - cost)


def polymarket_fee(fills, coef):
    """Banker's rounding (half to even) of the cumulative fee to the cent."""
    return float(_exact_fee(fills, coef).quantize(CENT, rounding=ROUND_HALF_EVEN))


def total_fee(exchange, fills, coef):
    return kalshi_fee(fills, coef) if exchange == "kalshi" else polymarket_fee(fills, coef)


def as_list(x):
    return [] if x is None else list(x) if isinstance(x, (list, tuple)) else [x]


def best_match(kalshi, poly, kside, pside):
    """The matched pair (kalshi contract, polymarket contract, payout) these two positions form, or None.
    kalshi/poly: a contract or a list of them (a market can be matched in several pairs, each with its
    own contract); the pair must come from the same match (same game and question)."""
    best = None
    for kc in as_list(kalshi):
        for pc in as_list(poly):
            if (kc.game_key, kc.var) != (pc.game_key, pc.var):
                continue
            pay = guaranteed_payout([(kc, kside), (pc, pside)])
            if best is None or pay > best[2]:
                best = (kc, pc, pay)
    return best

