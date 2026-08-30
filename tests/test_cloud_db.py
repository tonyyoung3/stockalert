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
