#!/usr/bin/env python3
"""分點契約：空 schema、fixture、path A live ingest（mocked FinMind）。"""
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        indexes = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_broker_branch_date_net", indexes)
        self.assertIn("idx_broker_branch_stock_date", indexes)
        self.assertIn("idx_broker_branch_broker_date", indexes)
        self.assertIn("broker_branch_meta", names)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM broker_branch_daily").fetchone()[0], 0)
        con.close()


class TokenAndTitleTests(unittest.TestCase):
    def test_token_absent_is_blocker(self):
        status = broker_branch.ingest_status({})
        self.assertFalse(status["token_present"])
        self.assertFalse(status["live_ingest"])
        self.assertEqual(status["path"], "A")
        self.assertEqual(status["slice_decision"], "hot_n")
        self.assertIn("FINMIND_TOKEN", status["blocker"])
        self.assertIn("Path A", status["blocker"])
        self.assertEqual(status["not"], "t86_foreign")

    def test_token_present_enables_live_ingest(self):
        status = broker_branch.ingest_status({"FINMIND_TOKEN": "secret"})
        self.assertTrue(status["token_present"])
        self.assertTrue(status["live_ingest"])
        self.assertEqual(status["path"], "A")
        self.assertEqual(status["slice_decision"], "hot_n")
        self.assertIsNone(status["blocker"])
        self.assertEqual(status["hot_n"], 80)

    def test_hot_n_from_env(self):
        self.assertEqual(broker_branch.configured_hot_n({}), 80)
        self.assertEqual(broker_branch.configured_hot_n({"BROKER_BRANCH_HOT_N": "50"}), 50)
        self.assertEqual(broker_branch.configured_hot_n({"BROKER_BRANCH_HOT_N": "nope"}), 80)

    def test_readme_labels_hot_names_not_full_market(self):
        text = Path(__file__).resolve().parents[1].joinpath("README.md").read_text()
        self.assertIn("熱門股", text)
        self.assertRegex(text, r"不是.{0,6}全市場")

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
        self.assertIn("熱門前 N", stock["coverage_note"])
        self.assertNotIn("全市場分點", stock["coverage_note"])
        self.assertEqual(stock["data"][0][0], "1020")
        self.assertEqual(stock["data"][1][0], "9A00")
        self.assertEqual(stock["data"][2][0], "5850")
        self.assertEqual(stock["buy"][0][0], "1020")
        self.assertEqual(stock["buy"][0][2], 400000)
        self.assertEqual(stock["sell"][0][0], "5850")
        self.assertEqual(stock["sell"][0][2], -280000)

        missing = broker_branch.stock_branches(self.conn, "1101", "2026-09-03", env={})
        self.assertEqual(missing["coverage"], "single_stock")
        self.assertEqual(missing["data"], [])
        self.assertEqual(missing["buy"], [])
        self.assertEqual(missing["sell"], [])
        self.assertEqual(missing["stock_id"], "1101")

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
        self.assertIsNone(top["start"])
        self.assertEqual(top["trading_days"], 0)
        blank.close()

    def test_near_n_days_sums_without_seeding_default_path(self):
        broker_branch.load_fixture(self.conn, FIXTURE, dev=True)
        self.conn.execute(
            "INSERT OR REPLACE INTO broker_branch_daily "
            "VALUES ('2026-09-02','2330','1020',10000,0,10000)"
        )
        self.conn.commit()
        one = broker_branch.top_branches(self.conn, "2026-09-03", k=3, env={})
        self.assertEqual(one["days"], 1)
        self.assertEqual(one["start"], "2026-09-03")
        self.assertEqual(one["end"], "2026-09-03")
        self.assertEqual(one["buy"][0][0], "1020")
        self.assertEqual(one["buy"][0][2], 430000)
        multi = broker_branch.top_branches(
            self.conn, "2026-09-03", k=3, env={}, days=5,
        )
        self.assertEqual(multi["days"], 5)
        self.assertEqual(multi["start"], "2026-09-02")
        self.assertEqual(multi["end"], "2026-09-03")
        self.assertEqual(multi["trading_days"], 2)
        self.assertEqual(multi["buy"][0][0], "1020")
        self.assertEqual(multi["buy"][0][2], 440000)
        self.assertEqual(multi["title"], "熱門股分點動向")
        self.assertNotIn("全市場", multi["title"])


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
        self.assertEqual(r["data_mode"], "empty_awaiting_token")
        self.assertEqual(r["path"], "A")
        self.assertEqual(r["slice_decision"], "hot_n")
        self.assertFalse(r["live_ingest"])
        self.assertFalse(r["token_present"])
        self.assertEqual(r["buy"], [])
        self.assertEqual(r["sell"], [])
        self.assertNotIn("全市場", r["title"])
        json.dumps(r)

    def test_top_days_query_default_is_single_day(self):
        r = self.call("/api/broker_branch/top", days=5)
        self.assertEqual(r["data_mode"], "empty_awaiting_token")
        self.assertEqual(r["buy"], [])
        self.assertEqual(r["days"], 5)
        self.assertIsNone(r["start"])

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
        self.assertEqual(stock["buy"][0][0], "9A00")
        self.assertTrue(all(row[2] > 0 for row in stock["buy"]))
        self.assertTrue(all(row[2] < 0 for row in stock["sell"]))
        missing = self.call("/api/broker_branch/stock", id="1101", date="2026-09-03")
        self.assertEqual(missing["coverage"], "single_stock")
        self.assertEqual(missing["data"], [])
        self.assertEqual(missing["buy"], [])
        self.assertEqual(missing["sell"], [])
        fresh = self.call("/api/broker_branch/freshness")
        self.assertEqual(fresh["last_date"], "2026-09-03")
        self.assertEqual(fresh["expected_after_hour"], 21)
        self.assertFalse(fresh["live_ingest"])


class LiveIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "test.db"
        self.conn = sqlite3.connect(self.db)
        collector.init_db(self.conn)
        for sid, name, turnover in (
            ("2330", "台積電", 90_000_000_000),
            ("2317", "鴻海", 20_000_000_000),
            ("2454", "聯發科", 15_000_000_000),
            ("1101", "台泥", 1_000_000_000),
        ):
            self.conn.execute(
                "INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-09-03", sid, name, 1, 1, 1, 1, 1, turnover),
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ingest_refuses_without_token_and_does_not_call_finmind(self):
        fetcher = MagicMock(side_effect=AssertionError("must not call FinMind"))
        with self.assertRaises(broker_branch.FinMindError) as ctx:
            broker_branch.ingest_hot_n(
                self.conn, trade_date="2026-09-03", n=2, env={}, fetcher=fetcher,
            )
        self.assertIn("FINMIND_TOKEN", str(ctx.exception))
        fetcher.assert_not_called()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM broker_branch_daily").fetchone()[0],
            0,
        )

    def test_cli_ingest_without_token_is_exit_2(self):
        with patch.object(broker_branch, "token_present", return_value=False), \
             patch.object(broker_branch, "fetch_secid_agg") as fetch:
            code = broker_branch.main(["ingest"])
        self.assertEqual(code, 2)
        fetch.assert_not_called()

    def test_map_secid_agg_drops_prices(self):
        mapped = broker_branch.map_secid_agg_row({
            "date": "2026-09-03",
            "stock_id": "2330",
            "securities_trader_id": "1020",
            "securities_trader": "合庫",
            "buy_volume": 500000,
            "sell_volume": 100000,
            "buy_price": 900.5,
            "sell_price": 901.0,
        })
        self.assertEqual(mapped, ("2026-09-03", "2330", "1020", 500000, 100000, 400000))
        self.assertEqual(len(mapped), 6)

    def test_hot_n_ingest_writes_live_rows_not_fixture(self):
        secret = "unit-test-finmind-token"
        calls = []

        def fetcher(stock_id, start, end):
            calls.append((stock_id, start, end))
            return [
                {
                    "date": start,
                    "stock_id": stock_id,
                    "securities_trader_id": "1020",
                    "securities_trader": "合庫",
                    "buy_volume": 1000 if stock_id == "2330" else 100,
                    "sell_volume": 10,
                    "buy_price": 1,
                    "sell_price": 2,
                }
            ]

        result = broker_branch.ingest_hot_n(
            self.conn,
            trade_date="2026-09-03",
            n=2,
            env={"FINMIND_TOKEN": secret},
            fetcher=fetcher,
        )
        self.assertTrue(result["live_ingest"])
        self.assertTrue(result["production"])
        self.assertEqual(result["data_mode"], "live")
        self.assertEqual(result["title"], "熱門股分點動向")
        self.assertNotIn("全市場", result["title"])
        self.assertEqual(result["slice_trade_date"], "2026-09-03")
        self.assertEqual([c[0] for c in calls], ["2330", "2317"])
        self.assertNotIn("1101", [c[0] for c in calls])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM broker_branch_daily").fetchone()[0],
            2,
        )
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(broker_branch_daily)")}
        self.assertNotIn("buy_price", cols)
        top = broker_branch.top_branches(
            self.conn, "2026-09-03", k=3, env={"FINMIND_TOKEN": secret},
        )
        self.assertEqual(top["data_mode"], "live")
        self.assertTrue(top["live_ingest"])
        self.assertEqual(top["title"], "熱門股分點動向")
        self.assertEqual(top["coverage"], "hot_n")
        self.assertNotIn("fixture_warning", top)
        self.assertEqual(top["slice_trade_date"], "2026-09-03")
        self.assertEqual(top["buy"][0][0], "1020")

    def test_live_rows_are_not_labeled_fixture_without_cloud_token(self):
        broker_branch.ingest_hot_n(
            self.conn,
            trade_date="2026-09-03",
            n=1,
            env={"FINMIND_TOKEN": "secret"},
            fetcher=lambda *_a: [{
                "date": "2026-09-03",
                "stock_id": "2330",
                "securities_trader_id": "9A00",
                "securities_trader": "永豐金",
                "buy_volume": 50,
                "sell_volume": 10,
            }],
        )
        top = broker_branch.top_branches(self.conn, "2026-09-03", env={})
        self.assertEqual(top["data_mode"], "live")
        self.assertFalse(top["token_present"])
        self.assertFalse(top["live_ingest"])
        self.assertNotIn("fixture_warning", top)

    def test_fetch_puts_token_in_bearer_header_not_query(self):
        secret = "super-secret-finmind-token"
        captured = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "msg": "success",
                    "status": 200,
                    "data": [{
                        "date": "2026-09-03",
                        "stock_id": "2330",
                        "securities_trader_id": "1020",
                        "securities_trader": "合庫",
                        "buy_volume": 1,
                        "sell_volume": 0,
                    }],
                }

        session = MagicMock()
        session.get.return_value = FakeResp()

        rows = broker_branch.fetch_secid_agg(
            "2330", "2026-09-03", "2026-09-03", secret,
            session=session, sleep=lambda _s: None,
        )
        self.assertEqual(len(rows), 1)
        args, kwargs = session.get.call_args
        captured["url"] = args[0]
        captured["params"] = kwargs.get("params") or {}
        captured["headers"] = kwargs.get("headers") or {}
        blob = json.dumps({"url": captured["url"], "params": captured["params"]})
        self.assertNotIn(secret, blob)
        self.assertNotIn("token", captured["params"])
        self.assertEqual(captured["headers"]["Authorization"], f"Bearer {secret}")
        self.assertEqual(captured["url"], broker_branch.FINMIND_SECID_AGG_URL)

    def test_auth_error_does_not_include_token(self):
        secret = "super-secret-finmind-token"

        class FakeResp:
            status_code = 401

        session = MagicMock()
        session.get.return_value = FakeResp()
        with self.assertRaises(broker_branch.FinMindError) as ctx:
            broker_branch.fetch_secid_agg(
                "2330", "2026-09-03", "2026-09-03", secret,
                session=session, sleep=lambda _s: None,
            )
        self.assertNotIn(secret, str(ctx.exception))

    def test_empty_with_token_is_not_awaiting_token_mode(self):
        top = broker_branch.top_branches(
            self.conn, env={"FINMIND_TOKEN": "secret"},
        )
        self.assertEqual(top["data_mode"], "empty")
        self.assertTrue(top["live_ingest"])
        self.assertEqual(top["title"], "熱門股分點動向")
        self.assertIn("已接 token", top["coverage_note"])
        self.assertNotIn("全市場", top["title"])


