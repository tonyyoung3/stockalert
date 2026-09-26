"""Institutional gap fill uses T86 and keeps failed dates retryable."""
import sqlite3
import unittest
from datetime import date
from unittest.mock import Mock, patch

from market import backfill, collector


class InstitutionalT86Tests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        collector.init_db(self.conn)
        self.day = date(2026, 8, 31)
        self.conn.execute(
            "INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
            (self.day.isoformat(), "2330", "台積電", 100, 40, 60),
        )
        self.conn.commit()
        for target, kwargs in (
            ("market.backfill.get_conn", {"return_value": self.conn}),
            ("market.backfill.time.sleep", {}),
            ("data.cloud_db.configured", {"return_value": False}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_empty_or_failed_t86_leaves_gap_retryable(self):
        for failure in (None, ValueError("invalid JSON")):
            with self.subTest(failure=failure):
                fetch = Mock(return_value=collector.EMPTY_T86, side_effect=failure)
                with self.assertRaises(backfill.InstitutionalGapError) as error:
                    backfill.backfill_institutional_gaps(
                        today=self.day, t86_fetch=fetch,
                    )
                fetch.assert_called_once_with(self.day)
                self.assertEqual(error.exception.wrote, 0)
                self.assertEqual(error.exception.failed_dates, [self.day.isoformat()])
                self.assertEqual(
                    backfill.missing_institutional_dates(self.conn),
                    [self.day.isoformat()],
                )
                for table in ("trust_daily", "dealer_daily"):
                    self.assertEqual(self.conn.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0], 0)

    def test_t86_success_fills_gap_and_next_run_skips_it(self):
        ds = self.day.isoformat()
        tables = collector.T86Tables(
            [(ds, "2330", "台積電", 100, 40, 60)],
            [(ds, "2330", "台積電", 20, 10, 10)],
            [(ds, "2330", "台積電", 30, 10, 20)],
        )
        fetch = Mock(return_value=tables)
        self.assertEqual(backfill.backfill_institutional_gaps(
            today=self.day, t86_fetch=fetch,
        ), 1)
        self.assertEqual(backfill.backfill_institutional_gaps(
            today=self.day, t86_fetch=fetch,
        ), 0)
        fetch.assert_called_once_with(self.day)
        self.assertEqual(self.conn.execute(
            "SELECT trust_net FROM trust_daily"
        ).fetchone()[0], 10)
        self.assertEqual(self.conn.execute(
            "SELECT dealer_net FROM dealer_daily"
        ).fetchone()[0], 20)

    def test_fetch_reports_error_and_empty_separately(self):
        empty = backfill.fetch_institutional_day(
            self.day, t86_fetch=Mock(return_value=collector.EMPTY_T86),
        )
        failed = backfill.fetch_institutional_day(
            self.day, t86_fetch=Mock(side_effect=ValueError("HTML response")),
        )
        self.assertEqual((empty.source, empty.status), ("t86", "empty"))
        self.assertEqual((failed.source, failed.status), ("t86", "error"))
        self.assertEqual(failed.reason, "t86_error:ValueError")
