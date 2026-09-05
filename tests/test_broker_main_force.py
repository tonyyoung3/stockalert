#!/usr/bin/env python3
"""Hot-N broker main-force metrics (#98). Query-time; no FinMind."""
import json
import tempfile
import unittest
from pathlib import Path

from market import broker_branch, collector
from web import broker_main_force
from web import dashboard

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "broker_branch_sample.json"
SRC = (REPO / "web" / "broker_main_force.py").read_text(encoding="utf-8")


class MathTests(unittest.TestCase):
    def test_buy_and_sell_concentration_hand_calc(self):
        # buy 100+80+20=200; top-2=180 → 0.9
        # sell |-50|+|-30|+|-10|=90; top-2=80 → 80/90
        nets = [100, 80, 20, -50, -30, -10]
        self.assertAlmostEqual(broker_main_force.buy_concentration(nets, 2), 0.9)
        self.assertAlmostEqual(broker_main_force.sell_concentration(nets, 2), 80 / 90)
        self.assertAlmostEqual(broker_main_force.buy_concentration(nets, 5), 1.0)
        self.assertAlmostEqual(broker_main_force.sell_concentration(nets, 1), 50 / 90)

    def test_null_when_one_side_missing(self):
        self.assertIsNone(broker_main_force.buy_concentration([-1, -2], 5))
        self.assertIsNone(broker_main_force.sell_concentration([1, 2], 5))
        self.assertIsNone(broker_main_force.buy_concentration([0, 0], 5))
        self.assertIsNone(broker_main_force.sell_concentration([], 5))

    def test_lead_branch_max_abs_signed_net(self):
        rows = [
            {"broker_id": "A", "broker_name": "甲", "net_volume": 100},
            {"broker_id": "B", "broker_name": "乙", "net_volume": -250},
            {"broker_id": "C", "broker_name": "丙", "net_volume": 200},
        ]
        lead = broker_main_force.lead_branch(rows)
        self.assertEqual(lead["lead_broker_id"], "B")
        self.assertEqual(lead["lead_broker_name"], "乙")
        self.assertEqual(lead["lead_branch_net"], -250)

    def test_lead_tie_prefers_larger_signed_then_id(self):
        rows = [
            {"broker_id": "1020", "broker_name": "合庫", "net_volume": -100},
            {"broker_id": "9A00", "broker_name": "永豐", "net_volume": 100},
        ]
        lead = broker_main_force.lead_branch(rows)
        self.assertEqual(lead["lead_broker_id"], "9A00")
        self.assertEqual(lead["lead_branch_net"], 100)


