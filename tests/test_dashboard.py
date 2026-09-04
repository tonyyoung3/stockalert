import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from alertsdb import store as alerts_db
from data import market_db
from web import dashboard


class DashboardAlertsAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.screener = root / "screener.db"
        self.market = root / "twse.db"
        alerts_db.set_db_path(self.screener)
        market_db.set_db_path(self.market)

    def tearDown(self):
        alerts_db.set_db_path(None)
        market_db.set_db_path(None)
        self.tmp.cleanup()

    def call(self, path, **qs):
        return dashboard.api(path, {k: [str(v)] for k, v in qs.items()})

    def _init_screener(self):
        alerts_db.init_db()

    def _init_market_names(self):
        conn = sqlite3.connect(self.market)
        with conn:
            conn.execute(
                "CREATE TABLE stocks (stock_id TEXT PRIMARY KEY, stock_name TEXT, updated TEXT)"
            )
            conn.execute("INSERT INTO stocks VALUES ('2330','台積電','2026-07-31')")
            conn.execute("INSERT INTO stocks VALUES ('2317','鴻海','2026-07-31')")
        conn.close()

    def test_html_has_alert_sections_and_empty_copy(self):
        self.assertIn("今日／近期告警", dashboard.HTML)
        self.assertIn("績效摘要", dashboard.HTML)
        self.assertIn("尚無告警", dashboard.HTML)
        self.assertIn("尚未結算", dashboard.HTML)
        self.assertIn("/api/alerts", dashboard.HTML)
        self.assertIn("/api/performance", dashboard.HTML)

    def test_alerts_empty_when_db_missing(self):
        r = self.call("/api/alerts")
        self.assertTrue(r["empty"])
        self.assertEqual(r["data"], [])
        self.assertEqual(r["days"], 30)
        json.dumps(r)

    def test_alerts_empty_when_table_has_no_rows(self):
        self._init_screener()
        r = self.call("/api/alerts", days=30)
        self.assertTrue(r["empty"])
        self.assertEqual(r["data"], [])

    def test_performance_empty_when_db_missing(self):
        r = self.call("/api/performance")
        self.assertTrue(r["empty"])
        self.assertEqual([h["horizon_td"] for h in r["horizons"]], [5, 20, 60])
        self.assertIn("交易日", r["assumptions"])
        json.dumps(r)

    def test_alerts_lists_recent_and_attaches_name(self):
        self._init_screener()
        self._init_market_names()
        today = date.today()
        recent = str(today - timedelta(days=2))
        old = str(today - timedelta(days=40))
        alerts_db.save_alert("2330", "upper_shadow_reversal", recent, 1450.0)
        alerts_db.save_alert("2317", "inside_day", old, 100.0)
        r = self.call("/api/alerts", days=30)
        self.assertFalse(r["empty"])
        self.assertEqual(len(r["data"]), 1)
        row = r["data"][0]
        self.assertEqual(row["ticker"], "2330")
        self.assertEqual(row["name"], "台積電")
        self.assertEqual(row["pattern_type"], "upper_shadow_reversal")
        self.assertEqual(row["price_at_alert"], 1450.0)
        self.assertIsNone(row["theme"])
        self.assertEqual(row["alert_date"], recent)

        wide = self.call("/api/alerts", days=90)
        self.assertEqual({a["ticker"] for a in wide["data"]}, {"2330", "2317"})
        self.assertEqual(wide["data"][0]["ticker"], "2330")

    def test_alerts_days_default_and_clamp(self):
        self._init_screener()
        self.assertEqual(self.call("/api/alerts")["days"], 30)
        self.assertEqual(self.call("/api/alerts", days="nope")["days"], 30)
        self.assertEqual(self.call("/api/alerts", days=0)["days"], 1)
        self.assertEqual(self.call("/api/alerts", days=9999)["days"], 365)

    def test_performance_per_horizon_not_mixed(self):
        self._init_screener()
        a = alerts_db.save_alert("2330", "upper_shadow_reversal", "2026-06-01", 100.0)
        b = alerts_db.save_alert("2317", "inside_day", "2026-06-02", 50.0)
        alerts_db.save_performance(a, "2026-06-09", 110.0, 10.0, horizon_td=5)
        alerts_db.save_performance(a, "2026-06-30", 98.0, -2.0, horizon_td=20)
        alerts_db.save_performance(b, "2026-06-10", 52.0, 4.0, horizon_td=5)
        r = self.call("/api/performance")
        self.assertFalse(r["empty"])
        by_h = {h["horizon_td"]: h for h in r["horizons"]}
        self.assertEqual(by_h[5]["n"], 2)
        self.assertEqual(by_h[5]["wins"], 2)
        self.assertEqual(by_h[5]["avg_return_pct"], 7.0)
        self.assertEqual(by_h[20]["n"], 1)
        self.assertEqual(by_h[20]["wins"], 0)
        self.assertEqual(by_h[60]["n"], 0)
        t5 = {p["pattern_type"]: p for p in by_h[5]["by_pattern"]}
        self.assertEqual(t5["inside_day"]["n"], 1)
        self.assertEqual(t5["upper_shadow_reversal"]["avg_return_pct"], 10.0)
        json.dumps(r)

    def test_missing_alerts_table_is_empty_not_error(self):
        sqlite3.connect(self.screener).close()
        r = self.call("/api/alerts")
        self.assertTrue(r["empty"])
        p = self.call("/api/performance")
        self.assertTrue(p["empty"])


if __name__ == "__main__":
    unittest.main()
