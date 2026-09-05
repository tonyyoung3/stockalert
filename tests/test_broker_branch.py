#!/usr/bin/env python3
"""分點契約：空 schema、fixture ingest、排行 SQL。不打 FinMind。"""
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from market import broker_branch, collector
from data import market_db
from web import dashboard, freshness as freshness_mod

TW = freshness_mod.TW
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "broker_branch_sample.json"


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "test.db"
        self._orig = collector.DB_PATH
        collector.DB_PATH = self.db

    def tearDown(self):
        collector.DB_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_db_creates_empty_branch_tables_and_indexes(self):
        collector.get_conn().close()
        con = sqlite3.connect(self.db)
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("broker_branch_daily", names)
        self.assertIn("brokers", names)
        cols = {r[1] for r in con.execute("PRAGMA table_info(broker_branch_daily)")}
        self.assertEqual(
            cols,
            {"trade_date", "stock_id", "broker_id",
             "buy_volume", "sell_volume", "net_volume"},
        )
        self.assertNotIn("buy_price", cols)
        self.assertNotIn("sell_price", cols)
        self.assertNotIn("price", cols)
        info = list(con.execute("PRAGMA table_info(broker_branch_daily)"))
        self.assertEqual(
            {r[1] for r in info if r[5]},
            {"trade_date", "stock_id", "broker_id"},
        )
        indexes = {r[1] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_broker_branch_date_net", indexes)
        self.assertIn("idx_broker_branch_stock_date", indexes)
        self.assertIn("idx_broker_branch_broker_date", indexes)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM broker_branch_daily").fetchone()[0], 0)
        con.close()


class TokenAndTitleTests(unittest.TestCase):
    def test_token_absent_is_blocker(self):
        status = broker_branch.ingest_status({})
        self.assertFalse(status["token_present"])
        self.assertFalse(status["live_ingest"])
        self.assertEqual(status["slice_decision"], "pending_owner")
        self.assertIn("FINMIND_TOKEN", status["blocker"])
        self.assertEqual(status["not"], "t86_foreign")

    def test_token_present_still_no_live_ingest_in_this_pr(self):
        status = broker_branch.ingest_status({"FINMIND_TOKEN": "secret"})
        self.assertTrue(status["token_present"])
        self.assertFalse(status["live_ingest"])
        self.assertIsNone(status["blocker"])

    def test_title_never_says_full_market_for_hot_n_or_empty(self):
        for coverage in ("empty", "hot_n", "single_stock", "not_applicable"):
            title = broker_branch.market_title(coverage)
            self.assertNotIn("全市場", title)
        self.assertEqual(broker_branch.market_title("hot_n"), "熱門股分點動向")
        self.assertEqual(broker_branch.market_title("full_market"), "全市場分點買賣超")

    def test_cli_status_and_fixture_refuses_without_dev(self):
        code = broker_branch.main(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(broker_branch.main(["load-fixture"]), 2)

    def test_env_example_has_empty_token_placeholder(self):
        text = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text()
        self.assertIn("FINMIND_TOKEN=", text)
        self.assertNotRegex(text, r"FINMIND_TOKEN=\S+")


class FixtureAndRankingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "test.db"
        self.conn = sqlite3.connect(self.db)
        collector.init_db(self.conn)
        self.conn.execute(
            "INSERT INTO stocks VALUES ('2330','台積電','2026-09-03')"
        )
        self.conn.execute(
            "INSERT INTO stocks VALUES ('2317','鴻海','2026-09-03')"
        )
        self.conn.execute(
            "INSERT INTO stocks VALUES ('2454','聯發科','2026-09-03')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fixture_refuses_without_dev_flag(self):
        with self.assertRaises(RuntimeError):
            broker_branch.load_fixture(self.conn, FIXTURE, dev=False)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM broker_branch_daily").fetchone()[0],
            0,
        )

    def test_fixture_upserts_and_rankings(self):
        result = broker_branch.load_fixture(self.conn, FIXTURE, dev=True)
        self.assertEqual(result["rows"], 9)
        self.assertFalse(result["live_ingest"])
        self.assertFalse(result["production"])
        self.assertEqual(result["data_mode"], "dev_fixture")

        top = broker_branch.top_branches(self.conn, "2026-09-03", k=3, env={})
        self.assertEqual(top["title"], "熱門股分點動向")
        self.assertNotIn("全市場", top["title"])
        self.assertEqual(top["coverage"], "hot_n")
        self.assertEqual(top["data_mode"], "dev_fixture")
        self.assertFalse(top["live_ingest"])
        self.assertFalse(top["token_present"])
        self.assertEqual(top["universe_count"], 3)
        self.assertEqual(top["buy"][0][0], "1020")
        self.assertEqual(top["buy"][0][2], 430000)
        self.assertEqual(top["sell"][0][0], "5850")
        self.assertEqual(top["sell"][0][2], -350000)
        json.dumps(top)

        drill = broker_branch.broker_stocks(self.conn, "1020", "2026-09-03", env={})
        ids = [r[0] for r in drill["data"]]
        self.assertEqual(ids[0], "2330")
        self.assertEqual(drill["data"][0][1], "台積電")
        self.assertEqual(drill["broker_name"], "合庫")

        stock = broker_branch.stock_branches(self.conn, "2330", "2026-09-03", env={})
        self.assertEqual(stock["coverage"], "single_stock")
        self.assertEqual(stock["title"], "個股分點買賣超")
        self.assertEqual(stock["data"][0][0], "1020")
        self.assertEqual(stock["data"][1][0], "9A00")
        self.assertEqual(stock["data"][2][0], "5850")

    def test_idempotent_fixture_load(self):
        broker_branch.load_fixture(self.conn, FIXTURE, dev=True)
        broker_branch.load_fixture(self.conn, FIXTURE, dev=True)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM broker_branch_daily").fetchone()[0],
            9,
        )

    def test_hot_n_from_stock_daily_turnover(self):
        for sid, name, turnover in (
            ("2330", "台積電", 90_000_000_000),
            ("2317", "鴻海", 20_000_000_000),
            ("1101", "台泥", 1_000_000_000),
            ("2454", "聯發科", 15_000_000_000),
        ):
            self.conn.execute(
                "INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-09-03", sid, name, 1, 1, 1, 1, 1, turnover),
            )
        self.conn.commit()
        picked = broker_branch.select_hot_n(self.conn, n=3)
        self.assertEqual([p[0] for p in picked], ["2330", "2317", "2454"])
        self.assertEqual(picked[0][2], 90_000_000_000)

    def test_empty_helpers_when_tables_missing(self):
        blank = sqlite3.connect(":memory:")
        top = broker_branch.top_branches(blank, env={})
        self.assertEqual(top["coverage"], "empty")
        self.assertEqual(top["buy"], [])
        self.assertEqual(top["title"], "熱門股分點動向")
        self.assertTrue(top["freshness"]["empty"])
        blank.close()


class DashboardStubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.market = Path(self.tmp.name) / "twse.db"
        sqlite3.connect(self.market).close()
        market_db.set_db_path(self.market)

    def tearDown(self):
        market_db.set_db_path(None)
        self.tmp.cleanup()

    def call(self, path, **qs):
        return dashboard.api(path, {k: [str(v)] for k, v in qs.items()})

    def test_empty_api_is_honest_not_full_market(self):
        r = self.call("/api/broker_branch/top")
        self.assertEqual(r["kind"], "broker_branch")
        self.assertEqual(r["not"], "t86_foreign")
        self.assertEqual(r["data_mode"], "empty_awaiting_owner_decision")
        self.assertFalse(r["live_ingest"])
        self.assertFalse(r["token_present"])
        self.assertEqual(r["buy"], [])
        self.assertNotIn("全市場", r["title"])
        json.dumps(r)

    def test_freshness_key_tables_unchanged(self):
        with patch("web.freshness.taiwan_now",
                   return_value=datetime(2026, 9, 4, 18, 0, tzinfo=TW)):
            r = self.call("/api/freshness")
        self.assertEqual(
            [t["table"] for t in r["tables"]],
            ["foreign_daily", "stock_daily", "taifex", "alerts"],
        )
        self.assertNotIn(
            "broker_branch_daily",
            [t["table"] for t in r["tables"]],
        )

    def test_fixture_via_dashboard_api(self):
        conn = sqlite3.connect(self.market)
        collector.init_db(conn)
        broker_branch.load_fixture(conn, FIXTURE, dev=True)
        conn.close()
        top = self.call("/api/broker_branch/top", date="2026-09-03", k=2)
        self.assertEqual(top["buy"][0][0], "1020")
        self.assertEqual(top["coverage"], "hot_n")
        drill = self.call("/api/broker_branch/broker", broker_id="5850", date="2026-09-03")
        self.assertTrue(any(row[0] == "2330" for row in drill["data"]))
        stock = self.call("/api/broker_branch/stock", id="2454", date="2026-09-03")
        self.assertEqual(stock["data"][0][0], "9A00")
        fresh = self.call("/api/broker_branch/freshness")
        self.assertEqual(fresh["last_date"], "2026-09-03")
        self.assertEqual(fresh["expected_after_hour"], 21)
        self.assertFalse(fresh["live_ingest"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
