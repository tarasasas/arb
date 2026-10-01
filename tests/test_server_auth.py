import http.client
import socket
import threading
import time
import unittest
import urllib.parse

from arb import server


class FakeScanner:
    alerter = None

    def run_forever(self, stop):
        stop.wait()

    def log(self, m):
        pass

    def snapshot(self):
        return {"ok": True}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class PhoneModeTests(unittest.TestCase):
    PASSWORD = "correct horse"

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        threading.Thread(target=server.serve, args=(FakeScanner(), cls.port),
                         kwargs={"open_browser": False, "phone": True, "password": cls.PASSWORD}, daemon=True).start()
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)

    def req(self, method, path, host=None, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"localhost:{self.port}", **(headers or {})}
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()

    def test_this_computer_needs_no_login(self):
        status, _, body = self.req("GET", "/api/state")
        self.assertEqual((status, body), (200, b'{"ok": true}'))

    def test_other_host_names_need_the_password(self):
        status, _, body = self.req("GET", "/", host=f"192.168.1.5:{self.port}")
        self.assertIn(b"DASHBOARD_PASSWORD", body)
        self.assertEqual(self.req("GET", "/api/state", host=f"192.168.1.5:{self.port}")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", host=f"evil.example:{self.port}")[0], 403)

    def test_login_wrong_then_right(self):
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        status, headers, body = self.req("POST", "/login", host=f"192.168.1.5:{self.port}",
                                         body=urllib.parse.urlencode({"password": "nope"}), headers=form)
        self.assertIn(b"Wrong password", body)
        self.assertNotIn("Set-Cookie", headers)
        status, headers, _ = self.req("POST", "/login", host=f"192.168.1.5:{self.port}",
                                      body=urllib.parse.urlencode({"password": self.PASSWORD}), headers=form)
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        status, _, body = self.req("GET", "/api/state", host=f"192.168.1.5:{self.port}", headers={"Cookie": cookie})
        self.assertEqual(status, 200)

    def test_posts_need_the_page_token_even_with_the_cookie(self):
        cookie = "arb_session=" + server.session_value(self.PASSWORD)
        status, _, _ = self.req("POST", "/api/trade/fast", host=f"192.168.1.5:{self.port}", body="{}",
                                headers={"Cookie": cookie, "Origin": f"http://192.168.1.5:{self.port}"})
        self.assertEqual(status, 403)

    def test_icon_is_public(self):
        status, headers, body = self.req("GET", "/apple-touch-icon.png", host=f"192.168.1.5:{self.port}")
        self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))


class PasswordRequiredTests(unittest.TestCase):
    def test_phone_mode_refuses_to_start_without_a_password(self):
        with self.assertRaises(SystemExit):
            server.serve(FakeScanner(), free_port(), open_browser=False, phone=True, password="short")


class QuietDisconnectTests(unittest.TestCase):
    def test_dropped_browser_connections_print_nothing(self):
        import io
        import sys
        from contextlib import redirect_stderr
        srv = server.ExclusiveServer.__new__(server.ExclusiveServer)
        err = io.StringIO()
        with redirect_stderr(err):
            for exc in (ConnectionAbortedError(10053, "aborted"), ConnectionResetError(), BrokenPipeError()):
                try:
                    raise exc
                except OSError:
                    srv.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(err.getvalue(), "")
        with redirect_stderr(err):
            try:
                raise ValueError("a real bug")
            except ValueError:
                srv.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("a real bug", err.getvalue())
