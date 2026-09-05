"""Dashboard data freshness: calendar, empty/stale APIs, /health JSON."""
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from alertsdb import store as alerts_db
from data import market_db
from web import dashboard, freshness
from web.tw_calendar import holiday_name, is_tw_holiday, is_tw_trading_day

TW = timezone(timedelta(hours=8))
# Friday 2026-09-04
FRIDAY_AFTER_CLOSE = datetime(2026, 9, 4, 18, 0, tzinfo=TW)
FRIDAY_MORNING = datetime(2026, 9, 4, 10, 0, tzinfo=TW)
SATURDAY = datetime(2026, 9, 5, 12, 0, tzinfo=TW)
MONDAY_MORNING = datetime(2026, 9, 7, 10, 0, tzinfo=TW)
# Day after 2026-01-01 (Thu national holiday), before the 16:00 cutoff
DAY_AFTER_NY_MORNING = datetime(2026, 1, 2, 10, 0, tzinfo=TW)
# First weekday after 2026 Lunar New Year, before open/close
CNY_REOPEN_MORNING = datetime(2026, 2, 23, 10, 0, tzinfo=TW)
CNY_REOPEN_AFTER_CLOSE = datetime(2026, 2, 23, 18, 0, tzinfo=TW)


class CalendarTests(unittest.TestCase):
    def test_weekend_is_not_a_trading_day(self):
        self.assertFalse(is_tw_trading_day(date(2026, 9, 5)))
        self.assertFalse(is_tw_trading_day(date(2026, 9, 6)))
        self.assertTrue(is_tw_trading_day(date(2026, 9, 4)))

    def test_known_holiday_is_not_a_trading_day(self):
        self.assertTrue(is_tw_holiday(date(2026, 1, 1)))
        self.assertFalse(is_tw_trading_day(date(2026, 1, 1)))
        self.assertIn("開國", holiday_name(date(2026, 1, 1)))
        self.assertFalse(is_tw_trading_day(date(2026, 2, 20)))
        self.assertFalse(is_tw_trading_day(date(2025, 10, 10)))

    def test_previous_weekday_skips_weekend(self):
        self.assertEqual(freshness.previous_tw_trading_day(date(2026, 9, 7)), date(2026, 9, 4))
        self.assertEqual(freshness.previous_tw_trading_day(date(2026, 9, 5)), date(2026, 9, 4))
        self.assertEqual(freshness.previous_tw_trading_day(date(2026, 9, 4)), date(2026, 9, 3))

    def test_previous_skips_national_holiday(self):
        self.assertEqual(freshness.previous_tw_trading_day(date(2026, 1, 2)), date(2025, 12, 31))
        self.assertEqual(freshness.previous_tw_trading_day(date(2026, 2, 23)), date(2026, 2, 11))

    def test_expected_after_16_on_weekday_is_today(self):
        self.assertEqual(freshness.expected_tw_trade_date(FRIDAY_AFTER_CLOSE), date(2026, 9, 4))

    def test_expected_before_16_on_weekday_is_previous(self):
        self.assertEqual(freshness.expected_tw_trade_date(FRIDAY_MORNING), date(2026, 9, 3))

    def test_expected_weekend_is_friday(self):
        self.assertEqual(freshness.expected_tw_trade_date(SATURDAY), date(2026, 9, 4))

    def test_broker_branch_cutoff_is_21_not_16(self):
        friday_20 = datetime(2026, 9, 4, 20, 0, tzinfo=TW)
        friday_21 = datetime(2026, 9, 4, 21, 0, tzinfo=TW)
        self.assertEqual(
            freshness.expected_tw_trade_date(friday_20, after_hour=21),
            date(2026, 9, 3),
        )
        self.assertEqual(
            freshness.expected_tw_trade_date(friday_21, after_hour=21),
            date(2026, 9, 4),
        )
        self.assertEqual(freshness.expected_tw_trade_date(friday_20), date(2026, 9, 4))

    def test_monday_morning_expects_friday(self):
        self.assertEqual(freshness.expected_tw_trade_date(MONDAY_MORNING), date(2026, 9, 4))

    def test_day_after_holiday_before_open_expects_prior_session(self):
        """#47: weekday after a national holiday must not look like a missing trade day."""
        self.assertEqual(freshness.expected_tw_trade_date(DAY_AFTER_NY_MORNING), date(2025, 12, 31))
        self.assertEqual(freshness.expected_tw_trade_date(CNY_REOPEN_MORNING), date(2026, 2, 11))

    def test_first_session_after_holiday_becomes_expected_after_close(self):
        self.assertEqual(freshness.expected_tw_trade_date(CNY_REOPEN_AFTER_CLOSE), date(2026, 2, 23))

    def test_table_status_empty_is_stale(self):
        row = freshness.table_status("foreign_daily", None, date(2026, 9, 4), date(2026, 9, 4))
        self.assertTrue(row["empty"])
        self.assertTrue(row["stale"])
        self.assertIsNone(row["last_date"])
        self.assertIsNone(row["days_ago"])

    def test_table_status_fresh_and_stale(self):
        today, expected = date(2026, 9, 4), date(2026, 9, 4)
        fresh = freshness.table_status("stock_daily", "2026-09-04", today, expected)
        self.assertFalse(fresh["stale"])
        self.assertEqual(fresh["days_ago"], 0)
        stale = freshness.table_status("stock_daily", "2026-09-02", today, expected)
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["days_ago"], 2)
        self.assertFalse(stale["empty"])


class FreshnessAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.market = root / "twse.db"
        self.screener = root / "screener.db"
        self._init_market()
        sqlite3.connect(self.screener).close()
        alerts_db.set_db_path(self.screener)
        market_db.set_db_path(self.market)

    def tearDown(self):
        alerts_db.set_db_path(None)
        market_db.set_db_path(None)
        self.tmp.cleanup()

    def call(self, path, **qs):
        return dashboard.api(path, {k: [str(v)] for k, v in qs.items()})

    def _init_market(self):
        conn = sqlite3.connect(self.market)
        with conn:
            conn.executescript("""
                CREATE TABLE foreign_daily (
                    trade_date TEXT, stock_id TEXT, stock_name TEXT,
                    foreign_buy INTEGER, foreign_sell INTEGER, foreign_net INTEGER,
                    PRIMARY KEY (trade_date, stock_id));
                CREATE TABLE stock_daily (
                    trade_date TEXT, stock_id TEXT,
                    PRIMARY KEY (trade_date, stock_id));
                CREATE TABLE taifex_fut_oi (
                    trade_date TEXT, product TEXT, investor TEXT,
                    PRIMARY KEY (trade_date, product, investor));
                CREATE TABLE taiex_hourly (
                    ts TEXT PRIMARY KEY, trade_date TEXT,
                    index_value REAL, change REAL, volume_100m REAL);
            """)
        conn.close()

    def _insert(self, last_date, alerts=True):
        conn = sqlite3.connect(self.market)
        with conn:
            conn.execute(
                "INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                (last_date, "2330", "台積電", 1, 0, 1),
            )
            conn.execute(
                "INSERT INTO stock_daily VALUES (?,?)", (last_date, "2330"),
            )
            conn.execute(
                "INSERT INTO taifex_fut_oi VALUES (?,?,?)",
                (last_date, "臺股期貨", "外資及陸資"),
            )
        conn.close()
        if alerts:
            alerts_db.init_db()
            alerts_db.save_alert("2330", "inside_day", last_date, 100.0)

    def test_html_has_banner_kpis_and_empty_copy(self):
        html = dashboard.HTML
        self.assertIn("fresh-banner", html)
        self.assertIn("fk-foreign_daily", html)
        self.assertIn("fk-stock_daily", html)
        self.assertIn("fk-taifex", html)
        self.assertIn("fk-alerts", html)
        self.assertIn("請跑 python -m market.update_market_data", html)
        self.assertIn("setChartEmpty", html)
        self.assertIn("chart-empty", html)
        self.assertIn("renderFreshness", html)
        self.assertIn("s.freshness", html)
        self.assertIn("過期（早於上一個台股交易日）", html)
        self.assertIn("週末與國定假日不計過期", html)

    def test_empty_tables_are_stale_and_empty(self):
        with patch("web.freshness.taiwan_now", return_value=FRIDAY_AFTER_CLOSE):
            r = self.call("/api/freshness")
        self.assertTrue(r["empty"])
        self.assertTrue(r["stale"])
        self.assertEqual(r["calendar"], "tw_trading_days")
        self.assertIn("國定假日", r["calendar_note"])
        self.assertIn("2025", r["calendar_note"])
        names = [t["table"] for t in r["tables"]]
        self.assertEqual(names, ["foreign_daily", "stock_daily", "taifex", "alerts"])
        for t in r["tables"]:
            self.assertTrue(t["empty"])
            self.assertTrue(t["stale"])
            self.assertIsNone(t["last_date"])
        json.dumps(r)

    def test_fresh_dates_after_close(self):
        self._insert("2026-09-04")
        with patch("web.freshness.taiwan_now", return_value=FRIDAY_AFTER_CLOSE):
            r = self.call("/api/freshness")
        self.assertFalse(r["empty"])
        self.assertFalse(r["stale"])
        by_name = {t["table"]: t for t in r["tables"]}
        self.assertEqual(by_name["foreign_daily"]["last_date"], "2026-09-04")
        self.assertEqual(by_name["foreign_daily"]["days_ago"], 0)
        self.assertEqual(by_name["stock_daily"]["last_date"], "2026-09-04")
        self.assertEqual(by_name["taifex"]["last_date"], "2026-09-04")
        self.assertEqual(by_name["alerts"]["last_date"], "2026-09-04")
        self.assertEqual(r["expected_trade_date"], "2026-09-04")

    def test_stale_when_earlier_than_previous_trading_day(self):
        self._insert("2026-09-02")
        with patch("web.freshness.taiwan_now", return_value=FRIDAY_AFTER_CLOSE):
            r = self.call("/api/freshness")
        self.assertFalse(r["empty"])
        self.assertTrue(r["stale"])
        by_name = {t["table"]: t for t in r["tables"]}
        self.assertEqual(by_name["foreign_daily"]["last_date"], "2026-09-02")
        self.assertEqual(by_name["foreign_daily"]["days_ago"], 2)
        self.assertTrue(by_name["foreign_daily"]["stale"])
        self.assertTrue(by_name["alerts"]["stale"])

    def test_thursday_data_is_fresh_friday_morning(self):
        self._insert("2026-09-03")
        with patch("web.freshness.taiwan_now", return_value=FRIDAY_MORNING):
            r = self.call("/api/freshness")
        self.assertFalse(r["stale"])
        self.assertEqual(r["expected_trade_date"], "2026-09-03")

    def test_holiday_reopen_morning_does_not_stale_on_last_session(self):
        """Last pre-holiday session is fresh on the first weekday back, before 16:00."""
        self._insert("2026-02-11")
        with patch("web.freshness.taiwan_now", return_value=CNY_REOPEN_MORNING):
            r = self.call("/api/freshness")
        self.assertEqual(r["expected_trade_date"], "2026-02-11")
        self.assertFalse(r["stale"])
        self.assertFalse(r["empty"])

    def test_new_year_morning_does_not_expect_holiday_weekday(self):
        self._insert("2025-12-31")
        with patch("web.freshness.taiwan_now", return_value=DAY_AFTER_NY_MORNING):
            r = self.call("/api/freshness")
        self.assertEqual(r["expected_trade_date"], "2025-12-31")
        self.assertFalse(r["stale"])

    def test_thursday_data_is_stale_friday_after_close(self):
        self._insert("2026-09-03")
        with patch("web.freshness.taiwan_now", return_value=FRIDAY_AFTER_CLOSE):
            r = self.call("/api/freshness")
        self.assertTrue(r["stale"])
        self.assertEqual(r["expected_trade_date"], "2026-09-04")

    def test_summary_includes_freshness(self):
        self._insert("2026-09-04")
        with patch("web.freshness.taiwan_now", return_value=FRIDAY_AFTER_CLOSE):
            s = self.call("/api/summary")
        self.assertEqual(s["latest_date"], "2026-09-04")
        self.assertIn("freshness", s)
        self.assertFalse(s["freshness"]["stale"])

    def test_missing_tables_count_as_empty(self):
        empty = Path(self.tmp.name) / "blank.db"
        sqlite3.connect(empty).close()
        market_db.set_db_path(empty)
        with patch("web.freshness.taiwan_now", return_value=FRIDAY_AFTER_CLOSE):
            r = self.call("/api/freshness")
        self.assertTrue(r["empty"])
        self.assertTrue(r["stale"])
        self.assertEqual(len(r["tables"]), 4)

    def test_health_payload_ok_when_stale(self):
        self._insert("2026-08-01")
        with patch("web.freshness.taiwan_now", return_value=FRIDAY_AFTER_CLOSE):
            h = dashboard.health_payload()
        self.assertEqual(h["status"], "ok")
        self.assertTrue(h["ok"])
        self.assertTrue(h["freshness"]["stale"])
        self.assertIn("503", h["note"])
        json.dumps(h)

    def test_health_http_200_json_when_stale(self):
        self._insert("2026-08-01")
        httpd = dashboard.make_server("127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            with patch("web.freshness.taiwan_now", return_value=FRIDAY_AFTER_CLOSE):
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    ctype = resp.headers.get("Content-Type", "")
                    self.assertIn("application/json", ctype)
                    body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(body["status"], "ok")
            self.assertTrue(body["freshness"]["stale"])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_freshness_opens_one_market_connection(self):
        self._insert("2026-09-04")
        calls = {"n": 0}
        real = market_db.connect

        def wrapped(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        with patch.object(market_db, "connect", wrapped):
            with patch("web.freshness.taiwan_now", return_value=FRIDAY_AFTER_CLOSE):
                r = self.call("/api/freshness")
        self.assertEqual(r["tables"][0]["last_date"], "2026-09-04")
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
