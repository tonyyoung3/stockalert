#!/usr/bin/env python3
"""stock_chips_daily VIEW contract (#77)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from market import collector


STOCK_CHIPS_COLUMNS = [
    "trade_date",
    "stock_id",
    "stock_name",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "foreign_buy",
    "foreign_sell",
    "foreign_net",
    "trust_buy",
    "trust_sell",
    "trust_net",
    "dealer_buy",
    "dealer_sell",
    "dealer_net",
]


class StockChipsDailyTests(unittest.TestCase):
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

    def _insert_price(self, trade_date, stock_id, stock_name, close, volume=100, turnover=1000):
        self.conn.execute(
            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
            (trade_date, stock_id, stock_name, close - 1, close + 1, close - 2, close,
             volume, turnover),
        )

    def _insert_chips(self, table, trade_date, stock_id, stock_name, buy, sell, net):
        self.conn.execute(
            f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?)",
            (trade_date, stock_id, stock_name, buy, sell, net),
        )

    def _load_fixtures(self):
        # Listed, full chips both days
        self._insert_price("2026-09-03", "2330", "台積電", 1450.0, 10_000_000, 14_500_000_000)
        self._insert_price("2026-09-04", "2330", "台積電", 1460.0, 11_000_000, 16_000_000_000)
        self._insert_chips("foreign_daily", "2026-09-03", "2330", "台積電", 100, 40, 60)
        self._insert_chips("foreign_daily", "2026-09-04", "2330", "台積電", 200, 50, 150)
        self._insert_chips("trust_daily", "2026-09-03", "2330", "台積電", 10, 3, 7)
        self._insert_chips("trust_daily", "2026-09-04", "2330", "台積電", 12, 2, 10)
        self._insert_chips("dealer_daily", "2026-09-03", "2330", "台積電", 5, 1, 4)
        self._insert_chips("dealer_daily", "2026-09-04", "2330", "台積電", 8, 6, 2)
        # Listed, foreign only on one day
        self._insert_price("2026-09-04", "2317", "鴻海", 200.0, 5_000_000, 1_000_000_000)
        self._insert_chips("foreign_daily", "2026-09-04", "2317", "鴻海", 80, 90, -10)
        # OTC-like: price only (T86 tables have no OTC)
        self._insert_price("2026-09-03", "6488", "環球晶", 500.0, 1_234_000, 600_000_000)
        self._insert_price("2026-09-04", "6488", "環球晶", 505.0, 1_100_000, 550_000_000)
        # Chip row with no stock_daily must not appear
        self._insert_chips("foreign_daily", "2026-09-04", "1101", "台泥", 1, 0, 1)
        self.conn.commit()

    def test_view_exists_with_contract_columns(self):
        row = self.conn.execute(
            "SELECT type FROM sqlite_master WHERE name='stock_chips_daily'"
        ).fetchone()
        self.assertEqual(row[0], "view")
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(stock_chips_daily)")]
        self.assertEqual(cols, STOCK_CHIPS_COLUMNS)

    def test_join_correctness_on_fixtures(self):
        self._load_fixtures()
        rows = self.conn.execute(
            "SELECT trade_date, stock_id, stock_name, close, volume, turnover, "
            "foreign_buy, foreign_sell, foreign_net, "
            "trust_buy, trust_sell, trust_net, "
            "dealer_buy, dealer_sell, dealer_net "
            "FROM stock_chips_daily "
            "WHERE trade_date >= '2026-09-03' "
            "AND stock_id IN ('2330','2317','6488','1101') "
            "ORDER BY trade_date, stock_id"
        ).fetchall()
        by_key = {(r[0], r[1]): r for r in rows}
        self.assertNotIn(("2026-09-04", "1101"), by_key)
        self.assertEqual(len(rows), 5)

        tsmc_d1 = by_key[("2026-09-03", "2330")]
        self.assertEqual(tsmc_d1[2], "台積電")
        self.assertEqual(tsmc_d1[3], 1450.0)
        self.assertEqual(tsmc_d1[4], 10_000_000)
        self.assertEqual(tsmc_d1[5], 14_500_000_000)
        self.assertEqual(tsmc_d1[6:15], (100, 40, 60, 10, 3, 7, 5, 1, 4))

        tsmc_d2 = by_key[("2026-09-04", "2330")]
        self.assertEqual(tsmc_d2[3], 1460.0)
        self.assertEqual(tsmc_d2[6:15], (200, 50, 150, 12, 2, 10, 8, 6, 2))

        honhai = by_key[("2026-09-04", "2317")]
        self.assertEqual(honhai[6:15], (80, 90, -10, None, None, None, None, None, None))

        otc = by_key[("2026-09-03", "6488")]
        self.assertEqual(otc[2], "環球晶")
        self.assertEqual(otc[3], 500.0)
        self.assertTrue(all(v is None for v in otc[6:15]))

    def test_recent_n_days_many_tickers(self):
        self._load_fixtures()
        rows = self.conn.execute(
            "SELECT stock_id, COUNT(*) FROM stock_chips_daily "
            "WHERE trade_date >= '2026-09-03' "
            "AND stock_id IN ('2330','2317','6488') "
            "GROUP BY stock_id ORDER BY stock_id"
        ).fetchall()
        self.assertEqual(rows, [("2317", 1), ("2330", 2), ("6488", 2)])

    def test_view_tracks_base_table_writes_without_refresh(self):
        self._insert_price("2026-09-04", "2330", "台積電", 1460.0)
        self.conn.commit()
        self.assertIsNone(
            self.conn.execute(
                "SELECT foreign_net FROM stock_chips_daily "
                "WHERE trade_date='2026-09-04' AND stock_id='2330'"
            ).fetchone()[0]
        )
        self._insert_chips("foreign_daily", "2026-09-04", "2330", "台積電", 1, 0, 1)
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT foreign_net FROM stock_chips_daily "
                "WHERE trade_date='2026-09-04' AND stock_id='2330'"
            ).fetchone()[0],
            1,
        )

    def test_dashboard_base_tables_still_queryable(self):
        """Existing dashboard read paths keep hitting base tables."""
        self._load_fixtures()
        latest = self.conn.execute("SELECT MAX(trade_date) FROM foreign_daily").fetchone()[0]
        self.assertEqual(latest, "2026-09-04")
        tot = self.conn.execute(
            "SELECT SUM(foreign_net), COUNT(*) FROM foreign_daily WHERE trade_date=?",
            (latest,),
        ).fetchone()
        self.assertEqual(tot, (141, 3))
        close = self.conn.execute(
            "SELECT trade_date, close, stock_name FROM stock_daily "
            "WHERE stock_id=? ORDER BY trade_date",
            ("2330",),
        ).fetchall()
        self.assertEqual(len(close), 2)
        self.assertEqual(close[-1], ("2026-09-04", 1460.0, "台積電"))
        trust_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trust_daily)")}
        self.assertEqual(
            trust_cols,
            {"trade_date", "stock_id", "stock_name", "trust_buy", "trust_sell", "trust_net"},
        )
