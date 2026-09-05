#!/usr/bin/env python3
"""Chip z-score math + multi-ticker API (#78)."""
import json
import math
import tempfile
import unittest
from pathlib import Path

from market import collector
from web import chip_zscore
from web import dashboard
from web.freshness import KEY_TABLES

REPO = Path(__file__).resolve().parents[1]


def _expected_z(xs):
    n = len(xs)
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return (xs[-1] - mean) / math.sqrt(var)


class ZScoreMathTests(unittest.TestCase):
    def test_sample_zscore_matches_hand_calculation(self):
        xs = [10.0, 20.0, 30.0, 40.0, 50.0]
        # mean=30, sample var=250, std=sqrt(250), z(50)=(50-30)/sqrt(250)
        self.assertAlmostEqual(_expected_z(xs), 20.0 / math.sqrt(250.0))
        z = chip_zscore.zscore(xs[-1], xs, min_periods=5)
        self.assertAlmostEqual(z, _expected_z(xs))
        self.assertAlmostEqual(z, 1.264911, places=6)

    def test_null_when_value_missing_or_too_few_or_zero_std(self):
        self.assertIsNone(chip_zscore.zscore(None, [1, 2, 3], min_periods=2))
        self.assertIsNone(chip_zscore.zscore(3, [1, None, 3], min_periods=3))
        self.assertIsNone(chip_zscore.zscore(5, [5, 5, 5, 5], min_periods=2))
        self.assertIsNone(chip_zscore.zscore(1, [1], min_periods=1))

    def test_skips_nulls_inside_window(self):
        window = [1.0, None, 3.0, 5.0]
        z = chip_zscore.zscore(5.0, window, min_periods=3)
        self.assertAlmostEqual(z, _expected_z([1.0, 3.0, 5.0]))


class ParseQueryTests(unittest.TestCase):
    def test_tickers_comma_and_defaults(self):
        p = chip_zscore.parse_query({"tickers": ["2330, 2454,2330"]})
        self.assertEqual(p["tickers"], ["2330", "2454"])
        self.assertEqual(p["window"], 20)
        self.assertEqual(p["min_periods"], 20)
        self.assertIsNone(p["asof"])

    def test_window_and_min_periods_clamped(self):
        p = chip_zscore.parse_query({
            "tickers": ["2330"],
            "window": ["5"],
            "min_periods": ["2"],
            "asof": ["2026-09-04"],
        })
        self.assertEqual(p["window"], 5)
        self.assertEqual(p["min_periods"], 2)
        self.assertEqual(p["asof"], "2026-09-04")
        wide = chip_zscore.parse_query({"tickers": ["2330"], "window": ["9999"]})
        self.assertEqual(wide["window"], 252)
        self.assertEqual(wide["min_periods"], 252)
        bad = chip_zscore.parse_query({
            "tickers": ["2330"],
            "window": ["nope"],
            "asof": ["20260904"],
        })
        self.assertEqual(bad["window"], 20)
        self.assertIsNone(bad["asof"])


class ChipZScoreQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "twse.db"
        self._orig = collector.DB_PATH
        collector.DB_PATH = self.path
        self.conn = collector.get_conn()

    def tearDown(self):
        self.conn.close()
        collector.DB_PATH = self._orig
        self.tmp.cleanup()

    def _price(self, day, stock_id, name, close, volume=100, turnover=1000):
        self.conn.execute(
            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
            (day, stock_id, name, close - 1, close + 1, close - 2, close, volume, turnover),
        )

    def _chips(self, table, day, stock_id, name, buy, sell, net):
        self.conn.execute(
            f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?)",
            (day, stock_id, name, buy, sell, net),
        )

    def _days(self, n, start="2026-08-01"):
        """n consecutive calendar dates from start (fixtures; not a real calendar)."""
        y, m, d = (int(x) for x in start.split("-"))
        from datetime import date, timedelta
        first = date(y, m, d)
        return [(first + timedelta(days=i)).isoformat() for i in range(n)]

    def _load_series(self, stock_id, name, nets, start="2026-08-01"):
        days = self._days(len(nets), start)
        for day, net in zip(days, nets):
            self._price(day, stock_id, name, 100.0 + net, 1_000_000, 100_000_000)
            buy = max(net, 0) + 10
            sell = buy - net
            self._chips("foreign_daily", day, stock_id, name, buy, sell, net)
            self._chips("trust_daily", day, stock_id, name, 1, 0, 1)
            self._chips("dealer_daily", day, stock_id, name, 2, 1, 1)
        self.conn.commit()
        return days

    def test_multi_ticker_window_and_math(self):
        a = [10, 20, 30, 40, 50]
        b = [2, 4, 6, 8, 10]
        days = self._load_series("2330", "台積電", a)
        self._load_series("2454", "聯發科", b)
        out = chip_zscore.query_chip_zscore(
            self.conn, ["2454", "2330"], window=5, asof=days[-1],
        )
        self.assertEqual(out["window"], 5)
        self.assertEqual(out["min_periods"], 5)
        self.assertEqual(out["asof"], days[-1])
        self.assertEqual(out["ddof"], 1)
        self.assertEqual([r["stock_id"] for r in out["data"]], ["2454", "2330"])
        tsmc = out["data"][1]
        self.assertEqual(tsmc["stock_name"], "台積電")
        self.assertEqual(tsmc["trade_date"], days[-1])
        self.assertEqual(tsmc["foreign_net"], 50)
        self.assertEqual(tsmc["foreign_net_n"], 5)
        self.assertFalse(tsmc["insufficient_sample"])
        self.assertEqual(tsmc["sample_count"], 5)
        self.assertAlmostEqual(tsmc["foreign_net_z"], _expected_z(a), places=6)
        self.assertAlmostEqual(tsmc["foreign_net_z"], 1.264911, places=6)
        md = out["data"][0]
        self.assertAlmostEqual(md["foreign_net_z"], _expected_z(b), places=6)
        self.assertEqual(md["trust_net"], 1)
        self.assertIsNone(md["trust_net_z"])  # constant 1 → std 0
        self.assertIn("foreign_buy_z", tsmc)
        self.assertIn("dealer_net_z", tsmc)

    def test_insufficient_sample_when_history_shorter_than_window(self):
        days = self._load_series("2330", "台積電", [1, 2, 3])
        out = chip_zscore.query_chip_zscore(
            self.conn, ["2330", "9999"], window=20, asof=days[-1],
        )
        tsmc, missing = out["data"]
        self.assertTrue(tsmc["insufficient_sample"])
        self.assertEqual(tsmc["sample_count"], 3)
        self.assertIsNone(tsmc["foreign_net_z"])  # min_periods defaults to 20
        self.assertTrue(missing["insufficient_sample"])
        self.assertIsNone(missing["trade_date"])
        self.assertIsNone(missing["foreign_net_z"])

    def test_min_periods_can_compute_z_while_flag_stays_short(self):
        xs = [10, 20, 30, 40, 50]
        days = self._load_series("2330", "台積電", xs)
        out = chip_zscore.query_chip_zscore(
            self.conn, ["2330"], window=20, min_periods=5, asof=days[-1],
        )
        row = out["data"][0]
        self.assertTrue(row["insufficient_sample"])
        self.assertEqual(row["sample_count"], 5)
        self.assertAlmostEqual(row["foreign_net_z"], _expected_z(xs), places=6)

    def test_asof_cuts_the_window(self):
        xs = [10, 20, 30, 40, 50]
        days = self._load_series("2330", "台積電", xs)
        out = chip_zscore.query_chip_zscore(
            self.conn, ["2330"], window=3, asof=days[2],
        )
        row = out["data"][0]
        self.assertEqual(row["trade_date"], days[2])
        self.assertEqual(row["foreign_net"], 30)
        self.assertAlmostEqual(row["foreign_net_z"], _expected_z([10, 20, 30]), places=6)

    def test_otc_null_chips_yield_null_z(self):
        days = self._days(5)
        for day in days:
            self._price(day, "6488", "環球晶", 500.0)
        self.conn.commit()
        out = chip_zscore.query_chip_zscore(
            self.conn, ["6488"], window=5, asof=days[-1],
        )
        row = out["data"][0]
        self.assertFalse(row["insufficient_sample"])
        self.assertIsNone(row["foreign_net"])
        self.assertIsNone(row["foreign_net_z"])
        self.assertEqual(row["foreign_net_n"], 0)
        self.assertEqual(row["close"], 500.0)

    def test_missing_tickers_error(self):
        out = chip_zscore.query_chip_zscore(self.conn, [])
        self.assertEqual(out["error"], "missing_tickers")
        self.assertEqual(out["data"], [])
        self.assertEqual(out["window"], 20)


class ChipZScoreDashboardAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "twse.db"
        self._orig = collector.DB_PATH
        collector.DB_PATH = self.path
        self.conn = collector.get_conn()
        from data import market_db
        self._mdb = market_db
        market_db.set_db_path(self.path)

    def tearDown(self):
        self.conn.close()
        collector.DB_PATH = self._orig
        self._mdb.set_db_path(None)
        self.tmp.cleanup()

    def call(self, path, **qs):
        return dashboard.api(path, {k: [str(v)] for k, v in qs.items()})

    def test_api_multi_ticker_json(self):
        from datetime import date, timedelta
        first = date(2026, 8, 1)
        for i, net in enumerate([10, 20, 30, 40, 50]):
            day = (first + timedelta(days=i)).isoformat()
            self.conn.execute(
                "INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
                (day, "2330", "台積電", 1, 2, 0, 100 + net, 10, 100),
            )
            self.conn.execute(
                "INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                (day, "2330", "台積電", net + 10, 10, net),
            )
        self.conn.commit()
        r = self.call(
            "/api/scanner/chip_zscore",
            tickers="2330,2454",
            window=5,
            asof="2026-08-05",
        )
        json.dumps(r)
        self.assertEqual(r["window"], 5)
        self.assertEqual(len(r["data"]), 2)
        self.assertEqual(r["data"][0]["stock_id"], "2330")
        self.assertFalse(r["data"][0]["insufficient_sample"])
        self.assertAlmostEqual(r["data"][0]["foreign_net_z"], 1.264911, places=6)
        self.assertTrue(r["data"][1]["insufficient_sample"])

    def test_existing_dashboard_paths_still_hit_base_tables(self):
        self.conn.execute(
            "INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
            ("2026-09-04", "2330", "台積電", 100, 40, 60),
        )
        self.conn.execute(
            "INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-09-04", "2330", "台積電", 1, 2, 0, 1460, 10, 100),
        )
        self.conn.commit()
        summary = self.call("/api/summary")
        self.assertEqual(summary["latest_date"], "2026-09-04")
        self.assertEqual(summary["foreign_net_total"], 60)
        stock = self.call("/api/stock", id="2330", days=90)
        self.assertEqual(stock["data"][0][4], 60)
        ohlc = self.call("/api/stock_ohlc", id="2330", days=90)
        self.assertEqual(ohlc["data"][0], ["2026-09-04", 1460])

    def test_dashboard_module_does_not_embed_view_sql(self):
        """Existing dashboard read paths stay on base tables (#77 / #78)."""
        self.assertEqual(
            KEY_TABLES,
            ("foreign_daily", "stock_daily", "taifex", "alerts"),
        )
        dash = (REPO / "web/dashboard.py").read_text()
        fresh = (REPO / "web/freshness.py").read_text()
        self.assertNotIn("stock_chips_daily", dash)
        self.assertNotIn("stock_chips_daily", fresh)
        self.assertIn("stock_chips_daily", (REPO / "web/chip_zscore.py").read_text())
        self.assertIn("/api/scanner/chip_zscore", dash)
        self.assertIn("/api/scanner/chip_zscore", dashboard.HTML)
        self.assertIn("function loadScanner(", dashboard.HTML)
