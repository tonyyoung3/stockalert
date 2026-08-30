import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from alertsdb import store as db


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
        today = date.today()
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

    def test_default_db_path_stays_at_repo_root(self):
        db.set_db_path(None)
        self.assertEqual(
            db.get_db_path(),
            Path(__file__).resolve().parents[1] / "screener.db",
        )


if __name__ == "__main__":
    unittest.main()
