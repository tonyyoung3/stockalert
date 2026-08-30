import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cloud_db


class FakeRemote:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")

    def execute(self, sql, params=()):
        return self.db.execute(sql, params)

    def executemany(self, sql, seq):
        return self.db.executemany(sql, seq)

    def commit(self):
        self.db.commit()


class ConfiguredTests(unittest.TestCase):
    def test_both_required(self):
        self.assertFalse(cloud_db.configured({}))
        self.assertFalse(cloud_db.configured({"TURSO_DATABASE_URL": "libsql://x"}))
        self.assertFalse(cloud_db.configured({"TURSO_AUTH_TOKEN": "tok"}))
        self.assertTrue(cloud_db.configured({
            "TURSO_DATABASE_URL": "libsql://x",
            "TURSO_AUTH_TOKEN": "tok",
        }))

    def test_status_without_secrets(self):
        with patch("cloud_db.configured", return_value=False):
            self.assertEqual(cloud_db.main(["status"]), 0)


class PushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "twse_data.db"
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE taiex_daily (
                trade_date TEXT PRIMARY KEY, open REAL, high REAL, low REAL, close REAL
            );
            CREATE INDEX idx_taiex_daily_date ON taiex_daily(trade_date);
            CREATE TABLE stocks (
                stock_id TEXT PRIMARY KEY, stock_name TEXT, last_seen TEXT
            );
        """)
        conn.executemany(
            "INSERT INTO taiex_daily VALUES (?,?,?,?,?)",
            [
                ("2026-08-01", 1, 2, 0.5, 1.5),
                ("2026-08-20", 2, 3, 1.5, 2.5),
                ("2026-08-29", 3, 4, 2.5, 3.5),
            ],
        )
        conn.execute("INSERT INTO stocks VALUES ('2330','台積電','2026-08-29')")
        conn.commit()
        conn.close()
        self.remote = FakeRemote()

    def tearDown(self):
        self.tmp.cleanup()

    def test_push_recent_skips_old_trade_dates_but_copies_master(self):
        counts = cloud_db.push_file(self.path, self.remote, since="2026-08-15")
        self.assertEqual(counts["taiex_daily"], 2)
        self.assertEqual(counts["stocks"], 1)
        days = [r[0] for r in self.remote.execute(
            "SELECT trade_date FROM taiex_daily ORDER BY 1")]
        self.assertEqual(days, ["2026-08-20", "2026-08-29"])
        self.assertEqual(
            self.remote.execute("SELECT stock_name FROM stocks").fetchone()[0],
            "台積電",
        )

    def test_push_is_idempotent(self):
        cloud_db.push_file(self.path, self.remote, since="2026-08-15")
        cloud_db.push_file(self.path, self.remote, since="2026-08-15")
        n = self.remote.execute("SELECT COUNT(*) FROM taiex_daily").fetchone()[0]
        self.assertEqual(n, 2)

    def test_missing_file_returns_empty(self):
        self.assertEqual(
            cloud_db.push_file(Path(self.tmp.name) / "nope.db", self.remote),
            {},
        )

    def test_rejects_unsafe_table_name(self):
        with self.assertRaises(ValueError):
            cloud_db._ident("taiex;drop")

    def test_push_market_files_noop_without_config(self):
        with patch("cloud_db.configured", return_value=False):
            self.assertEqual(cloud_db.push_market_files(files=(self.path,)), {})

    def test_alerts_filter_on_alert_date_and_performance_on_check_date(self):
        path = Path(self.tmp.name) / "screener.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                pattern_type TEXT NOT NULL,
                alert_date TEXT NOT NULL,
                price_at_alert REAL NOT NULL
            );
            CREATE TABLE performance (
                id INTEGER PRIMARY KEY,
                alert_id INTEGER NOT NULL,
                check_date TEXT NOT NULL,
                price_at_check REAL NOT NULL,
                return_pct REAL NOT NULL,
                horizon_td INTEGER NOT NULL DEFAULT 20
            );
        """)
        conn.executemany(
            "INSERT INTO alerts VALUES (?,?,?,?,?)",
            [
                (1, "2330", "upper_shadow_reversal", "2026-07-01", 100.0),
                (2, "2317", "inside_day", "2026-08-20", 50.0),
            ],
        )
        conn.executemany(
            "INSERT INTO performance VALUES (?,?,?,?,?,?)",
            [
                (1, 1, "2026-07-08", 110.0, 10.0, 5),
                (2, 1, "2026-08-20", 112.0, 12.0, 20),
            ],
        )
        conn.commit()
        conn.close()
        remote = FakeRemote()
        counts = cloud_db.push_file(path, remote, since="2026-08-15")
        self.assertEqual(counts["alerts"], 1)
        self.assertEqual(counts["performance"], 1)
        self.assertEqual(
            remote.execute("SELECT ticker FROM alerts").fetchone()[0],
            "2317",
        )
        self.assertEqual(
            remote.execute("SELECT horizon_td FROM performance").fetchone()[0],
            20,
        )

    def test_push_alert_files_full_copy_by_default(self):
        path = Path(self.tmp.name) / "screener.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE alerts (id INTEGER PRIMARY KEY, ticker TEXT, "
            "pattern_type TEXT, alert_date TEXT, price_at_alert REAL)"
        )
        conn.execute(
            "INSERT INTO alerts VALUES (1,'2330','inside_day','2026-01-01',10.0)"
        )
        conn.commit()
        conn.close()
        remote = FakeRemote()
        with patch("cloud_db.configured", return_value=False):
            counts = cloud_db.push_alert_files(files=(path,), remote=remote)
        self.assertEqual(counts["screener.db"]["alerts"], 1)

    def test_push_alerts_cli_ok_without_secrets(self):
        with patch("cloud_db.configured", return_value=False):
            self.assertEqual(cloud_db.main(["push-alerts"]), 0)
