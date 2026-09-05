import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data import market_db


class ListenTests(unittest.TestCase):
    def test_local_defaults(self):
        host, port = market_db.listen_host_port({})
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 8765)
        self.assertTrue(market_db.should_open_browser({}))

    def test_cloud_run_binds_all_interfaces(self):
        env = {"PORT": "8080", "K_SERVICE": "stockalert"}
        host, port = market_db.listen_host_port(env)
        self.assertEqual(host, "0.0.0.0")
        self.assertEqual(port, 8080)
        self.assertFalse(market_db.should_open_browser(env))

    def test_port_alone_is_public(self):
        host, port = market_db.listen_host_port({"PORT": "9000"})
        self.assertEqual((host, port), ("0.0.0.0", 9000))
        self.assertFalse(market_db.should_open_browser({"PORT": "9000"}))

    def test_must_listen_on_cloud_run(self):
        self.assertTrue(market_db.must_listen({"PORT": "8080"}))
        self.assertTrue(market_db.must_listen({"K_SERVICE": "stockalert"}))
        self.assertFalse(market_db.must_listen({}))


class ImageTests(unittest.TestCase):
    def test_dockerfile_copies_dashboard_imports(self):
        root = Path(__file__).resolve().parents[1]
        text = root.joinpath("Dockerfile").read_text()
        self.assertIn("COPY data/", text)
        self.assertIn("COPY web/", text)
        self.assertIn("COPY alertsdb/", text)
        ignored = {
            line.strip()
            for line in root.joinpath(".dockerignore").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertNotIn("alertsdb", ignored)
        self.assertNotIn("alertsdb/", ignored)


class PathTests(unittest.TestCase):
    def tearDown(self):
        market_db.set_db_path(None)

    def test_override_wins_over_turso(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "twse.db"
            path.write_bytes(b"")
            market_db.set_db_path(path)
            with patch.dict(os.environ, {
                "TURSO_DATABASE_URL": "libsql://x",
                "TURSO_AUTH_TOKEN": "tok",
            }):
                self.assertFalse(market_db.using_turso())
                self.assertTrue(market_db.available())
                self.assertEqual(market_db.local_path(), path)

    def test_turso_counts_as_available_without_local_file(self):
        env = {"TURSO_DATABASE_URL": "libsql://x", "TURSO_AUTH_TOKEN": "tok"}
        self.assertTrue(market_db.using_turso(env))
        self.assertTrue(market_db.available(env))


class SnapshotTests(unittest.TestCase):
    def tearDown(self):
        market_db.set_db_path(None)

    def test_backtest_conn_reads_overridden_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "twse.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE taiex_daily (trade_date TEXT, close REAL)")
            conn.execute("INSERT INTO taiex_daily VALUES ('2026-08-01', 1.0)")
            conn.commit()
            conn.close()
            market_db.set_db_path(path)
            bt = market_db.connect_for_backtest()
            try:
                row = bt.execute("SELECT close FROM taiex_daily").fetchone()
                self.assertEqual(row[0], 1.0)
            finally:
                bt.close()

    def test_copy_table_into_snapshot(self):
        src = sqlite3.connect(":memory:")
        src.executescript("""
            CREATE TABLE taiex_daily (trade_date TEXT PRIMARY KEY, close REAL);
            INSERT INTO taiex_daily VALUES ('2026-08-01', 2.5);
        """)
        dest = sqlite3.connect(":memory:")
        n = market_db._copy_table(src, dest, "taiex_daily")
        dest.commit()
        self.assertEqual(n, 1)
        self.assertEqual(dest.execute("SELECT close FROM taiex_daily").fetchone()[0], 2.5)
        src.close()
        dest.close()
