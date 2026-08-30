import unittest
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
import sqlite3
import tempfile
import shutil

import update_market_data as umd


class TaiwanTimeTests(unittest.TestCase):
    def test_include_today_after_close_in_taiwan(self):
        # 10:00 UTC = 18:00 Taiwan, the production cron
        now = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
        self.assertTrue(umd.include_today(now))

    def test_exclude_today_during_session(self):
        # 02:00 UTC = 10:00 Taiwan, still in session
        now = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)
        self.assertFalse(umd.include_today(now))

    def test_naive_datetime_treated_as_utc(self):
        now = datetime(2026, 8, 31, 10, 0)
        self.assertTrue(umd.include_today(now))


class ParseArgsTests(unittest.TestCase):
    def test_positional_days(self):
        args = umd.parse_args(["30"])
        self.assertEqual(args.days, 30)
        self.assertEqual(args.us_hourly_days, 30)

    def test_default_jobs_include_stocks_taifex_us(self):
        args = umd.parse_args(["--exclude-today"])
        self.assertEqual(
            umd.planned_jobs(args),
            ["ohlc", "index_foreign_margin", "stock_daily", "taifex", "us"],
        )

    def test_skip_flags(self):
        args = umd.parse_args(["--skip-us", "--skip-taifex", "--skip-stocks", "--exclude-today"])
        self.assertEqual(umd.planned_jobs(args), ["ohlc", "index_foreign_margin"])

    def test_legacy_stocks_flag_is_default_on(self):
        args = umd.parse_args(["14", "--stocks", "--exclude-today"])
        self.assertIn("stock_daily", umd.planned_jobs(args))

    def test_force_include_today(self):
        args = umd.parse_args(["--include-today"])
        self.assertTrue(args.include_today)

    def test_exclude_today_wins(self):
        args = umd.parse_args(["--include-today", "--exclude-today"])
        self.assertFalse(args.include_today)


class DryRunTests(unittest.TestCase):
    def test_dry_run_does_not_call_collectors(self):
        with patch.object(umd, "run_jobs") as run:
            code = umd.main(["--dry-run", "--exclude-today"])
        self.assertEqual(code, 0)
        run.assert_not_called()


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_dbs(self):
        lines = umd.coverage_lines(self.tmp / "twse_data.db", self.tmp / "us_data.db")
        self.assertTrue(any("missing" in line for line in lines))

    def test_span_from_sqlite(self):
        db = self.tmp / "twse_data.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE taiex_daily (trade_date TEXT, open REAL, high REAL, low REAL, close REAL)")
        conn.execute("INSERT INTO taiex_daily VALUES ('2026-08-01',1,1,1,1)")
        conn.execute("INSERT INTO taiex_daily VALUES ('2026-08-29',1,1,1,1)")
        conn.commit()
        conn.close()
        lines = umd.coverage_lines(db, self.tmp / "us_data.db")
        daily = [l for l in lines if l.startswith("taiex_daily")][0]
        self.assertIn("2026-08-01", daily)
        self.assertIn("2026-08-29", daily)


class RunJobsTests(unittest.TestCase):
    def test_records_failures_without_raising(self):
        args = umd.parse_args(["--exclude-today", "--skip-us", "--skip-taifex", "--skip-stocks"])
        with patch("backfill.backfill_ohlc", side_effect=RuntimeError("ohlc boom")), \
             patch("backfill.backfill", return_value=None), \
             patch("collector.sync_stock_master", return_value=None):
            failed = umd.run_jobs(args)
        self.assertEqual(failed, ["ohlc"])

    def test_passes_include_today_into_backfill(self):
        args = umd.parse_args(["--include-today", "--skip-us", "--skip-taifex", "--skip-stocks"])
        with patch("backfill.backfill_ohlc") as ohlc, \
             patch("backfill.backfill") as bf, \
             patch("collector.sync_stock_master"):
            umd.run_jobs(args)
        ohlc.assert_called_once()
        kwargs = bf.call_args.kwargs
        self.assertTrue(kwargs["include_today"])
        self.assertEqual(kwargs["today"], umd.taiwan_now().date())
