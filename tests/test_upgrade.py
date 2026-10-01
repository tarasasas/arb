import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from arb import upgrade
from arb.http import ApiError


class FakeHTTP:
    def __init__(self, refuse=False):
        self.signer, self.refuse, self.tier = object(), refuse, "basic"
        self.posts = []

    def get(self, path, params=None):
        return {"usage_tier": self.tier, "read": {"refill_rate": 200 if self.tier == "basic" else 300}}

    def post(self, path, body):
        self.posts.append(path)
        if self.refuse:
            raise ApiError(403, "no API-created order")
        self.tier = "advanced"
        return {}


class UpgradeTests(unittest.TestCase):
    def run_with(self, http):
        client = mock.Mock(http=http)
        out = io.StringIO()
        with mock.patch.object(upgrade, "KalshiClient", return_value=client), redirect_stdout(out):
            code = upgrade.main()
        return code, out.getvalue()

    def test_upgrades_and_reports_both_tiers(self):
        http = FakeHTTP()
        code, out = self.run_with(http)
        self.assertEqual(code, 0)
        self.assertEqual(http.posts, ["/account/api_usage_level/upgrade"])
        self.assertIn("basic tier, read budget 200", out)
        self.assertIn("advanced tier, read budget 300", out)

    def test_refused_explains_what_to_do(self):
        code, out = self.run_with(FakeHTTP(refuse=True))
        self.assertEqual(code, 1)
        self.assertIn("Make trade", out)

    def test_needs_a_key(self):
        http = FakeHTTP()
        http.signer = None
        code, out = self.run_with(http)
        self.assertEqual((code, http.posts), (1, []))
