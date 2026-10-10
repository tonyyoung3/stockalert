#!/usr/bin/env python3
"""分點契約：空 schema、fixture、唯讀歷史資料查詢。"""
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
        indexes = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_broker_branch_date_net", indexes)
        self.assertIn("idx_broker_branch_stock_date", indexes)
        self.assertIn("idx_broker_branch_broker_date", indexes)
        self.assertIn("broker_branch_meta", names)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM broker_branch_daily").fetchone()[0], 0)
        con.close()


class StatusAndTitleTests(unittest.TestCase):
    def test_token_absent_is_blocker_not_website_live(self):
        status = broker_branch.ingest_status({})
        self.assertFalse(status["ingest_configured"])
        self.assertFalse(status["token_present"])
        self.assertNotIn("live_ingest", status)
        self.assertEqual(status["writes"], "actions_or_cli")
        self.assertEqual(status["reads"], "db")
        self.assertEqual(status["dataset"], "TaiwanStockTradingDailyReport")
        self.assertEqual(status["path"], "A")
        self.assertEqual(status["slice_decision"], "hot_n")
        self.assertEqual(status["not"], "t86_foreign")
        self.assertIn("FINMIND_TOKEN", status["blocker"])
        self.assertNotIn("securities_trader_id", status["dataset"])

    def test_token_present_enables_scheduled_ingest_only(self):
        status = broker_branch.ingest_status({"FINMIND_TOKEN": "sekrit"})
        self.assertTrue(status["ingest_configured"])
        self.assertTrue(status["token_present"])
        self.assertIsNone(status["blocker"])
        self.assertNotIn("sekrit", json.dumps(status))
        self.assertNotIn("live_ingest", status)

    def test_cli_ingest_without_token_is_exit_2(self):
        with patch.object(broker_branch, "token_present", return_value=False), \
             patch.object(broker_branch, "_connect_local") as connect, \
             patch.object(broker_branch, "fetch_trading_daily_report") as fetch:
            code = broker_branch.main(["ingest"])
        self.assertEqual(code, 2)
        connect.assert_not_called()
        fetch.assert_not_called()

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

    def test_historical_production_rows_keep_provenance_with_ingestion_disabled(self):
        broker_branch.load_fixture(self.conn, FIXTURE, dev=True)
        self.conn.execute(
            "UPDATE broker_branch_meta SET value='live' WHERE key='source'"
        )
        self.conn.commit()
        result = broker_branch.top_branches(self.conn)
        self.assertEqual(result["data_mode"], "live")
        self.assertTrue(result["buy"])
        self.assertNotIn("fixture_warning", result)
        self.assertFalse(result["ingest_configured"])
        self.assertEqual(result["writes"], "actions_or_cli")
        self.assertTrue(result["freshness"]["automatic_updates"])

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
        self.assertFalse(result["ingest_configured"])
        self.assertFalse(result["production"])
        self.assertEqual(result["data_mode"], "dev_fixture")

        top = broker_branch.top_branches(self.conn, "2026-09-03", k=3, env={})
        self.assertEqual(top["title"], "熱門股分點動向")
        self.assertNotIn("全市場", top["title"])
        self.assertEqual(top["coverage"], "hot_n")
        self.assertEqual(top["data_mode"], "dev_fixture")
        self.assertFalse(top["ingest_configured"])
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

    def test_empty_dashboard_apis_issue_no_http(self):
        boom = AssertionError("read-only requests must not call external services")
        with patch("requests.Session.get", side_effect=boom) as sess_get, \
             patch("requests.get", side_effect=boom) as req_get:
            for path, qs in (
                ("/api/broker_branch/top", {}),
                ("/api/broker_branch/broker", {"broker_id": ["1020"]}),
                ("/api/broker_branch/stock", {"id": ["2330"]}),
                ("/api/broker_branch/freshness", {}),
                ("/api/scanner/broker_main_force", {"tickers": ["2330"]}),
            ):
                body = self.call(path, **{k: v[0] for k, v in qs.items()})
                self.assertIsInstance(body, dict)
                if path == "/api/broker_branch/top":
                    self.assertEqual(body["data_mode"], "empty")
                    self.assertEqual(body["buy"], [])
                    self.assertTrue(body["freshness"]["empty"])
                    self.assertFalse(body["ingest_configured"])
                    self.assertEqual(body["writes"], "actions_or_cli")
        sess_get.assert_not_called()
        req_get.assert_not_called()


    def test_empty_api_is_honest_not_full_market(self):
        r = self.call("/api/broker_branch/top")
        self.assertEqual(r["kind"], "broker_branch")
        self.assertEqual(r["not"], "t86_foreign")
        self.assertEqual(r["data_mode"], "empty")
        self.assertEqual(r["path"], "A")
        self.assertEqual(r["slice_decision"], "hot_n")
        self.assertFalse(r["ingest_configured"])
        self.assertNotIn("live_ingest", r)
        self.assertEqual(r["writes"], "actions_or_cli")
        self.assertEqual(r["dataset"], "TaiwanStockTradingDailyReport")
        self.assertEqual(r["reads"], "db")
        self.assertEqual(r["buy"], [])
        self.assertEqual(r["sell"], [])
        self.assertTrue(r["freshness"]["empty"])
        self.assertIn("只讀", r["coverage_note"])
        self.assertNotIn("全市場", r["title"])
        json.dumps(r)

    def test_top_days_query_default_is_single_day(self):
        r = self.call("/api/broker_branch/top", days=5)
        self.assertEqual(r["data_mode"], "empty")
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
        self.assertFalse(fresh["ingest_configured"])
        self.assertNotIn("live_ingest", fresh)


class TradingDailyReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "test.db"
        self.conn = sqlite3.connect(self.db)
        collector.init_db(self.conn)
        for sid, name, turnover in (
            ("2330", "台積電", 90),
            ("2454", "聯發科", 50),
            ("00406A", "特別", 10),
        ):
            self.conn.execute(
                "INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-09-03", sid, name, 1, 1, 1, 1, 1, turnover),
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, stock, broker, name, price, buy, sell):
        return {
            "securities_trader": name,
            "price": price,
            "buy": buy,
            "sell": sell,
            "securities_trader_id": broker,
            "stock_id": stock,
            "date": "2026-09-03",
        }

    def test_aggregate_sums_price_rows_and_drops_price(self):
        mapped, brokers = broker_branch.aggregate_broker_rows([
            self._row("2330", "1020", "合庫", 508, 4000, 2000),
            self._row("2330", "1020", "合庫", 509, 1000, 0),
            self._row("2330", "5850", "統一", 510, 0, 500),
        ])
        self.assertEqual(
            mapped,
            [
                ("2026-09-03", "2330", "1020", 5000, 2000, 3000),
                ("2026-09-03", "2330", "5850", 0, 500, -500),
            ],
        )
        self.assertEqual(brokers, [("1020", "合庫"), ("5850", "統一")])
        flat = json.dumps(mapped)
        self.assertNotIn("508", flat)
        self.assertNotIn("price", flat)

    def test_fetch_uses_data_id_and_date_not_broker_or_query_token(self):
        seen = {}

        class Resp:
            status_code = 200

            def json(self):
                return {
                    "status": 200,
                    "msg": "success",
                "data": [{
                    "securities_trader": "合庫",
                    "price": 508,
                    "buy": 10,
                    "sell": 1,
                    "securities_trader_id": "1020",
                    "stock_id": "2330",
                    "date": "2026-09-03",
                }],
                }

        class Sess:
            def get(self, url, params=None, headers=None, timeout=None):
                seen["url"] = url
                seen["params"] = params
                seen["headers"] = headers
                return Resp()

        rows = broker_branch.fetch_trading_daily_report(
            "2330", "2026-09-03", "sekrit-token", session=Sess(),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            seen["url"],
            "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report",
        )
        self.assertEqual(seen["params"], {"data_id": "2330", "date": "2026-09-03"})
        self.assertNotIn("securities_trader_id", seen["params"])
        self.assertNotIn("start_date", seen["params"])
        self.assertNotIn("token", seen["params"])
        self.assertNotIn("sekrit-token", json.dumps(seen["params"]))
        self.assertEqual(seen["headers"]["Authorization"], "Bearer sekrit-token")
        self.assertNotIn("secid_agg", seen["url"])

    def test_http_400_detail_is_logged_without_token(self):
        class Resp:
            status_code = 400
            text = "sekrit-token leaked"

            def json(self):
                return {"detail": "sekrit-token parameter rejected"}

        class Sess:
            def get(self, url, params=None, headers=None, timeout=None):
                return Resp()

        with self.assertRaises(broker_branch.FinMindError) as caught:
            broker_branch.fetch_trading_daily_report(
                "00406A", "2026-09-03", "sekrit-token", session=Sess(), sleep=lambda _s: None,
            )
        message = str(caught.exception)
        self.assertIn("HTTP 400", message)
        self.assertIn("00406A", message)
        self.assertIn("parameter rejected", message)
        self.assertNotIn("sekrit-token", message)
        self.assertIn("[redacted]", message)

    def test_auth_error_aborts_without_walking_the_rest(self):
        calls = []

        def fetch(stock_id, day):
            calls.append(stock_id)
            raise broker_branch.FinMindAuthError("FinMind auth failed")

        with self.assertRaises(broker_branch.FinMindAuthError):
            broker_branch.ingest_hot_n(
                self.conn,
                trade_date="2026-09-03",
                n=3,
                env={"FINMIND_TOKEN": "sekrit-token"},
                fetcher=fetch,
            )
        self.assertEqual(calls, ["2330"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM broker_branch_daily").fetchone()[0],
            0,
        )

    def test_one_http_400_skips_and_keeps_the_job(self):
        def fetch(stock_id, day):
            if stock_id == "00406A":
                raise broker_branch.FinMindError(
                    "FinMind TradingDailyReport HTTP 400 stock_id=00406A "
                    "status=None detail=bad ticker"
                )
            return [
                self._row(stock_id, "1020", "合庫", 100, 4000, 1000),
                self._row(stock_id, "1020", "合庫", 101, 500, 0),
            ]

        result = broker_branch.ingest_hot_n(
            self.conn,
            trade_date="2026-09-03",
            n=3,
            env={"FINMIND_TOKEN": "sekrit-token"},
            fetcher=fetch,
        )
        self.assertEqual(result["dataset"], "TaiwanStockTradingDailyReport")
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["failed_stocks"], ["00406A"])
        self.assertNotIn("sekrit-token", json.dumps(result))
        stored = self.conn.execute(
            "SELECT stock_id, broker_id, buy_volume, sell_volume, net_volume "
            "FROM broker_branch_daily ORDER BY stock_id"
        ).fetchall()
        self.assertEqual(
            stored,
            [
                ("2330", "1020", 4500, 1000, 3500),
                ("2454", "1020", 4500, 1000, 3500),
            ],
        )
        self.assertEqual(
            self.conn.execute("SELECT value FROM broker_branch_meta WHERE key='source'").fetchone()[0],
            "live",
        )
        names = dict(self.conn.execute("SELECT broker_id, broker_name FROM brokers"))
        self.assertEqual(names["1020"], "合庫")

    def test_every_ticker_400_fails_the_job(self):
        def fetch(stock_id, day):
            raise broker_branch.FinMindError(f"HTTP 400 stock_id={stock_id}")

        with self.assertRaises(broker_branch.FinMindError) as caught:
            broker_branch.ingest_hot_n(
                self.conn,
                trade_date="2026-09-03",
                n=3,
                env={"FINMIND_TOKEN": "sekrit-token"},
                fetcher=fetch,
            )
        self.assertIn("failed=3/3", str(caught.exception))
        self.assertIsNone(
            self.conn.execute(
                "SELECT value FROM broker_branch_meta WHERE key='source'"
            ).fetchone()
        )

    def test_request_context_refuses_finmind_http(self):
        token = broker_branch.forbid_request_time_finmind()
        try:
            with self.assertRaises(broker_branch.FinMindError) as caught:
                broker_branch.fetch_trading_daily_report(
                    "2330", "2026-09-03", "sekrit-token",
                )
            self.assertIn("dashboard/API", str(caught.exception))
            self.assertNotIn("sekrit-token", str(caught.exception))
        finally:
            broker_branch.reset_request_time_finmind(token)


