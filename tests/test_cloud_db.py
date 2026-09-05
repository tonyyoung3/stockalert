import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data import cloud_db


class FakeRemote:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.sqls: list[str] = []

    def execute(self, sql, params=()):
        self.sqls.append(sql)
        return self.db.execute(sql, params)

    def executemany(self, sql, seq):
        self.sqls.append(sql)
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
        with patch("data.cloud_db.configured", return_value=False):
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

    def test_second_push_skips_complete_older_dates(self):
        first = cloud_db.push_file(self.path, self.remote, since="2026-08-15")
        self.assertEqual(first["taiex_daily"], 2)
        second = cloud_db.push_file(self.path, self.remote, since="2026-08-15")
        # latest day is always re-upserted; 2026-08-20 is already complete
        self.assertEqual(second["taiex_daily"], 1)
        days = [r[0] for r in self.remote.execute(
            "SELECT trade_date FROM taiex_daily ORDER BY 1")]
        self.assertEqual(days, ["2026-08-20", "2026-08-29"])

    def test_incomplete_older_date_is_reuploaded(self):
        cloud_db.push_file(self.path, self.remote, since="2026-08-15")
        self.remote.execute("DELETE FROM taiex_daily WHERE trade_date='2026-08-20'")
        self.remote.commit()
        counts = cloud_db.push_file(self.path, self.remote, since="2026-08-15")
        self.assertEqual(counts["taiex_daily"], 2)
        days = [r[0] for r in self.remote.execute(
            "SELECT trade_date FROM taiex_daily ORDER BY 1")]
        self.assertEqual(days, ["2026-08-20", "2026-08-29"])

    def test_insert_uses_one_statement_per_chunk(self):
        conn = sqlite3.connect(self.path)
        conn.executemany(
            "INSERT INTO taiex_daily VALUES (?,?,?,?,?)",
            [(f"2026-08-{i:02d}", 1, 1, 1, 1) for i in range(2, 12)],
        )
        conn.commit()
        conn.close()
        remote = FakeRemote()
        cloud_db.push_file(self.path, remote, since="2026-08-01")
        inserts = [
            sql for sql in remote.sqls
            if sql.lstrip().upper().startswith("INSERT") and "taiex_daily" in sql
        ]
        self.assertEqual(len(inserts), 1)
        self.assertGreater(inserts[0].count("(?"), 1)

    def test_missing_file_returns_empty(self):
        self.assertEqual(
            cloud_db.push_file(Path(self.tmp.name) / "nope.db", self.remote),
            {},
        )

    def test_rejects_unsafe_table_name(self):
        with self.assertRaises(ValueError):
            cloud_db._ident("taiex;drop")

    def test_push_market_files_noop_without_config(self):
        with patch("data.cloud_db.configured", return_value=False):
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
        with patch("data.cloud_db.configured", return_value=False):
            counts = cloud_db.push_alert_files(files=(path,), remote=remote)
        self.assertEqual(counts["screener.db"]["alerts"], 1)

    def test_push_alerts_cli_ok_without_secrets(self):
        with patch("data.cloud_db.configured", return_value=False):
            self.assertEqual(cloud_db.main(["push-alerts"]), 0)

    def test_broker_branch_tables_use_existing_trade_date_window(self):
        """#61: broker_branch_daily has trade_date so push_market_files N-day window applies."""
        path = Path(self.tmp.name) / "twse_broker.db"
        conn = sqlite3.connect(path)
        from market import broker_branch
        broker_branch.ensure_schema(conn)
        conn.execute(
            "INSERT INTO broker_branch_daily VALUES ('2026-08-01','2330','1020',1,0,1)"
        )
        conn.execute(
            "INSERT INTO broker_branch_daily VALUES ('2026-09-03','2330','1020',5,1,4)"
        )
        conn.execute("INSERT INTO brokers VALUES ('1020','合庫')")
        conn.execute("INSERT INTO broker_branch_meta VALUES ('source','live')")
        conn.commit()
        conn.close()
        remote = FakeRemote()
        counts = cloud_db.push_file(path, remote, since="2026-08-20")
        self.assertEqual(counts["broker_branch_daily"], 1)
        self.assertEqual(counts["brokers"], 1)
        self.assertEqual(counts["broker_branch_meta"], 1)
        self.assertEqual(
            remote.execute("SELECT trade_date FROM broker_branch_daily").fetchone()[0],
            "2026-09-03",
        )

    def test_init_db_trust_dealer_are_discovered_by_trade_date(self):
        """Turso push auto-discovers tables with trade_date; new T86 tables qualify."""
        from market import collector
        orig = collector.DB_PATH
        collector.DB_PATH = self.path
        try:
            conn = collector.get_conn()
            conn.close()
        finally:
            collector.DB_PATH = orig
        local = sqlite3.connect(self.path)
        try:
            for table in ("trust_daily", "dealer_daily"):
                self.assertEqual(cloud_db._since_column(local, table), "trade_date")
            local.execute(
                "INSERT INTO trust_daily VALUES ('2026-09-04','2330','台積電',1,0,1)"
            )
            local.execute(
                "INSERT INTO dealer_daily VALUES ('2026-09-04','2330','台積電',2,1,1)"
            )
            local.commit()
        finally:
            local.close()
        remote = FakeRemote()
        counts = cloud_db.push_file(self.path, remote, since="2026-09-01")
        self.assertEqual(counts["trust_daily"], 1)
        self.assertEqual(counts["dealer_daily"], 1)

    def test_stock_chips_daily_view_is_pushed_not_copied_as_table(self):
        """#77: Turso gets the VIEW DDL; rows still come from base tables."""
        from market import collector
        orig = collector.DB_PATH
        collector.DB_PATH = self.path
        try:
            conn = collector.get_conn()
            conn.execute(
                "INSERT INTO stock_daily VALUES "
                "('2026-09-04','2330','台積電',1,1,1,1,100,1000)"
            )
            conn.execute(
                "INSERT INTO foreign_daily VALUES "
                "('2026-09-04','2330','台積電',9,3,6)"
            )
            conn.commit()
            conn.close()
        finally:
            collector.DB_PATH = orig
        remote = FakeRemote()
        counts = cloud_db.push_file(self.path, remote, since="2026-09-01")
        self.assertNotIn("stock_chips_daily", counts)
        self.assertEqual(counts["stock_daily"], 1)
        self.assertEqual(counts["foreign_daily"], 1)
        kind = remote.execute(
            "SELECT type FROM sqlite_master WHERE name='stock_chips_daily'"
        ).fetchone()
        self.assertEqual(kind[0], "view")
        row = remote.execute(
            "SELECT close, volume, turnover, foreign_net, trust_net, dealer_net "
            "FROM stock_chips_daily WHERE stock_id='2330'"
        ).fetchone()
        self.assertEqual(row, (1, 100, 1000, 6, None, None))
        # Definition updates: second push DROP+CREATE still queryable
        cloud_db.push_file(self.path, remote, since="2026-09-01")
        self.assertEqual(
            remote.execute(
                "SELECT foreign_net FROM stock_chips_daily WHERE stock_id='2330'"
            ).fetchone()[0],
            6,
        )