class WorkflowAndDocsTests(unittest.TestCase):
    def test_workflow_is_weekday_21_taipei_and_has_no_token_literal(self):
        text = Path(__file__).resolve().parents[1].joinpath(
            ".github", "workflows", "update_broker_branch.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("0 13 * * 1-5", text)
        self.assertIn("熱門股分點動向", text)
        self.assertNotIn("全市場", text)
        self.assertIn("secrets.FINMIND_TOKEN", text)
        self.assertIn("notify.notify_job", text)
        self.assertIn("market.broker_branch ingest", text)
        self.assertNotRegex(text, r"FINMIND_TOKEN:\s+['\"]?[A-Za-z0-9_\-]{8,}")
        self.assertNotIn("load-fixture", text)

    def test_docs_and_readme_stay_hot_n_not_full_market(self):
        root = Path(__file__).resolve().parents[1]
        for rel in ("docs/broker_branch.md", "README.md", "TODO.md"):
            text = root.joinpath(rel).read_text(encoding="utf-8")
            self.assertIn("熱門股", text)
            self.assertRegex(text, r"不是.{0,12}全市場")

    def test_env_example_has_empty_token_placeholder(self):
        text = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text()
        self.assertIn("FINMIND_TOKEN=", text)
        self.assertNotRegex(text, r"FINMIND_TOKEN=\S+")
        self.assertIn("BROKER_BRANCH_HOT_N", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
