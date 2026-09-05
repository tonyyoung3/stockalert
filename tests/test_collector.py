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

FAKE_T86_FIELDS = [
    "證券代號", "證券名稱",
    "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)",
    "外陸資買賣超股數(不含外資自營商)",
    "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
    "投信買進股數", "投信賣出股數", "投信買賣超股數",
    "自營商買賣超股數",
    "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)",
    "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)",
    "三大法人買賣超股數",
]

# 19-col T86 layout confirmed 2026-09-04. 2330 foreign cols 2–4 stay the same
# so existing dashboard / foreign tests keep their expected nets.
FAKE_T86 = {"stat": "OK", "fields": FAKE_T86_FIELDS, "data": [
    ["2330 ", "台積電",
     "42,318,827", "28,538,728", "13,780,099",
     "0", "0", "0",
     "1,000,000", "200,000", "800,000",
     "406,740",
     "176,100", "40,900", "135,200",
     "341,070", "69,530", "271,540",
     "14,986,839"],
    ["2317 ", "鴻海",
     "10,000,000", "12,000,000", "-2,000,000",
     "0", "0", "0",
     "0", "0", "0",
     "0",
     "0", "0", "0",
     "0", "0", "0",
     "-2,000,000"]]}

# Live T86 2026-09-04 excerpt (chip-event-study sample check).
FAKE_T86_20260904 = {"stat": "OK", "fields": FAKE_T86_FIELDS, "data": [
    ["2330", "台積電          ",
     "10,735,399", "8,850,314", "1,885,085",
     "0", "0", "0",
     "79,000", "401,024", "-322,024",
     "406,740",
     "176,100", "40,900", "135,200",
     "341,070", "69,530", "271,540",
     "1,969,801"],
    ["2454", "聯發科          ",
     "4,917,028", "4,382,524", "534,504",
     "0", "0", "0",
     "357,561", "18,200", "339,361",
     "103,292",
     "113,296", "36,048", "77,248",
     "166,303", "140,259", "26,044",
     "977,157"],
]}

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

FAKE_TPEX_QUOTES = {"tables": [{
    "title": "上櫃股票每日收盤行情(不含定價)",
    "fields": ["代號", "名稱", "收盤 ", "漲跌", "開盤 ", "最高 ", "最低",
               "成交股數  ", " 成交金額(元)"],
    "data": [["6488", "環球晶", "500.00", "+10.00", "490.00", "505.00", "488.00",
              "1,234,000", "600,000,000"]],
}]}

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

    def test_short_row_still_parses_foreign(self):
        """Old 8-col fixtures / truncated rows must not break foreign_daily."""
        tables = collector.parse_t86(
            {"stat": "OK", "data": [
                ["2330 ", "台積電", "1,000", "400", "600", "0", "0", "0"],
            ]},
            date(2026, 7, 31),
        )
        self.assertEqual(tables.foreign[0], ("2026-07-31", "2330", "台積電", 1000, 400, 600))
        self.assertEqual(tables.trust[0][3:], (0, 0, 0))
        self.assertEqual(tables.dealer[0][3:], (0, 0, 0))


