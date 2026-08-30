import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import db
from performance_checker import main


class PerformanceCheckerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self.tmp.name) / "test.db")
        db.init_db()

    def tearDown(self):
        db.set_db_path(None)
        self.tmp.cleanup()

    def test_saves_return_for_old_unchecked_alert(self):
        old = str(date.today() - timedelta(days=40))
        alert_id = db.save_alert("2330", "upper_shadow_reversal", old, 100.0)
        with patch("performance_checker.fetch_latest_closes", return_value={"2330": 110.0}):
            main()
        pending = db.get_pending_alerts()
        self.assertEqual(pending, [])
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT return_pct, price_at_check FROM performance WHERE alert_id = ?",
                (alert_id,),
            ).fetchone()
        self.assertAlmostEqual(row["return_pct"], 10.0)
        self.assertEqual(row["price_at_check"], 110.0)

    def test_skips_when_price_missing(self):
        old = str(date.today() - timedelta(days=40))
        db.save_alert("2330", "upper_shadow_reversal", old, 100.0)
        with patch("performance_checker.fetch_latest_closes", return_value={}):
            main()
        self.assertEqual(len(db.get_pending_alerts()), 1)

    def test_skips_zero_alert_price(self):
        old = str(date.today() - timedelta(days=40))
        db.save_alert("2330", "upper_shadow_reversal", old, 0.0)
        with patch("performance_checker.fetch_latest_closes", return_value={"2330": 10.0}):
            main()
        self.assertEqual(len(db.get_pending_alerts()), 1)

    def test_duplicate_performance_is_ignored(self):
        old = str(date.today() - timedelta(days=40))
        alert_id = db.save_alert("2330", "inside_day", old, 50.0)
        self.assertTrue(db.save_performance(alert_id, str(date.today()), 55.0, 10.0))
        self.assertFalse(db.save_performance(alert_id, str(date.today()), 56.0, 12.0))
        with db.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM performance WHERE alert_id = ?",
                (alert_id,),
            ).fetchone()["n"]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
