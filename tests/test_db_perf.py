"""Query-plan and catch-up helpers for the sqlite performance pass."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alertsdb import store as alerts_db
from data import sqlite_util
from market import backfill, collector, taifex_collector, us_collector


def _plan(conn, sql, params=()):
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(row[-1] for row in rows)


class SqliteUtilTests(unittest.TestCase):
    def test_busy_timeout_and_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "local.db")
            sqlite_util.configure_local(conn)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            conn.close()

    def test_readonly_skips_wal(self):
        conn = sqlite3.connect(":memory:")
        sqlite_util.configure_local(conn, wal=False)
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        conn.close()


class CoveringIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "twse.db"
        self._orig = collector.DB_PATH
        collector.DB_PATH = self.path
        collector.get_conn().close()

    def tearDown(self):
        collector.DB_PATH = self._orig
        self.tmp.cleanup()

    def test_init_db_creates_covering_indexes(self):
        names = {
            row[0]
            for row in sqlite3.connect(self.path).execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        self.assertTrue(
            {
                "idx_stock_daily_id_date",
                "idx_foreign_stock_date",
                "idx_trust_stock_date",
                "idx_dealer_stock_date",
                "idx_margin_stock_date",
                "idx_taiex_hourly_trade_date",
                "idx_taiex_hourly_ohlc_trade_date",
            } <= names
        )

    def test_stock_daily_range_uses_covering_index(self):
        conn = sqlite3.connect(self.path)
        rows = [
            (f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", f"{1000 + (i % 80):04d}",
             "x", 1.0, 1.0, 1.0, 1.0, 1, 1)
            for i in range(2000)
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)", rows)
        conn.execute("ANALYZE")
        conn.commit()
        plan = _plan(
            conn,
            "SELECT trade_date, close FROM stock_daily "
            "WHERE stock_id=? AND trade_date >= ? ORDER BY trade_date",
            ("1050", "2025-01-01"),
        )
        conn.close()
        self.assertIn("idx_stock_daily_id_date", plan)
        self.assertIn("SEARCH", plan)

    def test_existing_dates_can_limit_to_catchup_window(self):
        conn = sqlite3.connect(self.path)
        conn.executemany(
            "INSERT INTO stock_daily (trade_date, stock_id) VALUES (?, ?)",
            [("2020-01-02", "2330"), ("2026-08-01", "2330"), ("2026-08-02", "2317")],
        )
        conn.commit()
        self.assertEqual(
            backfill.existing_dates(conn, "stock_daily"),
            {"2020-01-02", "2026-08-01", "2026-08-02"},
        )
        self.assertEqual(
            backfill.existing_dates(conn, "stock_daily", since="2026-08-01"),
            {"2026-08-01", "2026-08-02"},
        )
        with self.assertRaises(ValueError):
            backfill.existing_dates(conn, "sqlite_master")
        conn.close()

    def test_existing_dates_allows_trust_and_dealer(self):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO trust_daily (trade_date, stock_id) VALUES ('2026-09-04','2330')")
        conn.execute("INSERT INTO dealer_daily (trade_date, stock_id) VALUES ('2026-09-04','2330')")
        conn.commit()
        self.assertEqual(backfill.existing_dates(conn, "trust_daily"), {"2026-09-04"})
        self.assertEqual(backfill.existing_dates(conn, "dealer_daily"), {"2026-09-04"})
        conn.close()

    def test_stock_daily_counts_can_limit_to_catchup_window(self):
        conn = sqlite3.connect(self.path)
        conn.executemany(
            "INSERT INTO stock_daily (trade_date, stock_id) VALUES (?, ?)",
            [
                ("2020-01-02", "2330"),
                ("2026-08-01", "2330"),
                ("2026-08-01", "2317"),
                ("2026-08-02", "2317"),
            ],
        )
        conn.commit()
        self.assertEqual(
            backfill.stock_daily_counts(conn),
            {"2020-01-02": 1, "2026-08-01": 2, "2026-08-02": 1},
        )
        self.assertEqual(
            backfill.stock_daily_counts(conn, since="2026-08-01"),
            {"2026-08-01": 2, "2026-08-02": 1},
        )
        conn.close()


class AlertsIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        alerts_db.set_db_path(Path(self.tmp.name) / "screener.db")
        alerts_db.init_db()

    def tearDown(self):
        alerts_db.set_db_path(None)
        self.tmp.cleanup()

    def test_alert_date_index_and_plan(self):
        for i in range(40):
            alerts_db.save_alert("2330", "inside_day", f"2026-07-{(i % 28) + 1:02d}", 10.0 + i)
        with alerts_db.get_conn() as conn:
            names = {row[1] for row in conn.execute("PRAGMA index_list(alerts)")}
            self.assertIn("idx_alerts_alert_date", names)
            plan = _plan(
                conn,
                "SELECT id FROM alerts WHERE alert_date <= ? ORDER BY alert_date",
                ("2026-07-15",),
            )
        self.assertIn("idx_alerts_alert_date", plan)


class OtherSchemaTests(unittest.TestCase):
    def test_taifex_product_date_index(self):
        conn = sqlite3.connect(":memory:")
        taifex_collector.init_db(conn)
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        self.assertIn("idx_fut_oi_product_date", names)
        conn.close()

    def test_us_daily_date_index(self):
        conn = sqlite3.connect(":memory:")
        us_collector.init_db(conn)
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        self.assertIn("idx_us_daily_date", names)
        conn.close()


class DashboardConnectionReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "twse.db"
        self._orig = collector.DB_PATH
        collector.DB_PATH = self.path
        with collector.get_conn() as conn:
            conn.execute(
                "INSERT INTO taiex_hourly VALUES "
                "('2026-07-31T13:00:00','2026-07-31',43119.75,120.5,4321.0)"
            )
            conn.execute(
                "INSERT INTO foreign_daily VALUES "
                "('2026-07-31','2330','台積電',1,0,1)"
            )
        from data import market_db
        from web import dashboard
        self.market_db = market_db
        self.dashboard = dashboard
        market_db.set_db_path(self.path)

    def tearDown(self):
        from data import market_db
        market_db.set_db_path(None)
        collector.DB_PATH = self._orig
        self.tmp.cleanup()

    def test_summary_opens_one_connection(self):
        calls = {"n": 0}
        real = self.market_db.connect

        def wrapped(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        with patch.object(self.market_db, "connect", wrapped):
            result = self.dashboard.api("/api/summary", {})
        self.assertEqual(result["latest_date"], "2026-07-31")
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
