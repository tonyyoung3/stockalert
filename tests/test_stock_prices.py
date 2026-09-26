import sqlite3
import unittest
from datetime import date

from market.collector import init_db
from web.stock_prices import query_prices


class StockPricesTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        init_db(self.conn)
        self.today = date(2026, 9, 24)

    def insert(self, day, close, volume=10000):
        self.conn.execute("INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
                          (day, "2330", "台積電", close, close+1, close-1, close, volume, 1))

    def test_prices_available_without_institutional_data(self):
        self.insert("2026-09-23", 100)
        self.insert("2026-09-24", 102)
        result = query_prices(self.conn, "2330", today=self.today)
        self.assertEqual(result["summary"]["change_pct"], 2)
        self.assertEqual(result["data"][0]["volume"], 10000)
        self.assertEqual(result["price_mode"], "daily_close")

    def test_previous_close_outside_requested_window(self):
        self.insert("2026-09-21", 100)
        self.insert("2026-09-24", 102)
        result = query_prices(self.conn, "2330", days=1, today=self.today)
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(result["summary"]["previous_close"], 100)

    def test_first_price_has_no_invented_change(self):
        self.insert("2026-09-24", 102)
        result = query_prices(self.conn, "2330", today=self.today)
        self.assertIsNone(result["summary"]["change_pct"])

    def test_empty_and_invalid_id(self):
        self.assertIsNone(query_prices(self.conn, "2330", today=self.today)["summary"])
        self.assertEqual(query_prices(self.conn, "' OR 1=1")["error"], "invalid_stock_id")

    def test_missing_prices_not_zero_filled(self):
        self.insert("2026-09-24", 0)
        self.assertEqual(query_prices(self.conn, "2330", today=self.today)["data"], [])


class StockPriceBrowserLogicTests(unittest.TestCase):
    def test_async_ui_races_and_empty_states(self):
        import shutil
        import subprocess
        from pathlib import Path
        if not shutil.which("node"):
            self.skipTest("Node.js unavailable")
        subprocess.run(["node", "tests/js/stock_price.test.js"],
                       cwd=Path(__file__).resolve().parents[1], check=True,
                       capture_output=True, text=True)
