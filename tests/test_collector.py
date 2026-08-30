#!/usr/bin/env python3
"""台股資料收集器測試

執行:
    python -m unittest tests.test_collector -v

不需額外套件(內建 unittest)。所有測試都用假的 API 回應,
不會真的連線證交所,也不會動到 twse_data.db(每個測試用暫存檔)。

注意:假回應是依證交所文件手寫的。若證交所改版導致實際格式不同,
這些測試仍會通過但線上會失敗 —— 那時請更新本檔的 FAKE_* 常數。
"""
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

from market import backfill, collector


# ---------------------------------------------------------------- 假回應

FAKE_MARGIN = {"stat": "OK", "tables": [
    {"fields": ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"],
     "data": [["融資(交易單位)", "300,000", "310,000", "5,000", "5,000,000", "4,985,000"],
              ["融券(交易單位)", "5,000", "6,000", "100", "350,000", "349,900"],
              ["融資金額(仟元)", "30,000,000", "31,000,000", "500,000",
               "480,000,000", "478,500,000"]]},
    {"fields": ["股票代號", "股票名稱", "買進", "賣出", "現金償還", "前日餘額", "今日餘額",
                "限額", "買進", "賣出", "現券償還", "前日餘額", "今日餘額", "限額",
                "資券互抵", "註記"],
     "data": [["2330", "台積電", "1,500", "1,200", "50", "20,000", "20,250", "6,483,000",
               "10", "30", "5", "1,000", "975", "6,483,000", "3", ""]]}]}

FAKE_T86 = {"stat": "OK", "data": [
    ["2330 ", "台積電", "42,318,827", "28,538,728", "13,780,099", "0", "0", "0"],
    ["2317 ", "鴻海", "10,000,000", "12,000,000", "-2,000,000", "0", "0", "0"]]}

FAKE_TAIEX_MONTH = {"stat": "OK", "data": [
    ["115/07/01", "42,000.11", "42,500.22", "41,800.33", "42,300.44"],
    ["115/07/02", "42,300.44", "43,000.00", "42,100.00", "42,900.55"]]}

FAKE_MI_INDEX = {"stat": "OK", "tables": [
    {"fields": ["指數", "收盤指數"], "data": [["發行量加權股價指數", "43,119.75"]]},
    {"fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價",
                "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差"],
     "data": [["2330", "台積電", "42,318,827", "55,000", "61,362,300,000",
               "1,450.00", "1,465.00", "1,445.00", "1,460.00", "<p>+</p>", "10.00"],
              ["9999", "無成交股", "0", "0", "0", "--", "--", "--", "--", "<p> </p>", "0.00"]]}]}

FAKE_STOCK_DAY = {"stat": "OK", "title": "115年07月 2330 台積電 各日成交資訊",
                  "fields": ["日期", "成交股數", "成交金額", "開盤價", "最高價",
                             "最低價", "收盤價", "漲跌價差", "成交筆數"],
                  "data": [["115/07/01", "33,970,903", "49,000,000,000", "1,440.00",
                            "1,455.00", "1,435.00", "1,450.00", "+10.00", "40,000"]]}


def mock_get(payload):
    """把 requests.get 換成回傳指定 JSON 的 mock。"""
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return patch.object(collector.requests, "get", return_value=resp)


