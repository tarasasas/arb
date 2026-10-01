"""Local dashboard: serves static/index.html, the scanner state as JSON, and trade actions."""

import hashlib
import hmac
import json
import secrets
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .autotrade import fast_trade, record_trade
from .trader import TradeError

STATIC = Path(__file__).parent / "static"
FILES = {"/apple-touch-icon.png": "image/png", "/manifest.json": "application/manifest+json"}
MIN_PASSWORD = 8


def lan_ip():
    """This computer's address on the local network (what the phone connects to)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))           # no packet is sent; it just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def session_value(password):
    """The phone's login cookie: derived from the password, so it survives restarts and stops
    working as soon as you change DASHBOARD_PASSWORD."""
    return hashlib.sha256(f"arb-dashboard-session:{password}".encode()).hexdigest()


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<title>Arb Scanner</title><style>
:root{--bg:#f4f4f2;--card:#fcfcfb;--text:#0b0b0b;--muted:#7a7974;--border:#e3e2de;--accent:#2f5fd0;--bad:#d03b3b}
@media (prefers-color-scheme: dark){:root{--bg:#121211;--card:#1a1a19;--text:#fff;--muted:#8f8e86;--border:#2e2e2c;--accent:#7ea2f2}}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.4 system-ui,-apple-system,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}
form{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:22px;width:100%;max-width:360px}
h1{font-size:20px;margin:0 0 4px}p{color:var(--muted);margin:0 0 16px;font-size:14px}
input,button{width:100%;box-sizing:border-box;font-size:16px;padding:12px;border-radius:10px;border:1px solid var(--border);
background:var(--bg);color:var(--text)}button{margin-top:10px;background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.err{color:var(--bad);font-size:14px;margin-top:10px}</style></head><body>
<form method="post" action="/login"><h1>Arb Scanner</h1><p>Enter the DASHBOARD_PASSWORD from your .env.</p>
<input type="password" name="password" autocomplete="current-password" autofocus required>
<button type="submit">Open dashboard</button>__ERR__</form></body></html>"""


class ExclusiveServer(ThreadingHTTPServer):
    """Fail loudly if the port is taken. The stdlib default (SO_REUSEADDR) lets several
    servers bind one port on Windows, so the browser can silently reach the wrong one."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        """A browser that closes its connection mid-reply (refresh, closed tab, phone going to sleep)
        isn't an error worth a traceback; anything else still prints one."""
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def serve(scanner, port, open_browser=True, phone=False, password=""):
    """phone=True also listens on your local network, for the dashboard on your phone. Devices
    other than this computer must log in with `password` (DASHBOARD_PASSWORD in .env)."""
    stop = threading.Event()
    if phone and len(password) < MIN_PASSWORD:
        raise SystemExit(f"Phone mode needs a password: add DASHBOARD_PASSWORD=<at least {MIN_PASSWORD} characters> "
                         f"to .env (anyone on your Wi-Fi could otherwise open the dashboard and place trades).")
    cookie = session_value(password) if phone else None
    failures = {}                                  # ip -> (count, first failure time)
    # Trade requests must carry this token, which only the dashboard page receives. Other
    # websites open in the browser can't read the page, so they can't place orders here.
    token = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, ctype="application/json", headers=()):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _local(self):
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _allowed(self):
            """This computer: only via localhost names (blocks DNS-rebinding pages). Anything else:
            phone mode with the login cookie."""
            host = self.headers.get("Host", "")
            if self._local() and host in (f"localhost:{port}", f"127.0.0.1:{port}"):
                return True
            if not phone:
                return False
            jar = {}
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                jar[k] = v
            return hmac.compare_digest(jar.get("arb_session", ""), cookie)

        def _login_page(self, err=""):
            body = LOGIN_PAGE.replace("__ERR__", f'<div class="err">{err}</div>' if err else "")
            return self._send(200, body.encode(), "text/html; charset=utf-8")

        def _login(self):
            ip = self.client_address[0]
            count, first = failures.get(ip, (0, time.time()))
            if time.time() - first > 300:
                count, first = 0, time.time()
            if count >= 10:
                return self._login_page("Too many wrong passwords. Wait 5 minutes.")
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            given = (form.get("password") or [""])[0]
            if phone and hmac.compare_digest(given.encode(), password.encode()):
                failures.pop(ip, None)
                scanner.log(f"Phone dashboard: logged in from {ip}")
                return self._send(303, b"", "text/plain", [
                    ("Location", "/"),
                    ("Set-Cookie", f"arb_session={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age={60 * 60 * 24 * 90}")])
            failures[ip] = (count + 1, first)
            time.sleep(1)
            return self._login_page("Wrong password.")

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, default=str).encode())

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path in FILES:                          # icon and app manifest: no secrets in them
                return self._send(200, (STATIC / path[1:]).read_bytes(), FILES[path])
            if not self._allowed():
                if phone and not path.startswith("/api/"):
                    return self._login_page()
                return self._json(403, {"error": "Forbidden"})
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
            if path == "/login":
                return self._login()
            origin = self.headers.get("Origin", "")
            if (not self._allowed() or not hmac.compare_digest(self.headers.get("X-Arb-Token", ""), token)
                    or (origin and origin != f"http://{self.headers.get('Host', '')}")):
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
                    record_trade(scanner, result, body.get("row"))
                    return self._json(200, result)
                if path == "/api/trade/fast":
                    return self._json(200, fast_trade(scanner, body.get("legs") or [], body.get("max_invest") or None))
                if path == "/api/focus":
                    return self._json(200, scanner.set_focus(body.get("days") or 0))
                if path == "/api/autotrade":
                    return self._json(200, scanner.autotrader.set(bool(body.get("on"))))
                return self._json(404, {"error": "Not found"})
            except (TradeError, ValueError, KeyError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:
                scanner.log(f"Trade error: {e!r}")
                return self._json(500, {"error": f"Unexpected error: {e!r}. Check both accounts."})

        def log_message(self, *args):
            pass

    try:
        httpd = ExclusiveServer(("0.0.0.0" if phone else "127.0.0.1", port), Handler)
    except OSError:
        raise SystemExit(f"Port {port} is already in use (another program, or the scanner is already running). "
                         f"Try: python -m arb --port {port + 1}")
    threading.Thread(target=scanner.run_forever, args=(stop,), daemon=True).start()
    url = f"http://localhost:{port}"
    if getattr(scanner, "alerter", None):
        scanner.alerter.dashboard_url = url
    scanner.log(f"Dashboard at {url} (Ctrl+C to stop)")
    if phone:
        scanner.log(f"Phone: open http://{lan_ip()}:{port} in Safari on the same Wi-Fi, log in with DASHBOARD_PASSWORD, "
                    f"then Share > Add to Home Screen. If it doesn't load, allow Python through Windows Firewall "
                    f"(Private networks).")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
