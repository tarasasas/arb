import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from arb import upgrade
from arb.http import ApiError


class Account:
    """Kalshi's view of the account: an upgrade adds a grant and a bigger read budget,
    while usage_tier keeps saying basic (as Kalshi documents)."""

    def __init__(self, refuse=False, unreachable=()):
        self.refuse, self.unreachable, self.upgraded, self.posts = refuse, set(unreachable), False, []

    def limits(self):
        return {"usage_tier": "basic", "read": {"refill_rate": 300 if self.upgraded else 200, "bucket_capacity": 200},
                "write": {"refill_rate": 100}, "grants": [{"level": "advanced", "exchange_instance": "event_contract",
                                                          "source": "volume"}] if self.upgraded else []}


def run(account):
    reads = mock.Mock(signer=object())
    reads.get.side_effect = lambda path, params=None: account.limits()

    class Http:
        def __init__(self, host, rps, signer=None):
            self.host = host

        def post(self, path, body):
            account.posts.append((self.host.split("/")[2], path, body))
            if self.host.split("/")[2] in account.unreachable:
                raise OSError("no route")
            if account.refuse:
                raise ApiError(403, "No API-created order was found")
            account.upgraded = True
            return {}
    out = io.StringIO()
    with mock.patch.object(upgrade, "KalshiClient", return_value=mock.Mock(http=reads)), \
            mock.patch.object(upgrade, "RateLimitedClient", Http), redirect_stdout(out):
        code = upgrade.main()
    return code, out.getvalue()


class UpgradeTests(unittest.TestCase):
    def test_sent_like_kalshis_example_and_judged_by_budget_not_the_label(self):
        acct = Account()
        code, out = run(acct)
        self.assertEqual(code, 0)
        self.assertEqual(acct.posts, [("external-api.kalshi.com", "/account/api_usage_level/upgrade", None)])  # no body
        self.assertIn("read budget: 300", out)
        self.assertIn("grant: advanced", out)
        self.assertIn("Upgraded.", out)
        self.assertIn("usage_tier:  basic", out)          # the label Kalshi keeps either way

    def test_falls_back_to_the_scanners_host(self):
        acct = Account(unreachable={"external-api.kalshi.com"})
        code, out = run(acct)
        self.assertEqual(code, 0)
        self.assertEqual([p[0] for p in acct.posts], ["external-api.kalshi.com", "api.elections.kalshi.com"])

    def test_refused_explains_what_to_do(self):
        acct = Account(refuse=True)
        code, out = run(acct)
        self.assertEqual(code, 1)
        self.assertEqual(len(acct.posts), 1)               # a real "no" isn't retried elsewhere
        self.assertIn("Make trade", out)

    def test_already_upgraded_does_nothing(self):
        acct = Account()
        acct.upgraded = True
        code, out = run(acct)
        self.assertEqual((code, acct.posts), (0, []))
        self.assertIn("Already upgraded", out)

    def test_blocked_host_page_is_not_a_refusal(self):
        acct = Account()
        reads = mock.Mock(signer=object())
        reads.get.side_effect = lambda path, params=None: acct.limits()

        class Http:
            def __init__(self, host, rps, signer=None):
                self.host = host

            def post(self, path, body):
                acct.posts.append(self.host.split("/")[2])
                if "external-api" in self.host:      # the front door's own HTML page, not Kalshi's API
                    raise ApiError(403, "<html><head><title>403 Forbidden</title></head></html>")
                acct.upgraded = True
                return {}
        out = io.StringIO()
        with mock.patch.object(upgrade, "KalshiClient", return_value=mock.Mock(http=reads)), \
                mock.patch.object(upgrade, "RateLimitedClient", Http), redirect_stdout(out):
            code = upgrade.main()
        self.assertEqual((code, acct.posts), (0, ["external-api.kalshi.com", "api.elections.kalshi.com"]))
        self.assertIn("Upgraded.", out.getvalue())

    def test_needs_a_key(self):
        reads = mock.Mock(signer=None)
        out = io.StringIO()
        with mock.patch.object(upgrade, "KalshiClient", return_value=mock.Mock(http=reads)), redirect_stdout(out):
            self.assertEqual(upgrade.main(), 1)
