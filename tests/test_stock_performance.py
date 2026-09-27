import sqlite3
import unittest

from web import stock_performance


class StockPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE stock_daily ("
            "trade_date TEXT, stock_id TEXT, stock_name TEXT, open REAL, high REAL, low REAL,"
            "close REAL, volume INTEGER, turnover INTEGER, PRIMARY KEY(trade_date, stock_id))"
        )

    def tearDown(self):
        self.conn.close()

    def insert(self, trade_date, stock_id, name, close):
        self.conn.execute(
            "INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
            (trade_date, stock_id, name, close, close, close, close, 1, 1),
        )

    def query(self, **values):
        return stock_performance.ranking(
            self.conn, {key: [str(value)] for key, value in values.items()}
        )

    def test_ranks_first_to_last_valid_close(self):
        for day, a, b, c in [
            ("2026-09-21", 100, 50, 30),
            ("2026-09-22", 105, 45, 33),
            ("2026-09-23", 120, 55, 34),
        ]:
            self.insert(day, "1111", "甲", a)
            self.insert(day, "2222", "乙", b)
            self.insert(day, "3333", "丙", c)
        result = self.query(days=3)
        self.assertEqual([row["stock_id"] for row in result["data"]], ["1111", "3333", "2222"])
        self.assertEqual(result["data"][0]["return_pct"], 20.0)
        self.assertEqual(result["data"][0]["first_close"], 100.0)
        self.assertEqual(result["data"][0]["last_close"], 120.0)
        self.assertEqual(result["trading_days"], 3)
        self.assertEqual(result["price_basis"], "unadjusted_close")

    def test_custom_range_swaps_dates_and_uses_available_endpoints(self):
        self.insert("2026-09-21", "1111", "甲", 100)
        self.insert("2026-09-23", "1111", "甲", 110)
        result = self.query(start="2026-09-24", end="2026-09-20")
        self.assertEqual((result["start"], result["end"]), ("2026-09-20", "2026-09-24"))
        row = result["data"][0]
        self.assertEqual((row["first_date"], row["last_date"]), ("2026-09-21", "2026-09-23"))
        self.assertEqual(row["observations"], 2)

    def test_excludes_one_observation_and_nonpositive_prices(self):
        self.insert("2026-09-21", "1111", "甲", 100)
        self.insert("2026-09-22", "1111", "甲", 0)
        self.insert("2026-09-21", "2222", "乙", 10)
        self.insert("2026-09-22", "2222", "乙", 12)
        result = self.query(days=2)
        self.assertEqual([row["stock_id"] for row in result["data"]], ["2222"])

    def test_limit_is_capped_and_ties_sort_by_stock_id(self):
        for number in range(105):
            stock_id = f"{number:04d}"
            self.insert("2026-09-21", stock_id, stock_id, 10)
            self.insert("2026-09-22", stock_id, stock_id, 11)
        result = self.query(days=2, limit=999)
        self.assertEqual(result["limit"], 100)
        self.assertEqual(len(result["data"]), 100)
        self.assertEqual(result["data"][0]["stock_id"], "0000")
        self.assertEqual(result["data"][-1]["stock_id"], "0099")

    def test_empty_table_has_stable_payload(self):
        result = self.query()
        self.assertIsNone(result["start"])
        self.assertEqual(result["data"], [])
        self.assertEqual(result["trading_days"], 0)


class StockPerformanceHtmlTests(unittest.TestCase):
    def test_homepage_has_ranking_controls_and_accessible_table(self):
        from web import dashboard

        html = dashboard.HTML
        self.assertIn('id="stock-performance-card"', html)
        self.assertIn("區間股價表現 Top 100", html)
        self.assertIn('id="sp-preset"', html)
        self.assertIn('id="sp-start"', html)
        self.assertIn('id="sp-end"', html)
        self.assertIn("/api/stock_performance", html)
        self.assertIn("function loadStockPerformance(", html)
        self.assertIn("loadStockPerformance();", html)
        self.assertIn("未還原股價", html)
        self.assertIn("至少兩筆有效收盤價", html)
        self.assertIn("openStockPerformanceRow(rows[Number(tr.dataset.i)])", html)
        self.assertLess(html.index("外資買賣超排行"), html.index('id="stock-performance-card"'))
        self.assertLess(html.index('id="stock-performance-card"'), html.index('id="broker-branch-card"'))


if __name__ == "__main__":
    unittest.main()