class ParseQueryTests(unittest.TestCase):
    def test_tickers_k_asof_defaults(self):
        p = broker_main_force.parse_query({"tickers": ["2330, 2454,2330"]})
        self.assertEqual(p["tickers"], ["2330", "2454"])
        self.assertEqual(p["k"], 5)
        self.assertIsNone(p["asof"])

    def test_k_clamped_and_asof_validated(self):
        p = broker_main_force.parse_query({
            "tickers": ["2330"],
            "k": ["2"],
            "asof": ["2026-09-03"],
        })
        self.assertEqual(p["k"], 2)
        self.assertEqual(p["asof"], "2026-09-03")
        wide = broker_main_force.parse_query({"tickers": ["2330"], "k": ["999"]})
        self.assertEqual(wide["k"], 50)
        bad = broker_main_force.parse_query({
            "tickers": ["2330"],
            "k": ["nope"],
            "asof": ["20260903"],
        })
        self.assertEqual(bad["k"], 5)
        self.assertIsNone(bad["asof"])


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "twse.db"
        self._orig = collector.DB_PATH
        collector.DB_PATH = self.path
        self.conn = collector.get_conn()
        self.conn.execute("INSERT INTO stocks VALUES ('2330','台積電','2026-09-03')")
        self.conn.execute("INSERT INTO stocks VALUES ('2317','鴻海','2026-09-03')")
        self.conn.execute("INSERT INTO stocks VALUES ('2454','聯發科','2026-09-03')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        collector.DB_PATH = self._orig
        self.tmp.cleanup()

    def test_fixture_multi_ticker_and_missing_hot_n(self):
        broker_branch.load_fixture(self.conn, FIXTURE, dev=True)
        # 2330: +400000, -280000, +10000 → buy 410000, sell 280000
        # k=1 buy = 400000/410000; sell = 1.0; lead = +400000 (1020)
        out = broker_main_force.query_broker_main_force(
            self.conn, ["2454", "2330", "1101"], k=1, asof="2026-09-03", env={},
        )
        self.assertEqual(out["title"], "熱門股分點動向")
        self.assertNotIn("全市場", out["title"])
        self.assertNotIn("全市場", out["coverage_note"])
        self.assertEqual(out["coverage"], "hot_n")
        self.assertEqual(out["path"], "A")
        self.assertEqual(out["slice_decision"], "hot_n")
        self.assertEqual(out["k"], 1)
        self.assertEqual(out["asof"], "2026-09-03")
        self.assertEqual(out["universe_count"], 3)
        self.assertEqual([r["stock_id"] for r in out["data"]], ["2454", "2330", "1101"])

        tsmc = out["data"][1]
        self.assertEqual(tsmc["stock_name"], "台積電")
        self.assertTrue(tsmc["in_hot_n"])
        self.assertEqual(tsmc["branch_count"], 3)
        self.assertEqual(tsmc["buy_side_sum"], 410000)
        self.assertEqual(tsmc["sell_side_sum"], 280000)
        self.assertEqual(tsmc["buy_top_k_sum"], 400000)
        self.assertEqual(tsmc["sell_top_k_sum"], 280000)
        self.assertAlmostEqual(tsmc["buy_concentration"], 400000 / 410000, places=6)
        self.assertEqual(tsmc["sell_concentration"], 1.0)
        self.assertEqual(tsmc["lead_broker_id"], "1020")
        self.assertEqual(tsmc["lead_broker_name"], "合庫")
        self.assertEqual(tsmc["lead_branch_net"], 400000)
        self.assertEqual(tsmc["trade_date"], "2026-09-03")

        md = out["data"][0]
        # 2454: +5000, -30000, +70000 → buy 75000; k=1 buy=70000/75000; lead +70000
        self.assertTrue(md["in_hot_n"])
        self.assertAlmostEqual(md["buy_concentration"], 70000 / 75000, places=6)
        self.assertEqual(md["sell_concentration"], 1.0)
        self.assertEqual(md["lead_broker_id"], "9A00")
        self.assertEqual(md["lead_branch_net"], 70000)

        missing = out["data"][2]
        self.assertFalse(missing["in_hot_n"])
        self.assertIsNone(missing["trade_date"])
        self.assertIsNone(missing["buy_concentration"])
        self.assertIsNone(missing["sell_concentration"])
        self.assertIsNone(missing["lead_branch_net"])

    def test_asof_defaults_to_latest_ingest_day(self):
        broker_branch.load_fixture(self.conn, FIXTURE, dev=True)
        out = broker_main_force.query_broker_main_force(self.conn, ["2330"], env={})
        self.assertEqual(out["asof"], "2026-09-03")
        self.assertTrue(out["data"][0]["in_hot_n"])

    def test_wrong_day_is_empty_not_fallback(self):
        broker_branch.load_fixture(self.conn, FIXTURE, dev=True)
        out = broker_main_force.query_broker_main_force(
            self.conn, ["2330"], asof="2026-09-02", env={},
        )
        self.assertEqual(out["coverage"], "hot_n")
        self.assertEqual(out["asof"], "2026-09-02")
        self.assertEqual(out["universe_count"], 0)
        self.assertFalse(out["data"][0]["in_hot_n"])
        self.assertIsNone(out["data"][0]["lead_branch_net"])

    def test_empty_table_title_stays_hot_n(self):
        out = broker_main_force.query_broker_main_force(
            self.conn, ["2330"], asof="2026-09-03", env={},
        )
        self.assertEqual(out["coverage"], "empty")
        self.assertEqual(out["title"], "熱門股分點動向")
        self.assertNotIn("全市場", out["title"])
        self.assertIn("不是全市場", out["coverage_note"])
        self.assertFalse(out["data"][0]["in_hot_n"])

    def test_missing_tickers_error(self):
        out = broker_main_force.query_broker_main_force(self.conn, [])
        self.assertEqual(out["error"], "missing_tickers")
        self.assertEqual(out["data"], [])
        self.assertEqual(out["k"], 5)
        self.assertEqual(out["title"], "熱門股分點動向")

    def test_never_labels_full_market(self):
        broker_branch.load_fixture(self.conn, FIXTURE, dev=True)
        out = broker_main_force.query_broker_main_force(
            self.conn, ["2330"], k=5, asof="2026-09-03", env={},
        )
        dumped = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("全市場", dumped)
        self.assertNotEqual(out["coverage"], "full_market")


class NoFinMindTests(unittest.TestCase):
    def test_module_has_no_finmind_client(self):
        self.assertNotIn("FINMIND", SRC)
        self.assertNotIn("finmind", SRC.lower())
        self.assertNotIn("requests", SRC)
        self.assertNotIn("TaiwanStockTradingDailyReport", SRC)
        self.assertNotIn("live_ingest", SRC)
        self.assertIn("broker_branch_daily", SRC)


class DashboardAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "twse.db"
        self._orig = collector.DB_PATH
        collector.DB_PATH = self.path
        self.conn = collector.get_conn()
        from data import market_db
        self._mdb = market_db
        market_db.set_db_path(self.path)
        self.conn.execute("INSERT INTO stocks VALUES ('2330','台積電','2026-09-03')")
        self.conn.execute("INSERT INTO stocks VALUES ('2454','聯發科','2026-09-03')")
        self.conn.commit()
        broker_branch.load_fixture(self.conn, FIXTURE, dev=True)

    def tearDown(self):
        self.conn.close()
        collector.DB_PATH = self._orig
        self._mdb.set_db_path(None)
        self.tmp.cleanup()

    def call(self, path, **qs):
        return dashboard.api(path, {k: [str(v)] for k, v in qs.items()})

    def test_api_multi_ticker_json(self):
        r = self.call(
            "/api/scanner/broker_main_force",
            tickers="2330,2454,1101",
            asof="2026-09-03",
            k=1,
        )
        json.dumps(r, ensure_ascii=False)
        self.assertEqual(r["title"], "熱門股分點動向")
        self.assertNotIn("全市場", r["title"])
        self.assertEqual(r["coverage"], "hot_n")
        self.assertEqual(len(r["data"]), 3)
        self.assertEqual(r["data"][0]["stock_id"], "2330")
        self.assertTrue(r["data"][0]["in_hot_n"])
        self.assertAlmostEqual(r["data"][0]["buy_concentration"], 400000 / 410000, places=6)
        self.assertTrue(r["data"][1]["in_hot_n"])
        self.assertFalse(r["data"][2]["in_hot_n"])

    def test_dashboard_routes_and_docs_without_ui(self):
        dash = (REPO / "web/dashboard.py").read_text(encoding="utf-8")
        html = dashboard.HTML
        self.assertIn("/api/scanner/broker_main_force", dash)
        self.assertIn("broker_main_force_mod.api_broker_main_force", dash)
        # No scanner UI this ticket — SWE later. Do not add a second fetch path.
        self.assertNotIn("/api/scanner/broker_main_force", html)
        self.assertIn("/api/scanner/chip_zscore", html)

    def test_existing_broker_branch_paths_intact(self):
        top = self.call("/api/broker_branch/top", date="2026-09-03", k=3)
        self.assertEqual(top["title"], "熱門股分點動向")
        self.assertEqual(top["coverage"], "hot_n")
        self.assertEqual(top["buy"][0][0], "1020")
        stock = self.call("/api/broker_branch/stock", id="2330", date="2026-09-03")
        self.assertEqual(stock["coverage"], "single_stock")
        self.assertEqual(stock["data"][0][0], "1020")


class DocsTests(unittest.TestCase):
    def test_docs_readme_hot_n_not_full_market(self):
        for rel in (
            "docs/broker_main_force.md",
            "docs/broker_branch.md",
            "README.md",
        ):
            text = (REPO / rel).read_text(encoding="utf-8")
            self.assertIn("熱門股", text)
            self.assertRegex(text, r"不是.{0,12}全市場")
            self.assertIn("/api/scanner/broker_main_force", text)
            self.assertNotIn("全市場分點主力", text)

    def test_title_constant_matches_path_a(self):
        self.assertEqual(broker_main_force.TITLE, "熱門股分點動向")
        self.assertEqual(broker_branch.market_title("hot_n"), broker_main_force.TITLE)
        self.assertNotIn("全市場", broker_main_force.TITLE)


if __name__ == "__main__":
    unittest.main()
