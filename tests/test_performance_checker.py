import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import db
from performance_checker import run_checks


def _bars(n: int = 80, start="2026-04-01", start_px: float = 100.0) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=n)
    closes = [start_px + i for i in range(n)]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [1_000.0] * n,
        },
        index=idx,
    )


class PerformanceCheckerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self.tmp.name) / "test.db")
        db.init_db()

    def tearDown(self):
        db.set_db_path(None)
        self.tmp.cleanup()

    def test_run_checks_saves_horizon_exit(self):
        today = date(2026, 8, 30)
        alert_day = date(2026, 4, 1)
        alert_id = db.save_alert("2330", "upper_shadow_reversal", str(alert_day), 100.0)
        frame = _bars()
        with patch("performance_checker.download_history", return_value={"2330": frame}):
            results = run_checks(horizons=(5,), today=today)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["horizon_td"], 5)
        self.assertAlmostEqual(results[0]["return_pct"], 5.0)
        pending = db.get_pending_horizon_jobs(horizons=(5,), today=today)
        self.assertFalse(any(row["id"] == alert_id for row, _ in pending))

    def test_skips_when_history_missing(self):
        old = str(date.today() - timedelta(days=40))
        db.save_alert("2330", "upper_shadow_reversal", old, 100.0)
        with patch("performance_checker.download_history", return_value={}):
            results = run_checks(horizons=(5,), today=date.today())
        self.assertEqual(results, [])
        self.assertEqual(len(db.get_pending_horizon_jobs(horizons=(5,), today=date.today())), 1)
