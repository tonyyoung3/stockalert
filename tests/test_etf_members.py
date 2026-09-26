import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock

from market import etf_members as members
from web.tw_calendar import TW


def payload():
    return {"PCF": {"markcd": "0050", "trandate": "20260924"},
            "FundWeights": {"StockWeights": [
                {"code": str(1100+i), "name": f"Test {i}", "weights": 2}
                for i in range(50)],
                "FutureWeights": [{"code": "TX", "weights": 10}]}}


class MemberTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "members.json"
        self.now = datetime(2026, 9, 25, 8, tzinfo=TW)

    def test_complete_list_excludes_futures(self):
        got = members.parse_holdings(payload(), self.now.date())
        self.assertEqual(len(got["stocks"]), 50)
        self.assertNotIn("TX", [r["code"] for r in got["stocks"]])
        self.assertEqual(got["asof"], "2026-09-24")

    def test_reject_wrong_etf_partial_duplicate_future_and_stale(self):
        for case in ("wrong", "partial", "duplicate", "future", "stale"):
            p = payload()
            if case == "wrong": p["PCF"]["markcd"] = "0056"
            if case == "partial": p["FundWeights"]["StockWeights"].pop()
            if case == "duplicate": p["FundWeights"]["StockWeights"][1]["code"] = "1100"
            if case == "future": p["PCF"]["trandate"] = "20260928"
            if case == "stale": p["PCF"]["trandate"] = "20260901"
            with self.subTest(case=case), self.assertRaises(ValueError):
                members.parse_holdings(p, self.now.date())

    def test_failed_refresh_preserves_cache_and_age(self):
        fresh = members.parse_holdings(payload(), self.now.date())
        members.write_cache(self.path, fresh)
        fetch = Mock(side_effect=ValueError("partial list"))
        got = members.load_members(self.path, now=self.now, refresh=True, fetcher=fetch)
        self.assertEqual(got, fresh)
        self.assertEqual(json.loads(self.path.read_text()), fresh)
        with self.assertRaises(RuntimeError):
            members.load_members(self.path, now=datetime(2026, 10, 8, tzinfo=TW), fetcher=fetch)

    def test_success_cached_once_per_day(self):
        fetch = Mock(return_value=members.parse_holdings(payload(), self.now.date()))
        first = members.load_members(self.path, now=self.now, fetcher=fetch)
        second = members.load_members(self.path, now=self.now, fetcher=fetch)
        self.assertEqual(first, second)
        fetch.assert_called_once()

    def test_no_silent_static_fallback(self):
        with self.assertRaises(RuntimeError):
            members.load_members(self.path, now=self.now, fetcher=Mock(side_effect=ValueError()))

    def test_date_does_not_move_backward(self):
        fresh = members.parse_holdings(payload(), self.now.date())
        members.write_cache(self.path, fresh)
        older = {**fresh, "asof": "2026-09-23"}
        got = members.load_members(self.path, now=self.now, refresh=True,
                                   fetcher=Mock(return_value=older))
        self.assertEqual(got["asof"], "2026-09-24")
