"""Local dashboard: serves static/index.html, the scanner state as JSON, and trade actions."""

import json
import secrets
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .trader import TradeError

STATIC = Path(__file__).parent / "static"


class ExclusiveServer(ThreadingHTTPServer):
    """Fail loudly if the port is taken. The stdlib default (SO_REUSEADDR) lets several
    servers bind one port on Windows, so the browser can silently reach the wrong one."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(scanner, port, open_browser=True):
    stop = threading.Event()
    # Trade requests must carry this token, which only the dashboard page receives. Other
    # websites open in the browser can't read the page, so they can't place orders here.
    token = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, ctype="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, default=str).encode())

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/api/state":
                return self._json(200, scanner.snapshot())
            if path == "/api/myarbs":
                return self._json(200, {"arbs": scanner.my_arbs.snapshot(scanner.kalshi, scanner.pm),
                                        **scanner.my_arbs.state()})
            if path == "/api/matching":
                q = {k: v[0] for k, v in parse_qs(query).items()}
                return self._json(200, scanner.matching_snapshot(q.get("q", ""), q.get("category", ""),
                                                                 int(q.get("offset", 0)), int(q.get("limit", 20))))
            if path == "/api/depth":
                q = {k: v[0] for k, v in parse_qs(query).items()}
                try:
                    return self._json(200, scanner.live_depth(q.get("exchange"), q.get("id", ""), q.get("side")))
                except Exception as e:
                    return self._json(502, {"error": repr(e)})
            if path in ("/", "/index.html"):
                html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__ARB_TOKEN__", token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            self.send_error(404)

        def do_POST(self):
            path = self.path.partition("?")[0]
            origin = self.headers.get("Origin", "")
            if self.headers.get("X-Arb-Token") != token or (origin and origin != f"http://localhost:{port}"
                                                              and origin != f"http://127.0.0.1:{port}"):
                return self._json(403, {"error": "Forbidden"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if path == "/api/trade/prepare":
                    plan = scanner.trader.prepare(body.get("legs") or [], body.get("max_invest") or None)
                    scanner.log(f"Trade plan: {plan['size']:g} pairs, ${plan['capital']:.2f}, "
                                f"expected +${plan['expected_profit']:.2f}")
                    return self._json(200, plan)
                if path == "/api/matching/decide":
                    rel = body.get("relation")
                    if rel not in ("same", "opposite", "reject", "remove", "reject_event"):
                        return self._json(400, {"error": "bad relation"})
                    scanner.decide(body.get("pm"), body.get("kalshi"), rel, body.get("extra"),
                                   body.get("pm_event"), body.get("kalshi_event"))
                    scanner.log(f"Match {rel}: {body.get('pm') or body.get('pm_event')} ↔ "
                                f"{body.get('kalshi') or body.get('kalshi_event')}")
                    return self._json(200, {"ok": True})
                if path == "/api/myarbs/save":
                    return self._json(200, scanner.my_arbs.save({**body, "edited": True}))
                if path == "/api/alerts/test":
                    return self._json(200, {"channels": scanner.alerter.test()})
                if path == "/api/myarbs/sync":
                    scanner.sync_positions()
                    return self._json(200, scanner.my_arbs.state())
                if path == "/api/myarbs/delete":
                    scanner.my_arbs.delete(body.get("id", ""))
                    return self._json(200, {"ok": True})
                if path == "/api/trade/execute":
                    result = scanner.trader.execute(body.get("plan_id", ""))
                    try:
                        if result.get("plan"):
                            scanner.my_arbs.add_from_trade(result["plan"], result, body.get("row"))
                    except Exception as e:
                        scanner.log(f"Couldn't add the trade to My arbs: {e!r}")
                    scanner.log(f"Trade {result['status']}: {result['hedged_pairs']:g} pairs hedged, "
                                f"net ${result['net']:.2f}" +
                                (f", {result['unhedged_shares']:g} UNHEDGED" if result["unhedged_shares"] else ""))
                    return self._json(200, result)
                return self._json(404, {"error": "Not found"})
            except (TradeError, ValueError, KeyError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:
                scanner.log(f"Trade error: {e!r}")
                return self._json(500, {"error": f"Unexpected error: {e!r}. Check both accounts."})

        def log_message(self, *args):
            pass

    try:
        httpd = ExclusiveServer(("127.0.0.1", port), Handler)
    except OSError:
        raise SystemExit(f"Port {port} is already in use (another program, or the scanner is already running). "
                         f"Try: python -m arb --port {port + 1}")
    threading.Thread(target=scanner.run_forever, args=(stop,), daemon=True).start()
    url = f"http://localhost:{port}"
    if getattr(scanner, "alerter", None):
        scanner.alerter.dashboard_url = url
    scanner.log(f"Dashboard at {url} (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
