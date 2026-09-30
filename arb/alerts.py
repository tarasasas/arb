"""Push a message to your phone when a new arb worth taking appears (Discord and/or Telegram).

Set in .env (all optional):
  ALERT_MIN_PROFIT=5           alert when an arb's profit is at least this many dollars
  ALERT_COOLDOWN_MINS=30       don't repeat the same arb sooner, unless its profit grows by half
  DISCORD_WEBHOOK_URL=...      Discord: channel settings > Integrations > Webhooks > New webhook
  TELEGRAM_BOT_TOKEN=...       Telegram: create a bot with @BotFather
  TELEGRAM_CHAT_ID=...         your chat id (message the bot, then see getUpdates)
The dashboard also beeps and shows a browser notification on its own; this is for when it's closed.
"""

import json
import os
import threading
import time
import urllib.request

SKIP_WARNINGS = ("ONE-WAY RULES", "DIFFERENT SETTLEMENT SOURCES", "PRICES CONTRADICT")


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def worth_alerting(row, min_profit):
    """A real-looking arb above the threshold: not too good to be true, no rule traps."""
    if row.get("suspicious") or (row.get("profit") or 0) < min_profit:
        return False
    return not any(w.startswith(SKIP_WARNINGS) for w in row.get("warnings") or [])


def _cents(p):
    return f"{p * 100:.1f}¢"


def message(row, dashboard_url):
    legs = []
    for leg in row["legs"]:
        fills = (row.get("book") or {}).get("kalshi" if leg["exchange"] == "Kalshi" else "polymarket") or []
        limit = fills[-1][0] if fills else leg["price"]
        legs.append(f"{leg['exchange']}: Buy {leg['side'].upper()} {row.get('size', 0):,g} @ ≤ {_cents(limit)}")
    return (f"${row['profit']:,.2f} arb ({row.get('roi', 0) * 100:.2f}%) · {row.get('tab', '')} · {row['game']}\n"
            + "\n".join(legs) + f"\n{dashboard_url}")


class Alerter:
    def __init__(self, log, send=None):
        self.log = log
        self.min_profit = _env_float("ALERT_MIN_PROFIT", 5.0)
        self.cooldown = _env_float("ALERT_COOLDOWN_MINS", 30.0) * 60
        self.discord = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.dashboard_url = "http://localhost:8791"
        self.sent = {}                      # row key -> (time, profit)
        self._send = send or self._post_all
        self.lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.discord or (self.tg_token and self.tg_chat))

    def channels(self):
        return [name for name, on in (("Discord", self.discord), ("Telegram", self.tg_token and self.tg_chat)) if on]

    def check(self, rows):
        """Send alerts for new or much bigger arbs. Returns the rows alerted."""
        if not self.enabled:
            return []
        now, out = time.time(), []
        with self.lock:
            for r in rows:
                if not worth_alerting(r, self.min_profit):
                    continue
                key = tuple(f"{l['market_id']}:{l['side']}" for l in r["legs"])
                last = self.sent.get(key)
                if last and now - last[0] < self.cooldown and r["profit"] < last[1] * 1.5:
                    continue
                self.sent[key] = (now, r["profit"])
                out.append(r)
        for r in out:
            threading.Thread(target=self._safe_send, args=(message(r, self.dashboard_url),), daemon=True).start()
        return out

    def test(self):
        if not self.enabled:
            raise ValueError("No alert channel set: add DISCORD_WEBHOOK_URL or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to .env")
        self._send("Test alert from the arb scanner: alerts are working.")
        return self.channels()

    def _safe_send(self, text):
        try:
            self._send(text)
        except Exception as e:
            self.log(f"Alert failed: {e!r}")

    def _post_all(self, text):
        if self.discord:
            _post_json(self.discord, {"content": text})
        if self.tg_token and self.tg_chat:
            _post_json(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                       {"chat_id": self.tg_chat, "text": text, "disable_web_page_preview": True})


def _post_json(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "arb-scanner"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
