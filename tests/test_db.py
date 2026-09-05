import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from alertsdb import store as db
from web.tw_calendar import taiwan_today


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self.tmp.name) / "test.db")
        db.init_db()

    def tearDown(self):
        db.set_db_path(None)
        self.tmp.cleanup()

    def test_save_alert_returns_id(self):
        alert_id = db.save_alert("2330", "upper_shadow_reversal", "2026-07-01", 100.0)
        self.assertIsInstance(alert_id, int)
        self.assertTrue(db.has_alert("2330", "upper_shadow_reversal", "2026-07-01"))

    def test_duplicate_alert_is_ignored(self):
        first = db.save_alert("2330", "upper_shadow_reversal", "2026-07-01", 100.0)
        second = db.save_alert("2330", "upper_shadow_reversal", "2026-07-01", 101.0)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_same_ticker_different_day_is_allowed(self):
        self.assertIsNotNone(db.save_alert("2330", "inside_day", "2026-07-01", 100.0))
        self.assertIsNotNone(db.save_alert("2330", "inside_day", "2026-07-02", 101.0))

    def test_pending_alerts_are_all_old_unchecked(self):
        today = taiwan_today()
        old = str(today - timedelta(days=40))
        recent = str(today - timedelta(days=10))
        just_old_enough = str(today - timedelta(days=28))
        too_old_for_old_window = str(today - timedelta(days=60))

        old_id = db.save_alert("1101", "upper_shadow_reversal", old, 10.0)
        db.save_alert("1102", "upper_shadow_reversal", recent, 10.0)
        just_id = db.save_alert("1103", "inside_day", just_old_enough, 10.0)
        very_old_id = db.save_alert("1104", "upper_shadow_reversal", too_old_for_old_window, 10.0)

        pending_ids = {row["id"] for row in db.get_pending_alerts(min_age_days=28)}
        self.assertIn(old_id, pending_ids)
        self.assertIn(just_id, pending_ids)
        self.assertIn(very_old_id, pending_ids)
        self.assertEqual(len(pending_ids), 3)

        db.save_performance(old_id, str(today), 11.0, 10.0)
        pending_after = {row["id"] for row in db.get_pending_alerts(min_age_days=28)}
        self.assertNotIn(old_id, pending_after)
        self.assertIn(just_id, pending_after)

    def test_pending_horizon_jobs_are_per_horizon(self):
        today = date(2026, 8, 30)
        old = str(today - timedelta(days=80))
        mid = str(today - timedelta(days=20))
        fresh = str(today - timedelta(days=3))
        old_id = db.save_alert("2330", "upper_shadow_reversal", old, 100.0)
        mid_id = db.save_alert("2317", "upper_shadow_reversal", mid, 50.0)
        db.save_alert("1101", "inside_day", fresh, 10.0)

        jobs = db.get_pending_horizon_jobs(horizons=(5, 20, 60), today=today)
        pairs = {(row["id"], horizon) for row, horizon in jobs}
        self.assertIn((old_id, 5), pairs)
        self.assertIn((old_id, 20), pairs)
        self.assertIn((mid_id, 5), pairs)
        self.assertNotIn((mid_id, 20), pairs)
        self.assertNotIn((mid_id, 60), pairs)
        fresh_ids = {row["id"] for row, _ in jobs if row["ticker"] == "1101"}
        self.assertEqual(fresh_ids, set())

        self.assertTrue(db.save_performance(old_id, "2026-05-20", 110.0, 10.0, horizon_td=5))
        self.assertFalse(db.save_performance(old_id, "2026-05-21", 111.0, 11.0, horizon_td=5))
        after = {(row["id"], horizon) for row, horizon in db.get_pending_horizon_jobs(horizons=(5, 20), today=today)}
        self.assertNotIn((old_id, 5), after)
        self.assertIn((old_id, 20), after)

    def test_init_db_creates_scanner_alert_tables(self):
        names = {
            row[0]
            for row in db.get_conn().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("scanner_alert_profile", names)
        self.assertIn("scanner_alert_runs", names)

    def test_default_db_path_stays_at_repo_root(self):
        db.set_db_path(None)
        self.assertEqual(
            db.get_db_path(),
            Path(__file__).resolve().parents[1] / "screener.db",
        )

    def test_performance_by_horizon_empty(self):
        got = db.performance_by_horizon()
        self.assertTrue(got["empty"])
        self.assertIn("交易日", got["assumptions"])
        self.assertEqual([h["horizon_td"] for h in got["horizons"]], [5, 20, 60])
        for block in got["horizons"]:
            self.assertEqual(block["n"], 0)
            self.assertIsNone(block["win_rate_pct"])
            self.assertIsNone(block["avg_return_pct"])
            self.assertEqual(block["by_pattern"], [])

    def test_performance_by_horizon_splits_horizons_and_patterns(self):
        a = db.save_alert("2330", "upper_shadow_reversal", "2026-06-01", 100.0)
        b = db.save_alert("2317", "inside_day", "2026-06-02", 50.0)
        db.save_performance(a, "2026-06-09", 110.0, 10.0, horizon_td=5)
        db.save_performance(a, "2026-06-30", 98.0, -2.0, horizon_td=20)
        db.save_performance(b, "2026-06-10", 52.0, 4.0, horizon_td=5)

        mixed = db.performance_summary()
        self.assertEqual(mixed["checked"], 3)

        got = db.performance_by_horizon()
        self.assertFalse(got["empty"])
        by_h = {h["horizon_td"]: h for h in got["horizons"]}
        self.assertEqual(by_h[5]["n"], 2)
        self.assertEqual(by_h[5]["wins"], 2)
        self.assertEqual(by_h[5]["win_rate_pct"], 100.0)
        self.assertEqual(by_h[5]["avg_return_pct"], 7.0)
        self.assertEqual(by_h[20]["n"], 1)
        self.assertEqual(by_h[20]["wins"], 0)
        self.assertEqual(by_h[20]["win_rate_pct"], 0.0)
        self.assertEqual(by_h[20]["avg_return_pct"], -2.0)
        self.assertEqual(by_h[60]["n"], 0)
        self.assertIsNone(by_h[60]["win_rate_pct"])

        t5_pat = {p["pattern_type"]: p for p in by_h[5]["by_pattern"]}
        self.assertEqual(t5_pat["upper_shadow_reversal"]["n"], 1)
        self.assertEqual(t5_pat["inside_day"]["avg_return_pct"], 4.0)

    def test_list_alerts_and_performance_accept_tuple_rows(self):
        """Turso/libsql connections do not set sqlite3.Row."""
        import sqlite3

        db.save_alert("2330", "inside_day", "2026-07-01", 100.0)
        aid = db.save_alert("2317", "upper_shadow_reversal", "2026-07-02", 50.0)
        db.save_performance(aid, "2026-07-10", 55.0, 10.0, horizon_td=5)
        conn = sqlite3.connect(db.get_db_path())
        try:
            rows = db.list_alerts(since="2026-07-01", conn=conn)
            self.assertEqual(rows[0]["ticker"], "2317")
            got = db.performance_by_horizon(conn=conn)
            self.assertEqual(got["horizons"][0]["n"], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
