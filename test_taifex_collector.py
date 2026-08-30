#!/usr/bin/env python3
"""taifex_collector.py 的單元測試(不打真實 API,全部用實際抓回的樣本資料 mock)。

執行: python -m unittest test_taifex_collector -v
"""
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

import taifex_collector as tc

# ---- 以下兩段是 2026-08-06 實際從期交所抓回的原始 CSV 片段(Big5 解碼後) ----

FUT_CSV = """日期,商品名稱,身份別,多方交易口數,多方契約金額(千元),空方交易口數,空方契約金額(千元),多空交易口數淨額,多空契約金額淨額(千元),多方未平倉口數,多方未平倉契約金額(千元),空方未平倉口數,空方未平倉契約金額(千元),多空未平倉口數淨額,多空未平倉契約金額淨額(千元)
2026/08/06,臺股期貨,自營商,4534,40089309,3235,28625200,1299,11464109,7060,62566503,4458,39505760,2602,23060743
2026/08/06,臺股期貨,投信,310,2748542,722,6391196,-412,-3642654,87666,775987798,3503,31007155,84163,744980643
2026/08/06,臺股期貨,外資及陸資,59873,529928712,61994,548737488,-2121,-18808776,8953,79285373,98336,870551296,-89383,-791265923
2026/08/06,小型臺指期貨,自營商,7899,17490984,7136,15808295,763,1682689,3342,7413303,7906,17545647,-4564,-10132344
"""

OPT_CSV = """日期,商品名稱,買賣權別,身份別,買方交易口數,買方契約金額(千元),賣方交易口數,賣方契約金額(千元),買賣交易口數淨額,買賣契約金額淨額(千元),買方未平倉口數,買方未平倉契約金額(千元),賣方未平倉口數,賣方未平倉契約金額(千元),買賣未平倉口數淨額,買賣未平倉契約金額淨額(千元)
2026/08/06,臺指選擇權,CALL,自營商,30935,270106,34199,339932,-3264,-69826,24847,1041525,22666,828929,2181,212596
2026/08/06,臺指選擇權,CALL,投信,0,0,241,15328,-241,-15328,5,390,2830,213322,-2825,-212932
2026/08/06,臺指選擇權,CALL,外資及陸資,39248,432752,38337,405138,911,27614,9345,388416,6693,445419,2652,-57003
2026/08/06,臺指選擇權,PUT,外資及陸資,46516,480978,44165,479520,2351,1458,17399,321922,13627,329513,3772,-7591
"""


class FakeResp:
    def __init__(self, text):
        self.content = text.encode("ms950")

    def raise_for_status(self):
        pass


def fake_get(url, params=None, headers=None, timeout=None):
    return FakeResp(FUT_CSV if "futContracts" in url else OPT_CSV)


class TestParsing(unittest.TestCase):

    def test_num(self):
        self.assertEqual(tc._num("1,234"), 1234)
        self.assertEqual(tc._num("-89,383"), -89383)
        self.assertEqual(tc._num("0"), 0)
        self.assertIsNone(tc._num(""))
        self.assertIsNone(tc._num("-"))
        self.assertIsNone(tc._num("abc"))

    @patch("taifex_collector.requests.get", fake_get)
    def test_fetch_fut_oi(self):
        rows = tc.fetch_fut_oi(date(2026, 8, 6), date(2026, 8, 6))
        self.assertEqual(len(rows), 4)          # 表頭已剝除
        r = rows[2]                             # 臺股期貨 外資
        self.assertEqual(r[0], "2026-08-06")    # 日期已轉 ISO
        self.assertEqual(r[1], "臺股期貨")
        self.assertEqual(r[2], "外資及陸資")
        self.assertEqual(r[9], 8953)            # 未平倉多方口數
        self.assertEqual(r[11], 98336)          # 未平倉空方口數
        self.assertEqual(r[13], -89383)         # 未平倉淨額口數(負=淨空)
        self.assertEqual(len(r), 15)

    @patch("taifex_collector.requests.get", fake_get)
    def test_fetch_opt_oi(self):
        rows = tc.fetch_opt_oi(date(2026, 8, 6), date(2026, 8, 6))
        self.assertEqual(len(rows), 4)
        call_f = [r for r in rows if r[2] == "CALL" and r[3] == "外資及陸資"][0]
        put_f = [r for r in rows if r[2] == "PUT" and r[3] == "外資及陸資"][0]
        self.assertEqual(call_f[14], 2652)      # CALL 未平倉淨額
        self.assertEqual(put_f[14], 3772)       # PUT  未平倉淨額
        self.assertEqual(len(call_f), 16)

    @patch("taifex_collector.requests.get", fake_get)
    def test_net_consistency(self):
        """未平倉淨額 應該等於 多方 - 空方(資料完整性檢查)。"""
        for r in tc.fetch_fut_oi(date(2026, 8, 6), date(2026, 8, 6)):
            self.assertEqual(r[13], r[9] - r[11], msg=f"{r[1]} {r[2]}")
        for r in tc.fetch_opt_oi(date(2026, 8, 6), date(2026, 8, 6)):
            self.assertEqual(r[14], r[10] - r[12], msg=f"{r[1]} {r[2]} {r[3]}")

    def test_month_iter(self):
        chunks = list(tc._month_iter(date(2026, 1, 15), date(2026, 3, 10)))
        self.assertEqual(chunks[0], (date(2026, 1, 15), date(2026, 1, 31)))
        self.assertEqual(chunks[1], (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(chunks[2], (date(2026, 3, 1), date(2026, 3, 10)))


class TestStorage(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        tc.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    @patch("taifex_collector.requests.get", fake_get)
    @patch("taifex_collector.time.sleep", lambda *_: None)
    def test_collect_and_idempotent(self):
        nf, no = tc.collect_range(date(2026, 8, 6), date(2026, 8, 6), self.conn)
        self.assertEqual((nf, no), (4, 4))
        # 重跑一次不該產生重複列(PRIMARY KEY + INSERT OR REPLACE)
        tc.collect_range(date(2026, 8, 6), date(2026, 8, 6), self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM taifex_fut_oi").fetchone()[0], 4)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM taifex_opt_oi").fetchone()[0], 4)

    @patch("taifex_collector.requests.get", fake_get)
    @patch("taifex_collector.time.sleep", lambda *_: None)
    def test_query_roundtrip(self):
        tc.collect_range(date(2026, 8, 6), date(2026, 8, 6), self.conn)
        v = self.conn.execute(
            "SELECT oi_net_lots FROM taifex_fut_oi "
            "WHERE product='臺股期貨' AND investor='外資及陸資'").fetchone()[0]
        self.assertEqual(v, -89383)
        v = self.conn.execute(
            "SELECT oi_net_lots FROM taifex_opt_oi "
            "WHERE product='臺指選擇權' AND cp='PUT' AND investor='外資及陸資'").fetchone()[0]
        self.assertEqual(v, 3772)


if __name__ == "__main__":
    unittest.main(verbosity=2)