class TestParseT86Institutional(unittest.TestCase):
    def test_one_payload_splits_foreign_trust_dealer(self):
        tables = collector.parse_t86(FAKE_T86, date(2026, 7, 31))
        self.assertEqual(tables.foreign[0], ("2026-07-31", "2330", "台積電",
                                             42318827, 28538728, 13780099))
        self.assertEqual(tables.trust[0], ("2026-07-31", "2330", "台積電",
                                           1_000_000, 200_000, 800_000))
        # 合計含避險: buy/sell = 自行 + 避險; net = T86「自營商買賣超股數」
        self.assertEqual(tables.dealer[0], ("2026-07-31", "2330", "台積電",
                                            176_100 + 341_070, 40_900 + 69_530, 406_740))
        self.assertEqual(
            tables.dealer[0][3] - tables.dealer[0][4],
            tables.dealer[0][5],
        )

    def test_live_20260904_2330_and_2454(self):
        tables = collector.parse_t86(FAKE_T86_20260904, date(2026, 9, 4))
        by_id = {r[1]: r for r in tables.foreign}
        self.assertEqual(by_id["2330"][3:], (10_735_399, 8_850_314, 1_885_085))
        self.assertEqual(by_id["2454"][3:], (4_917_028, 4_382_524, 534_504))
        trust = {r[1]: r for r in tables.trust}
        self.assertEqual(trust["2330"][3:], (79_000, 401_024, -322_024))
        self.assertEqual(trust["2454"][3:], (357_561, 18_200, 339_361))
        dealer = {r[1]: r for r in tables.dealer}
        self.assertEqual(dealer["2330"][3:], (517_170, 110_430, 406_740))
        self.assertEqual(dealer["2454"][3:], (279_599, 176_307, 103_292))

    def test_fetch_t86_is_one_http_call(self):
        with mock_get(FAKE_T86) as get:
            tables = collector.fetch_t86(date(2026, 7, 31))
        get.assert_called_once()
        self.assertEqual(len(tables.foreign), 2)
        self.assertEqual(len(tables.trust), 2)
        self.assertEqual(len(tables.dealer), 2)

    def test_non_trading_day_empty_tables(self):
        self.assertEqual(
            collector.parse_t86({"stat": "很抱歉，沒有符合條件的資料!"}, date(2026, 7, 31)),
            collector.EMPTY_T86,
        )

    def test_dealer_net_is_not_foreign_dealer(self):
        """「外資自營商買賣超」must not be read as 自營商合計."""
        cmap = collector._t86_colmap(FAKE_T86_FIELDS)
        self.assertEqual(cmap["dealer_net"], 11)
        self.assertEqual(cmap["trust_net"], 10)
        self.assertNotEqual(cmap["dealer_net"], 7)


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

    def test_tpex_daily_quotes(self):
        with mock_get(FAKE_TPEX_QUOTES):
            rows = collector.fetch_tpex_stock_day_all(date(2026, 9, 3))
        self.assertEqual(
            rows[0],
            ("2026-09-03", "6488", "環球晶", 490.0, 505.0, 488.0, 500.0, 1234000, 600000000),
        )
        self.assertEqual(collector.roc_date(date(2026, 9, 3)), "115/09/03")

    def test_official_session_bars_merges_twse_and_tpex(self):
        bars = collector.official_session_bars(
            date(2026, 9, 3),
            fetch_twse=lambda _day: [("2026-09-03", "2330", "台積電", 1.0, 2.0, 1.0, 1.5, 1, 1)],
            fetch_tpex=lambda _day: [("2026-09-03", "6488", "環球晶", 4.0, 5.0, 3.0, 4.5, 2, 2)],
        )
        self.assertEqual(bars["2330"][6], 1.5)
        self.assertEqual(bars["6488"][6], 4.5)

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
                         "foreign_daily", "trust_daily", "dealer_daily",
                         "margin_total", "margin_stock",
                         "stock_daily", "stocks",
                         "broker_branch_daily", "brokers"} <= names)
        cols = {r[1] for r in self.rows("PRAGMA table_info(trust_daily)")}
        self.assertEqual(
            cols,
            {"trade_date", "stock_id", "stock_name", "trust_buy", "trust_sell", "trust_net"},
        )
        cols = {r[1] for r in self.rows("PRAGMA table_info(dealer_daily)")}
        self.assertEqual(
            cols,
            {"trade_date", "stock_id", "stock_name", "dealer_buy", "dealer_sell", "dealer_net"},
        )
        indexes = {r[0] for r in self.rows(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"idx_trust_stock_date", "idx_dealer_stock_date"} <= indexes)
        views = {r[0] for r in self.rows(
            "SELECT name FROM sqlite_master WHERE type='view'")}
        self.assertIn("stock_chips_daily", views)

    def test_foreign_idempotent(self):
        """同一天寫兩次不應該產生重複列。"""
        with mock_get(FAKE_T86):
            collector.save_foreign(date(2026, 7, 31))
            collector.save_foreign(date(2026, 7, 31))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM foreign_daily")[0][0], 2)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM trust_daily")[0][0], 2)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM dealer_daily")[0][0], 2)
        self.assertEqual(
            self.rows("SELECT trust_net FROM trust_daily WHERE stock_id='2330'")[0][0],
            800_000,
        )
        self.assertEqual(
            self.rows("SELECT dealer_net FROM dealer_daily WHERE stock_id='2330'")[0][0],
            406_740,
        )

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
            if len(calls) < 3:
                return collector.EMPTY_T86
            return collector.T86Tables(
                [(day.isoformat(), "2330", "台積電", 1, 2, -1)],
                [(day.isoformat(), "2330", "台積電", 0, 0, 0)],
                [(day.isoformat(), "2330", "台積電", 0, 0, 0)],
            )

        with patch.object(collector, "fetch_t86", side_effect=fake), \
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
             patch.object(backfill, "fetch_t86", return_value=collector.EMPTY_T86), \
             patch.object(backfill, "fetch_margin", return_value=([], [])):
            backfill.backfill(14)
        self.assertTrue(all(d.weekday() < 5 for d in seen), "不該請求週末")

    def test_skips_twse_holidays(self):
        seen = []

        def fake(day):
            seen.append(day)
            return []

        today = date(2026, 1, 5)  # Monday after New Year
        with patch.object(backfill, "fetch_index_5sec", side_effect=fake), \
             patch.object(backfill, "fetch_t86", return_value=collector.EMPTY_T86), \
             patch.object(backfill, "fetch_margin", return_value=([], [])):
            backfill.backfill(7, today=today, include_today=True)
        self.assertNotIn(date(2026, 1, 1), seen)
        self.assertTrue(all(d.weekday() < 5 for d in seen), "不該請求週末")

    def test_default_excludes_today(self):
        seen = []

        def fake(day):
            seen.append(day)
            return []

        today = date(2026, 8, 31)  # Monday
        with patch.object(backfill, "fetch_index_5sec", side_effect=fake), \
             patch.object(backfill, "fetch_t86", return_value=collector.EMPTY_T86), \
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
             patch.object(backfill, "fetch_t86", return_value=collector.EMPTY_T86), \
             patch.object(backfill, "fetch_margin", return_value=([], [])):
            backfill.backfill(3, today=today, include_today=True)
        self.assertIn(today, seen)

    def test_stock_daily_uses_market_wide_endpoint(self):
        requested_twse = []
        requested_tpex = []

        def fake_twse(day):
            requested_twse.append(day)
            return [(day.isoformat(), "2330", "台積電", 1., 1., 1., 1., 1, 1)]

        def fake_tpex(day):
            requested_tpex.append(day)
            return [(day.isoformat(), "6488", "環球晶", 2., 2., 2., 2., 2, 2)]

        today = date(2026, 8, 31)
        with patch.object(backfill, "fetch_stock_day_all", side_effect=fake_twse), \
             patch.object(backfill, "fetch_tpex_stock_day_all", side_effect=fake_tpex):
            n = backfill.backfill_stock_daily(3, today=today, include_today=True)
        self.assertGreater(n, 0)
        self.assertTrue(all(d.weekday() < 5 for d in requested_twse))
        self.assertEqual(requested_twse, requested_tpex)
        self.assertIn(today, requested_twse)
        rows = self.rows("SELECT stock_id FROM stock_daily WHERE trade_date=?",
                         (today.isoformat(),))
        self.assertEqual({r[0] for r in rows}, {"2330", "6488"})

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

    def test_stock_daily_adds_tpex_to_existing_twse_day(self):
        today = date(2026, 8, 31)
        con = collector.get_conn()
        with con:
            con.execute(
                "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
                (today.isoformat(), "2330", "台積電", 1., 1., 1., 1., 1, 1),
            )
        con.close()
        twse_calls = []

        def fake_twse(day):
            twse_calls.append(day)
            return [(day.isoformat(), "2330", "台積電", 1., 1., 1., 1., 1, 1)]

        def fake_tpex(day):
            return [(day.isoformat(), "6488", "環球晶", 2., 2., 2., 2., 2, 2)]

        with patch.object(backfill, "fetch_stock_day_all", side_effect=fake_twse), \
             patch.object(backfill, "fetch_tpex_stock_day_all", side_effect=fake_tpex):
            backfill.backfill_stock_daily(
                0, today=today, include_today=True, min_combined=10,
            )
        self.assertEqual(twse_calls, [], "existing listed rows must not re-hit MI_INDEX")
        ids = {r[0] for r in self.rows(
            "SELECT stock_id FROM stock_daily WHERE trade_date=?",
            (today.isoformat(),),
        )}
        self.assertEqual(ids, {"2330", "6488"})

    def test_stock_daily_raises_when_tpex_empty_on_twse_day(self):
        today = date(2026, 8, 31)

        def fake_twse(day):
            return [(day.isoformat(), "2330", "台積電", 1., 1., 1., 1., 1, 1)]

        with patch.object(backfill, "fetch_stock_day_all", side_effect=fake_twse), \
             patch.object(backfill, "fetch_tpex_stock_day_all", return_value=[]):
            with self.assertRaises(RuntimeError) as ctx:
                backfill.backfill_stock_daily(0, today=today, include_today=True)
        self.assertIn("tpex:empty", str(ctx.exception))
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM stock_daily")[0][0],
            1,
            "TWSE rows should still be saved before the job fails",
        )

    def test_stock_daily_catchup_counts_are_window_scoped(self):
        today = date(2026, 8, 31)
        seen = {}

        def fake_counts(conn, since=None):
            seen["since"] = since
            return {}

        with patch.object(backfill, "stock_daily_counts", side_effect=fake_counts), \
             patch.object(backfill, "fetch_stock_day_all", return_value=[]), \
             patch.object(backfill, "fetch_tpex_stock_day_all", return_value=[]):
            backfill.backfill_stock_daily(3, today=today, include_today=True)
        self.assertEqual(seen["since"], (today - timedelta(days=3)).isoformat())

    def test_stock_daily_holiday_both_empty_is_ok(self):
        today = date(2026, 8, 31)
        with patch.object(backfill, "fetch_stock_day_all", return_value=[]), \
             patch.object(backfill, "fetch_tpex_stock_day_all", return_value=[]):
            n = backfill.backfill_stock_daily(0, today=today, include_today=True)
        self.assertEqual(n, 0)

    def test_save_stock_day_all_writes_listed_and_otc(self):
        twse = [("2026-09-03", "2330", "台積電", 1., 1., 1., 1., 1, 1)]
        tpex = [("2026-09-03", "6488", "環球晶", 2., 2., 2., 2., 2, 2)]
        with patch.object(collector, "fetch_stock_day_all", return_value=twse), \
             patch.object(collector, "fetch_tpex_stock_day_all", return_value=tpex):
            collector.save_stock_day_all(date(2026, 9, 3))
        ids = {r[0] for r in self.rows("SELECT stock_id FROM stock_daily")}
        self.assertEqual(ids, {"2330", "6488"})
        self.assertEqual(self.rows("SELECT stock_name FROM stocks WHERE stock_id='6488'")[0][0], "環球晶")

    def test_official_session_bars_prefers_complete_stock_daily(self):
        con = collector.get_conn()
        with con:
            collector.persist_stock_daily(con, [
                ("2026-09-03", "2330", "台積電", 1., 2., 1., 1.5, 1, 1),
                ("2026-09-03", "6488", "環球晶", 4., 5., 3., 4.5, 2, 2),
            ])
        con.close()

        def boom(_day):
            raise AssertionError("live fetch must not run when stock_daily is complete")

        with patch.object(collector, "fetch_stock_day_all", side_effect=boom), \
             patch.object(collector, "fetch_tpex_stock_day_all", side_effect=boom):
            bars = collector.official_session_bars(date(2026, 9, 3), min_cached=2)
        self.assertEqual(bars["2330"][6], 1.5)
        self.assertEqual(bars["6488"][6], 4.5)

    def _t86_day(self, day, sid="2330", name="台積電"):
        ds = day.isoformat() if isinstance(day, date) else day
        return collector.T86Tables(
            [(ds, sid, name, 10, 4, 6)],
            [(ds, sid, name, 3, 1, 2)],
            [(ds, sid, name, 8, 3, 5)],
        )

    def test_backfill_skips_t86_when_all_three_tables_have_the_day(self):
        today = date(2026, 8, 31)
        ds = today.isoformat()
        con = collector.get_conn()
        with con:
            con.execute("INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                        (ds, "2330", "台積電", 1, 1, 0))
            con.execute("INSERT INTO trust_daily VALUES (?,?,?,?,?,?)",
                        (ds, "2330", "台積電", 1, 1, 0))
            con.execute("INSERT INTO dealer_daily VALUES (?,?,?,?,?,?)",
                        (ds, "2330", "台積電", 1, 1, 0))
        con.close()
        with patch.object(backfill, "fetch_index_5sec", return_value=[]), \
             patch.object(backfill, "fetch_t86", return_value=self._t86_day(today)) as t86, \
             patch.object(backfill, "fetch_margin", return_value=([], [])):
            backfill.backfill(0, today=today, include_today=True, do_index=False, do_margin=False)
        t86.assert_not_called()

    def test_backfill_fetches_when_foreign_exists_but_trust_missing(self):
        today = date(2026, 8, 31)
        ds = today.isoformat()
        con = collector.get_conn()
        with con:
            con.execute("INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                        (ds, "2330", "台積電", 1, 1, 0))
        con.close()
        with patch.object(backfill, "fetch_index_5sec", return_value=[]), \
             patch.object(backfill, "fetch_t86", return_value=self._t86_day(today)) as t86, \
             patch.object(backfill, "fetch_margin", return_value=([], [])):
            backfill.backfill(0, today=today, include_today=True, do_index=False, do_margin=False)
        t86.assert_called_once_with(today)
        self.assertEqual(
            self.rows("SELECT trust_net FROM trust_daily WHERE stock_id='2330'")[0][0], 2)
        self.assertEqual(
            self.rows("SELECT dealer_net FROM dealer_daily WHERE stock_id='2330'")[0][0], 5)

    def test_institutional_gaps_only_fetches_missing_foreign_dates(self):
        have = date(2026, 8, 28)
        gap = date(2026, 8, 31)
        con = collector.get_conn()
        with con:
            for day, filled in ((have, True), (gap, False)):
                ds = day.isoformat()
                con.execute("INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                            (ds, "2330", "台積電", 1, 1, 0))
                if filled:
                    con.execute("INSERT INTO trust_daily VALUES (?,?,?,?,?,?)",
                                (ds, "2330", "台積電", 1, 0, 1))
                    con.execute("INSERT INTO dealer_daily VALUES (?,?,?,?,?,?)",
                                (ds, "2330", "台積電", 1, 0, 1))
        con.close()
        seen = []

        def fake(day):
            seen.append(day)
            return self._t86_day(day)

        with patch.object(backfill, "fetch_t86", side_effect=fake):
            n = backfill.backfill_institutional_gaps(today=date(2026, 8, 31))
        self.assertEqual(seen, [gap])
        self.assertEqual(n, 1)
        self.assertEqual(
            self.rows("SELECT trust_net FROM trust_daily WHERE trade_date=?",
                      (gap.isoformat(),))[0][0],
            2,
        )

    def test_institutional_dry_run_does_not_fetch(self):
        con = collector.get_conn()
        with con:
            con.execute("INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                        ("2026-08-31", "2330", "台積電", 1, 1, 0))
        con.close()
        with patch.object(backfill, "fetch_t86") as t86:
            n = backfill.backfill_institutional_gaps(
                today=date(2026, 8, 31), dry_run=True)
        self.assertEqual(n, 0)
        t86.assert_not_called()

    def test_institutional_days_window_ignores_older_gaps(self):
        con = collector.get_conn()
        with con:
            con.execute("INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                        ("2026-01-05", "2330", "台積電", 1, 1, 0))
            con.execute("INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                        ("2026-08-31", "2330", "台積電", 1, 1, 0))
        con.close()
        seen = []

        def fake(day):
            seen.append(day)
            return self._t86_day(day)

        with patch.object(backfill, "fetch_t86", side_effect=fake):
            backfill.backfill_institutional_gaps(days=3, today=date(2026, 8, 31))
        self.assertEqual(seen, [date(2026, 8, 31)])

    def test_missing_dates_include_turso_foreign_not_in_local_cache(self):
        """Actions cache may have ~85 foreign dates; Turso can have ~510."""
        collector.get_conn().close()
        con = sqlite3.connect(self.db)
        with con:
            con.execute(
                "INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                ("2026-08-31", "2330", "台積電", 1, 1, 0),
            )
            con.execute(
                "INSERT INTO trust_daily VALUES (?,?,?,?,?,?)",
                ("2026-08-31", "2330", "台積電", 1, 0, 1),
            )
            con.execute(
                "INSERT INTO dealer_daily VALUES (?,?,?,?,?,?)",
                ("2026-08-31", "2330", "台積電", 1, 0, 1),
            )
        con.close()
        remote = {
            "foreign_daily": {"2026-01-05", "2026-08-31"},
            "trust_daily": {"2026-08-31"},
            "dealer_daily": {"2026-08-31"},
        }
        conn = collector.get_conn()
        try:
            missing = backfill.missing_institutional_dates(conn, remote_dates=remote)
        finally:
            conn.close()
        self.assertEqual(missing, ["2026-01-05"])

    def test_missing_dates_skip_when_turso_already_has_trust_and_dealer(self):
        collector.get_conn().close()
        con = sqlite3.connect(self.db)
        with con:
            con.execute(
                "INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                ("2026-01-05", "2330", "台積電", 1, 1, 0),
            )
        con.close()
        remote = {
            "foreign_daily": {"2026-01-05"},
            "trust_daily": {"2026-01-05"},
            "dealer_daily": {"2026-01-05"},
        }
        conn = collector.get_conn()
        try:
            missing = backfill.missing_institutional_dates(conn, remote_dates=remote)
        finally:
            conn.close()
        self.assertEqual(missing, [])

    def test_missing_dates_skip_fetch_when_local_already_complete(self):
        """Local trust/dealer present → no T86; Turso push still sees the gap."""
        collector.get_conn().close()
        con = sqlite3.connect(self.db)
        with con:
            con.execute(
                "INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                ("2026-01-05", "2330", "台積電", 1, 1, 0),
            )
            con.execute(
                "INSERT INTO trust_daily VALUES (?,?,?,?,?,?)",
                ("2026-01-05", "2330", "台積電", 1, 0, 1),
            )
            con.execute(
                "INSERT INTO dealer_daily VALUES (?,?,?,?,?,?)",
                ("2026-01-05", "2330", "台積電", 1, 0, 1),
            )
        con.close()
        remote = {
            "foreign_daily": {"2026-01-05"},
            "trust_daily": set(),
            "dealer_daily": set(),
        }
        conn = collector.get_conn()
        try:
            missing = backfill.missing_institutional_dates(conn, remote_dates=remote)
            days = backfill.institutional_push_days(
                conn, date(2026, 8, 31), remote_dates=remote, default=14,
            )
        finally:
            conn.close()
        self.assertEqual(missing, [])
        self.assertGreaterEqual(days, (date(2026, 8, 31) - date(2026, 1, 5)).days)

    def test_institutional_gaps_fetches_turso_only_foreign_dates(self):
        collector.get_conn().close()
        con = sqlite3.connect(self.db)
        with con:
            con.execute(
                "INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                ("2026-08-31", "2330", "台積電", 1, 1, 0),
            )
            con.execute(
                "INSERT INTO trust_daily VALUES (?,?,?,?,?,?)",
                ("2026-08-31", "2330", "台積電", 1, 0, 1),
            )
            con.execute(
                "INSERT INTO dealer_daily VALUES (?,?,?,?,?,?)",
                ("2026-08-31", "2330", "台積電", 1, 0, 1),
            )
        con.close()
        seen = []

        def fake(day):
            seen.append(day)
            return self._t86_day(day)

        remote = {
            "foreign_daily": {"2026-01-05", "2026-08-31"},
            "trust_daily": {"2026-08-31"},
            "dealer_daily": {"2026-08-31"},
        }
        with patch.object(backfill, "fetch_t86", side_effect=fake), \
             patch.object(backfill.time, "sleep"):
            n = backfill.backfill_institutional_gaps(
                today=date(2026, 8, 31), remote_dates=remote,
            )
        self.assertEqual(seen, [date(2026, 1, 5)])
        self.assertEqual(n, 1)
        self.assertEqual(
            self.rows("SELECT trust_net FROM trust_daily WHERE trade_date=?",
                      ("2026-01-05",))[0][0],
            2,
        )

    def test_institutional_days_window_filters_turso_remote_dates(self):
        collector.get_conn().close()
        remote = {
            "foreign_daily": {"2026-01-05", "2026-08-31"},
            "trust_daily": set(),
            "dealer_daily": set(),
        }
        conn = collector.get_conn()
        try:
            missing = backfill.missing_institutional_dates(
                conn, since="2026-08-28", remote_dates=remote,
            )
        finally:
            conn.close()
        self.assertEqual(missing, ["2026-08-31"])

    def test_institutional_push_days_uses_oldest_turso_gap(self):
        collector.get_conn().close()
        con = sqlite3.connect(self.db)
        with con:
            con.execute(
                "INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                ("2026-08-31", "2330", "台積電", 1, 1, 0),
            )
        con.close()
        remote = {
            "foreign_daily": {"2025-09-01", "2026-08-31"},
            "trust_daily": {"2026-08-31"},
            "dealer_daily": {"2026-08-31"},
        }
        conn = collector.get_conn()
        try:
            days = backfill.institutional_push_days(
                conn, date(2026, 8, 31), remote_dates=remote, default=14,
            )
        finally:
            conn.close()
        self.assertEqual(days, (date(2026, 8, 31) - date(2025, 9, 1)).days + 7)


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
        self.assertEqual(r["start"], "2026-07-31")
        self.assertEqual(r["end"], "2026-07-31")
        self.assertEqual(r["trading_days"], 1)

    def _insert_foreign(self, rows):
        con = sqlite3.connect(self.db)
        with con:
            con.executemany(
                "INSERT OR REPLACE INTO foreign_daily VALUES (?,?,?,?,?,?)", rows)
        con.close()

    def test_top_defaults_to_latest_day(self):
        r = self.call("/api/top")
        self.assertEqual(r["date"], "2026-07-31")
        self.assertEqual(r["start"], r["end"])
        self.assertEqual(r["buy"][0][0], "2330")

    def test_top_aggregates_custom_date_range(self):
        self._insert_foreign([
            ("2026-07-30", "2330", "台積電", 1000, 0, 1000),
            ("2026-07-30", "2317", "鴻海", 0, 5_000_000, -5_000_000),
            ("2026-07-30", "2454", "聯發科", 8_000_000, 0, 8_000_000),
        ])
        r = self.call("/api/top", start="2026-07-30", end="2026-07-31")
        self.assertEqual(r["start"], "2026-07-30")
        self.assertEqual(r["end"], "2026-07-31")
        self.assertEqual(r["date"], "2026-07-30 ~ 2026-07-31")
        self.assertEqual(r["trading_days"], 2)
        self.assertEqual(r["buy"][0][0], "2330")
        self.assertEqual(r["buy"][0][2], 13_780_099 + 1000)
        self.assertEqual(r["buy"][1][0], "2454")
        self.assertEqual(r["buy"][1][2], 8_000_000)
        self.assertEqual(r["sell"][0][0], "2317")
        self.assertEqual(r["sell"][0][2], -7_000_000)

    def test_top_days_uses_last_n_trading_days(self):
        self._insert_foreign([
            ("2026-07-29", "2330", "台積電", 0, 1, -1),
            ("2026-07-30", "2330", "台積電", 0, 1, -1),
        ])
        one = self.call("/api/top", days=1)
        self.assertEqual(one["start"], "2026-07-31")
        self.assertEqual(one["end"], "2026-07-31")
        self.assertEqual(one["trading_days"], 1)
        two = self.call("/api/top", days=2)
        self.assertEqual(two["start"], "2026-07-30")
        self.assertEqual(two["end"], "2026-07-31")
        self.assertEqual(two["trading_days"], 2)

    def test_top_swaps_inverted_range_and_ignores_bad_dates(self):
        r = self.call("/api/top", start="2026-07-31", end="2026-07-01")
        self.assertEqual(r["start"], "2026-07-01")
        self.assertEqual(r["end"], "2026-07-31")
        bad = self.call("/api/top", start="not-a-date")
        self.assertEqual(bad["start"], "2026-07-31")
        self.assertEqual(bad["end"], "2026-07-31")

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

    def test_stock_search_by_id_and_name(self):
        by_id = self.call("/api/stocks", q="2330")
        self.assertEqual(by_id["data"][0][0], "2330")
        by_name = self.call("/api/stocks", q="台積")
        self.assertEqual(by_name["data"][0][0], "2330")
        self.assertEqual(self.call("/api/stocks")["data"], [])

    def test_stock_ohlc(self):
        con = sqlite3.connect(self.db)
        with con:
            con.execute(
                "INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-07-31", "2330", "台積電", 1450.0, 1465.0, 1445.0, 1460.0,
                 42318827, 61362300000),
            )
        con.close()
        r = self.call("/api/stock_ohlc", id="2330", days=90)
        self.assertEqual(r["name"], "台積電")
        self.assertEqual(r["data"][0], ["2026-07-31", 1460.0])

    def test_all_endpoints_json_serialisable(self):
        for p, kw in [("/api/summary", {}), ("/api/taiex", {"days": 90}),
                      ("/api/ohlc", {"days": 90}), ("/api/foreign_total", {"days": 90}),
                      ("/api/margin_total", {"days": 90}), ("/api/top", {}),
                      ("/api/stock", {"id": "2330"}), ("/api/stock_margin", {"id": "2330"}),
                      ("/api/stocks", {"q": "2330"}), ("/api/stock_ohlc", {"id": "2330"}),
                      ("/api/freshness", {}),
                      ("/api/broker_branch/top", {}),
                      ("/api/broker_branch/broker", {"broker_id": "1020"}),
                      ("/api/broker_branch/stock", {"id": "2330"}),
                      ("/api/broker_branch/freshness", {}),
                      ("/api/scanner/chip_zscore", {"tickers": "2330"})]:
            with self.subTest(path=p):
                json.dumps(self.call(p, **kw))


if __name__ == "__main__":
    unittest.main(verbosity=2)