class WorkflowAndDocsTests(unittest.TestCase):

    def test_docs_and_readme_stay_hot_n_not_full_market(self):
        root = Path(__file__).resolve().parents[1]
        for rel in ("docs/broker_branch.md", "docs/broker_main_force.md", "README.md", "TODO.md"):
            text = root.joinpath(rel).read_text(encoding="utf-8")
            self.assertIn("熱門股", text)
            self.assertRegex(text, r"不是.{0,12}全市場")
            self.assertNotIn("自動更新已停用", text)
            self.assertRegex(text, r"服務讀|只讀")

    def test_workflow_is_weekday_21_taipei_trading_daily_report(self):
        root = Path(__file__).resolve().parents[1]
        text = root.joinpath(".github/workflows/update_broker_branch.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("cron: '0 13 * * 1-5'", text)
        self.assertIn("workflow_dispatch", text)
        self.assertIn("FINMIND_TOKEN: ${{ secrets.FINMIND_TOKEN }}", text)
        self.assertIn("TURSO_DATABASE_URL", text)
        self.assertIn("TradingDailyReport", text)
        self.assertNotIn("secid_agg", text.lower())
        self.assertNotIn("SecIdAgg", text)
        self.assertNotIn("eyJ", text)
        source = root.joinpath("market/broker_branch.py").read_text(encoding="utf-8")
        self.assertIn("taiwan_stock_trading_daily_report", source)
        self.assertNotIn("secid_agg", source.lower())
        self.assertNotIn("SecIdAgg", source)
        env_example = root.joinpath(".env.example").read_text(encoding="utf-8")
        self.assertRegex(env_example, r"(?m)^FINMIND_TOKEN=$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