class DBTestCase(unittest.TestCase):
    """每個測試用獨立的暫存資料庫。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "test.db"
        self._orig = collector.DB_PATH
        collector.DB_PATH = self.db

    def tearDown(self):
        collector.DB_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self, sql, params=()):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()


# ---------------------------------------------------------------- 解析

class TestParseForeign(unittest.TestCase):
    def test_parses_and_strips(self):
        with mock_get(FAKE_T86):
            rows = collector.fetch_foreign(date(2026, 7, 31))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ("2026-07-31", "2330", "台積電",
                                   42318827, 28538728, 13780099))

    def test_negative_and_commas(self):
        with mock_get(FAKE_T86):
            rows = collector.fetch_foreign(date(2026, 7, 31))
        self.assertEqual(rows[1][5], -2000000)

    def test_non_trading_day_returns_empty(self):
        with mock_get({"stat": "很抱歉，沒有符合條件的資料!"}):
            self.assertEqual(collector.fetch_foreign(date(2026, 7, 31)), [])


class TestParseTaiexOHLC(unittest.TestCase):
    def test_roc_year_conversion(self):
        with mock_get(FAKE_TAIEX_MONTH):
            rows = collector.fetch_taiex_ohlc_month(date(2026, 7, 1))
        self.assertEqual(rows[0], ("2026-07-01", 42000.11, 42500.22, 41800.33, 42300.44))
        self.assertEqual(rows[1][0], "2026-07-02")

    def test_empty_on_bad_stat(self):
        with mock_get({"stat": "no data"}):
            self.assertEqual(collector.fetch_taiex_ohlc_month(date(2026, 7, 1)), [])


class TestHourlyOHLC(unittest.TestCase):
    """每 5 秒資料 → 小時 K 的彙整邏輯。"""

    def raw(self):
        out, t, v = [], 9 * 3600, 22000.0
        while t <= 13 * 3600 + 33 * 60:
            out.append((f"{t//3600:02d}:{t%3600//60:02d}:{t%60:02d}", v))
            v += 0.5
            t += 5
        return out

    def test_five_buckets(self):
        rows = collector.hourly_ohlc_from_5sec(date(2026, 7, 31), self.raw())
        self.assertEqual(len(rows), 5, "09,10,11,12,13 共五根")
        self.assertTrue(rows[0][0].endswith("T09:00:00"))
        self.assertTrue(rows[-1][0].endswith("T13:00:00"))

    def test_ohlc_relations(self):
        for ts, d, o, h, l, c in collector.hourly_ohlc_from_5sec(date(2026, 7, 31), self.raw()):
            self.assertLessEqual(l, o, f"{ts}: low <= open")
            self.assertLessEqual(l, c, f"{ts}: low <= close")
            self.assertGreaterEqual(h, o, f"{ts}: high >= open")
            self.assertGreaterEqual(h, c, f"{ts}: high >= close")

    def test_close_bar_absorbs_1330(self):
        """13:30 收盤資料要併入 13:00 那根,不另開一根。"""
        rows = collector.hourly_ohlc_from_5sec(date(2026, 7, 31), self.raw())
        last_close = self.raw()[-1][1]
        self.assertEqual(rows[-1][5], last_close)

    def test_empty_input(self):
        self.assertEqual(collector.hourly_ohlc_from_5sec(date(2026, 7, 31), []), [])


class TestParseMargin(unittest.TestCase):
    def test_totals_and_stocks(self):
        with mock_get(FAKE_MARGIN):
            totals, stocks = collector.fetch_margin(date(2026, 7, 31))
        self.assertIn(("2026-07-31", "融資金額(仟元)", 30000000, 31000000,
                       500000, 480000000, 478500000), totals)
        # 個股:融資買/賣/餘額 + 融券買/賣/餘額
        self.assertEqual(stocks[0], ("2026-07-31", "2330", "台積電",
                                     1500, 1200, 20250, 10, 30, 975))


class TestParseStockDaily(unittest.TestCase):
    def test_mi_index_all_market(self):
        with mock_get(FAKE_MI_INDEX):
            rows = collector.fetch_stock_day_all(date(2026, 7, 31))
        self.assertEqual(rows[0], ("2026-07-31", "2330", "台積電",
                                   1450.0, 1465.0, 1445.0, 1460.0,
                                   42318827, 61362300000))

    def test_dash_becomes_none(self):
        """無成交個股的價格欄是 '--',要轉成 None 而非 0。"""
        with mock_get(FAKE_MI_INDEX):
            rows = collector.fetch_stock_day_all(date(2026, 7, 31))
        self.assertIsNone(rows[1][3])
        self.assertEqual(rows[1][7], 0)

    def test_stock_day_single(self):
        with mock_get(FAKE_STOCK_DAY):
            rows = collector.fetch_stock_month("2330", date(2026, 7, 1))
        self.assertEqual(rows[0], ("2026-07-01", "2330", "台積電",
                                   1440.0, 1455.0, 1435.0, 1450.0,
                                   33970903, 49000000000))


# ---------------------------------------------------------------- 寫入

class TestPersistence(DBTestCase):
    def test_schema_created(self):
        collector.get_conn().close()
        names = {r[0] for r in self.rows(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"taiex_hourly", "taiex_daily", "taiex_hourly_ohlc",
                         "foreign_daily", "margin_total", "margin_stock",
                         "stock_daily", "stocks"} <= names)

    def test_foreign_idempotent(self):
        """同一天寫兩次不應該產生重複列。"""
        with mock_get(FAKE_T86):
            collector.save_foreign(date(2026, 7, 31))
            collector.save_foreign(date(2026, 7, 31))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM foreign_daily")[0][0], 2)

    def test_margin_idempotent(self):
        with mock_get(FAKE_MARGIN):
            collector.save_margin(date(2026, 7, 31))
            collector.save_margin(date(2026, 7, 31))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM margin_stock")[0][0], 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM margin_total")[0][0], 3)

    def test_taiex_hourly_aligned_to_hour(self):
        """即時抓取的 ts 必須對齊整點,否則會與 backfill 的資料重複。"""
        with patch.object(collector, "fetch_taiex", return_value={
                "index_value": 43119.75, "change": 3186.45,
                "volume_100m": 4321.0, "trade_date": "20260731"}):
            collector.save_taiex()
        ts = self.rows("SELECT ts FROM taiex_hourly")[0][0]
        self.assertTrue(ts.endswith(":00:00"), f"ts 未對齊整點: {ts}")

    def test_save_foreign_walks_back(self):
        """非交易日應往前找,最多 7 天。"""
        calls = []

        def fake(day):
            calls.append(day)
            return FAKE_T86["data"] and (
                [] if len(calls) < 3 else
                [(day.isoformat(), "2330", "台積電", 1, 2, -1)])

        with patch.object(collector, "fetch_foreign", side_effect=fake), \
             patch.object(collector.time, "sleep"):
            collector.save_foreign(date(2026, 8, 1))
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1], date(2026, 7, 30))


class TestStockMaster(DBTestCase):
    def test_sync_from_existing_tables(self):
        with mock_get(FAKE_T86):
            collector.save_foreign(date(2026, 7, 31))
        collector.sync_stock_master()
        got = dict(self.rows("SELECT stock_id, stock_name FROM stocks"))
        self.assertEqual(got["2330"], "台積電")
        self.assertEqual(got["2317"], "鴻海")

    def test_name_updated_only_by_newer_data(self):
        """改名時取較新的名稱;舊資料不該覆蓋新名稱。"""
        con = collector.get_conn()
        with con:
            collector.update_stock_master(con, [("1234", "舊名")], "2026-01-01")
            collector.update_stock_master(con, [("1234", "新名")], "2026-07-31")
            collector.update_stock_master(con, [("1234", "更舊的名")], "2025-01-01")
        con.close()
        self.assertEqual(self.rows("SELECT stock_name FROM stocks")[0][0], "新名")


# ---------------------------------------------------------------- backfill

class TestBackfill(DBTestCase):
    def setUp(self):
        super().setUp()
        self._sleep = backfill.SLEEP
        backfill.SLEEP = 0

    def tearDown(self):
        backfill.SLEEP = self._sleep
        super().tearDown()

    def test_ohlc_iterates_months(self):
        seen = []

        def fake(month):
            seen.append((month.year, month.month))
            return [("2026-07-01", 1.0, 2.0, 0.5, 1.5)]

        with patch.object(backfill, "fetch_taiex_ohlc_month", side_effect=fake):
            backfill.backfill_ohlc(90)
        self.assertGreaterEqual(len(seen), 3)
        self.assertEqual(len(seen), len(set(seen)), "同一月份不應重複請求")

    def test_skips_weekends(self):
        seen = []

        def fake(day):
            seen.append(day)
            return []

        with patch.object(backfill, "fetch_index_5sec", side_effect=fake), \
             patch.object(backfill, "fetch_foreign", return_value=[]), \
             patch.object(backfill, "fetch_margin", return_value=([], [])):
            backfill.backfill(14)
        self.assertTrue(all(d.weekday() < 5 for d in seen), "不該請求週末")

    def test_default_excludes_today(self):
        seen = []

        def fake(day):
            seen.append(day)
            return []

        today = date(2026, 8, 31)  # Monday
        with patch.object(backfill, "fetch_index_5sec", side_effect=fake), \
             patch.object(backfill, "fetch_foreign", return_value=[]), \
             patch.object(backfill, "fetch_margin", return_value=([], [])):
            backfill.backfill(3, today=today, include_today=False)
        self.assertNotIn(today, seen)

    def test_include_today_requests_today(self):
        seen = []

        def fake(day):
            seen.append(day)
            return []

        today = date(2026, 8, 31)  # Monday
        with patch.object(backfill, "fetch_index_5sec", side_effect=fake), \
             patch.object(backfill, "fetch_foreign", return_value=[]), \
             patch.object(backfill, "fetch_margin", return_value=([], [])):
            backfill.backfill(3, today=today, include_today=True)
        self.assertIn(today, seen)

    def test_stock_daily_uses_market_wide_endpoint(self):
        requested = []

        def fake(day):
            requested.append(day)
            return [(day.isoformat(), "2330", "台積電", 1., 1., 1., 1., 1, 1)]

        today = date(2026, 8, 31)
        with patch.object(backfill, "fetch_stock_day_all", side_effect=fake):
            n = backfill.backfill_stock_daily(3, today=today, include_today=True)
        self.assertGreater(n, 0)
        self.assertTrue(all(d.weekday() < 5 for d in requested))
        self.assertIn(today, requested)
        rows = self.rows("SELECT COUNT(*) FROM stock_daily")
        self.assertGreater(rows[0][0], 0)

    def test_stocks_resume_skips_completed(self):
        """已涵蓋回補區間的股票要跳過(中斷續跑)。"""
        today = date.today()
        con = collector.get_conn()
        with con:
            for d in (today - timedelta(days=700), today - timedelta(days=5)):
                con.execute("INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
                            (d.isoformat(), "2330", "台積電", 1., 1., 1., 1., 1, 1))
            con.execute("INSERT OR REPLACE INTO stocks VALUES ('2330','台積電',?)",
                        (today.isoformat(),))
            con.execute("INSERT OR REPLACE INTO stocks VALUES ('2454','聯發科',?)",
                        (today.isoformat(),))
        con.close()

        requested = []

        def fake(sid, month):
            requested.append(sid)
            return []

        with patch.object(backfill, "fetch_stock_month", side_effect=fake), \
             patch("builtins.input", return_value="y"):
            backfill.backfill_stocks(None, 730)
        self.assertNotIn("2330", requested, "已完成的股票應被跳過")
        self.assertIn("2454", requested)

    def test_stocks_aborts_without_confirmation(self):
        con = collector.get_conn()
        with con:
            con.execute("INSERT INTO stocks VALUES ('2330','台積電','2026-07-31')")
        con.close()
        with patch("builtins.input", return_value="n"), \
             patch.object(backfill, "fetch_stock_month") as mf:
            with self.assertRaises(SystemExit):
                backfill.backfill_stocks(None, 730)
            mf.assert_not_called()


# ---------------------------------------------------------------- 儀表板

class TestDashboardAPI(DBTestCase):
    def setUp(self):
        super().setUp()
        with mock_get(FAKE_T86):
            collector.save_foreign(date(2026, 7, 31))
        with mock_get(FAKE_MARGIN):
            collector.save_margin(date(2026, 7, 31))
        con = collector.get_conn()
        with con:
            con.execute("INSERT INTO taiex_hourly VALUES "
                        "('2026-07-31T13:00:00','2026-07-31',43119.75,120.5,4321.0)")
            con.execute("INSERT INTO taiex_daily VALUES "
                        "('2026-07-31',42000.0,43200.0,41900.0,43119.75)")
            con.execute("INSERT INTO taiex_hourly_ohlc VALUES "
                        "('2026-07-31T13:00:00','2026-07-31',43000.,43200.,42900.,43119.75)")
        con.close()
        from data import market_db
        from web import dashboard
        self.dash = dashboard
        market_db.set_db_path(self.db)

    def tearDown(self):
        from data import market_db
        market_db.set_db_path(None)
        super().tearDown()

    def call(self, path, **qs):
        return self.dash.api(path, {k: [str(v)] for k, v in qs.items()})

    def test_summary(self):
        r = self.call("/api/summary")
        self.assertEqual(r["latest_date"], "2026-07-31")
        self.assertEqual(r["stock_count"], 2)

    def test_top_buy_sell_ordering(self):
        r = self.call("/api/top", days=90)
        self.assertEqual(r["buy"][0][0], "2330")
        self.assertEqual(r["sell"][0][0], "2317")

    def test_ohlc_day_vs_hour(self):
        day = self.call("/api/ohlc", days=90, interval="day")["data"]
        hour = self.call("/api/ohlc", days=90, interval="hour")["data"]
        self.assertEqual(day[0][0], "2026-07-31")
        self.assertEqual(hour[0][0], "2026-07-31T13:00:00")

    def test_stock_and_margin_lookup(self):
        self.assertEqual(self.call("/api/stock", id="2330", days=90)["data"][0][4], 13780099)
        self.assertEqual(self.call("/api/stock_margin", id="2330", days=90)["data"][0][1], 20250)

    def test_unknown_path_returns_none(self):
        self.assertIsNone(self.call("/api/nope"))

    def test_all_endpoints_json_serialisable(self):
        for p, kw in [("/api/summary", {}), ("/api/taiex", {"days": 90}),
                      ("/api/ohlc", {"days": 90}), ("/api/foreign_total", {"days": 90}),
                      ("/api/margin_total", {"days": 90}), ("/api/top", {}),
                      ("/api/stock", {"id": "2330"}), ("/api/stock_margin", {"id": "2330"})]:
            with self.subTest(path=p):
                json.dumps(self.call(p, **kw))


if __name__ == "__main__":
    unittest.main(verbosity=2)
