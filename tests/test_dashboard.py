import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from alertsdb import store as alerts_db
from data import market_db
from web import dashboard


class DashboardAlertsAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.screener = root / "screener.db"
        self.market = root / "twse.db"
        sqlite3.connect(self.market).close()
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
        self.assertIn("data-ticker=", dashboard.HTML)
        self.assertNotIn("onclick=\"showStock(", dashboard.HTML)
        self.assertIn("fresh-banner", dashboard.HTML)
        self.assertIn("請跑 python -m market.update_market_data", dashboard.HTML)

    def test_html_stock_lookup_wires_ranking_search_and_deeplink(self):
        html = dashboard.HTML
        self.assertIn("function selectStock(", html)
        self.assertIn("selectStock(row[0], row[1])", html)
        self.assertIn("onClick:(evt, els)=>", html)
        self.assertIn("c-buy", html)
        self.assertIn("c-sell", html)
        self.assertIn("查無此股", html)
        self.assertIn("/api/stocks?q=", html)
        self.assertIn("submitStockSearch", html)
        self.assertIn("sidActive<0?0:sidActive", html)
        self.assertIn("new URLSearchParams", html)
        self.assertIn("history.replaceState", html)
        self.assertIn("parseStockQuery(location.search)", html)
        self.assertIn("代號或名稱", html)
        self.assertIn("id=\"stock-lookup\"", html)
        self.assertIn("scrollIntoView", html)
        self.assertIn("function showStock(id, name){ selectStock(id, name); }", html)
        self.assertNotIn("placeholder=\"輸入股票代號,例如 2330\"", html)

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

    def test_turso_alerts_reuse_request_connection(self):
        shared = MagicMock(name="request_conn")
        token = dashboard._request_conn.set(shared)
        try:
            with patch.object(market_db, "using_turso", return_value=True):
                conn, owns = dashboard._open_alerts_conn()
            self.assertIs(conn, shared)
            self.assertFalse(owns)
        finally:
            dashboard._request_conn.reset(token)


class ParseStockQueryTests(unittest.TestCase):
    def test_extracts_ticker_from_query_path_or_url(self):
        self.assertEqual(dashboard.parse_stock_query("stock=2330"), "2330")
        self.assertEqual(dashboard.parse_stock_query("?stock=2330"), "2330")
        self.assertEqual(dashboard.parse_stock_query("/?stock=2330&days=90"), "2330")
        self.assertEqual(
            dashboard.parse_stock_query("http://localhost:8765/?stock=2330"), "2330"
        )
        self.assertEqual(dashboard.parse_stock_query("?stock=00631L"), "00631L")
        self.assertEqual(dashboard.parse_stock_query("stock=2330 台積電"), "2330")
        self.assertEqual(
            dashboard.parse_stock_query("?stock=2330#backtest"), "2330"
        )
        self.assertEqual(
            dashboard.parse_stock_query("/?stock=2330#stock"), "2330"
        )

    def test_rejects_missing_or_unsafe_values(self):
        self.assertIsNone(dashboard.parse_stock_query(""))
        self.assertIsNone(dashboard.parse_stock_query(None))
        self.assertIsNone(dashboard.parse_stock_query("?days=90"))
        self.assertIsNone(dashboard.parse_stock_query("?stock="))
        self.assertIsNone(dashboard.parse_stock_query("?stock=<script>"))
        self.assertIsNone(dashboard.parse_stock_query("?stock=a"))
        self.assertIsNone(dashboard.parse_stock_query("?stock=" + "1" * 11))


class DashboardNavTests(unittest.TestCase):
    def test_html_has_section_tabs_defaulting_to_market(self):
        html = dashboard.HTML
        self.assertIn("role=\"tablist\"", html)
        self.assertIn(">市場</a>", html)
        self.assertIn(">個股</a>", html)
        self.assertIn(">告警</a>", html)
        self.assertIn(">回測</a>", html)
        self.assertIn("新增積木", html)
        self.assertIn("尚未加入濾網積木", html)
        self.assertIn("執行前規則摘要", html)
        self.assertIn("id=\"bt-summary\"", html)
        self.assertIn("function btBuildBlocks(", html)
        self.assertIn("function btAddFilter(", html)
        self.assertNotIn('id="bt-trend"', html)
        self.assertNotIn('id="bt-macross"', html)
        self.assertIn("id=\"section-market\"", html)
        self.assertIn("id=\"section-stock\"", html)
        self.assertIn("id=\"section-alerts\"", html)
        self.assertIn("id=\"section-backtest\"", html)
        self.assertIn("id=\"stock-lookup\"", html)
        self.assertIn("href=\"#market\"", html)
        self.assertIn("href=\"#stock\"", html)
        self.assertIn("href=\"#alerts\"", html)
        self.assertIn("href=\"#backtest\"", html)
        self.assertIn(
            "id=\"section-backtest\" class=\"page-section\" role=\"tabpanel\" "
            "aria-labelledby=\"tab-backtest\" hidden>",
            html,
        )
        self.assertIn(
            "id=\"section-stock\" class=\"page-section\" role=\"tabpanel\" "
            "aria-labelledby=\"tab-stock\" hidden>",
            html,
        )
        self.assertIn(
            "id=\"section-alerts\" class=\"page-section\" role=\"tabpanel\" "
            "aria-labelledby=\"tab-alerts\" hidden>",
            html,
        )
        self.assertNotIn(
            "id=\"section-market\" class=\"page-section\" role=\"tabpanel\" "
            "aria-labelledby=\"tab-market\" hidden>",
            html,
        )
        self.assertLess(html.index('id="section-market"'), html.index('id="section-backtest"'))

    def test_html_clarifies_global_days_vs_foreign_ranking(self):
        html = dashboard.HTML
        self.assertIn("全域天數影響指數／籌碼／個股圖；外資排行用自己的日期區間。", html)
        self.assertIn("此區間只控制排行，與上方全域天數無關", html)
        self.assertIn("id=\"days\"", html)
        self.assertIn("id=\"top-preset\"", html)

    def test_js_hash_and_selectstock_switch_sections(self):
        html = dashboard.HTML
        self.assertIn("const PAGE_SECTIONS", html)
        self.assertIn("function parseSectionHash(", html)
        self.assertIn("function resolveSection(", html)
        self.assertIn("function showSection(", html)
        self.assertIn("addEventListener('hashchange'", html)
        self.assertIn("parseStockQuery(location.search) ? 'stock' : 'market'", html)
        self.assertIn("if(opts.section !== false) showSection('stock')", html)
        self.assertIn("showSection(resolveSection(), {updateHash:false})", html)
        self.assertIn("id=\"bt-form\"", html)
        self.assertIn("data-bt-fold=\"filters\"", html)
        self.assertIn("data-bt-fold=\"entry\"", html)
        self.assertIn("scrollIntoView", html)

    def test_html_is_usable_on_narrow_phones(self):
        html = dashboard.HTML
        self.assertIn("@media(max-width:768px)", html)
        self.assertIn(".ov-search input{width:100%", html)
        self.assertNotIn(".ov-search input{width:260px}", html)
        self.assertIn("minmax(min(100%,420px)", html)
        self.assertIn("table-scroll", html)
        self.assertIn("拖曳／雙指縮放", html)
        self.assertNotIn("滾輪縮放", html)
        self.assertIn("function initBtFolds(", html)
        self.assertIn("function btApplyMobileBlockFolds(", html)
        self.assertIn("日內會收合,不套用", html)
        self.assertIn('<details class="bt-box" data-bt-fold="filters" open>', html)
        self.assertIn('<details class="bt-box" data-bt-fold="entry" open>', html)
        self.assertIn('data-bt-fold="exit"', html)
        self.assertIn(".top-range input[type=\"date\"]{min-width:9.5em;height:44px!important;min-height:44px", html)
        self.assertIn("-webkit-appearance:none", html)
        self.assertIn("bindSuggestPick", html)
        self.assertIn(">市場</a>", html)
        self.assertIn(">個股</a>", html)
        self.assertIn(">告警</a>", html)
        self.assertIn(">回測</a>", html)
        self.assertIn("參數為第二層", html)
        self.assertIn("偷看未來資訊", html)

    def test_backtest_blocks_ui_replaces_fixed_filter_wall(self):
        html = dashboard.HTML
        self.assertIn("尚未加入濾網積木", html)
        self.assertIn("新增積木", html)
        self.assertIn("執行前規則摘要", html)
        self.assertIn("id=\"bt-filter-empty\"", html)
        self.assertIn("id=\"bt-close-decided-hint\"", html)
        self.assertIn("沒有任何交易被觸發", html)
        self.assertIn("credentials:'same-origin'", html)
        self.assertIn("JSON.stringify(btBuildBlocks())", html)
        self.assertIn("則進場（日內）", html)
        self.assertIn("則出場（日內）", html)
        self.assertIn("則進場（隔夜,收盤）", html)
        self.assertIn("則出場（隔夜）", html)
        self.assertIn("則進場（波段,收盤）", html)
        self.assertNotIn("inside_day", dashboard.HTML[dashboard.HTML.find("BT_FILTER_CATALOG"):dashboard.HTML.find("let btFilters")])

    def test_backtest_local_presets_ui_wiring(self):
        from web.strategy_presets import (
            ERR_APPLY,
            ERR_CAP,
            ERR_EMPTY_JSON,
            ERR_JSON,
            ERR_MISSING,
            ERR_NAME,
            ERR_NOT_OBJECT,
            ERR_NOT_V1,
            ERR_STORAGE,
            ERR_STORE,
            ERR_STORE_NOT_RULE,
            PRESET_CAP,
            PRESET_STORAGE_KEY,
        )

        html = dashboard.HTML
        self.assertIn(PRESET_STORAGE_KEY, html)
        self.assertIn(f"BT_PRESET_CAP = {PRESET_CAP}", html)
        self.assertIn("localStorage.getItem(BT_PRESET_KEY)", html)
        self.assertIn("localStorage.setItem(BT_PRESET_KEY", html)
        self.assertIn("id=\"bt-preset-select\"", html)
        self.assertIn("id=\"bt-preset-name\"", html)
        self.assertIn("id=\"bt-preset-json\"", html)
        self.assertIn("id=\"bt-preset-msg\"", html)
        self.assertIn("onclick=\"btLoadPreset()\"", html)
        self.assertIn("onclick=\"btOverwritePreset()\"", html)
        self.assertIn("onclick=\"btDeletePreset()\"", html)
        self.assertIn("onclick=\"btSavePreset()\"", html)
        self.assertIn("onclick=\"btExportPresetJson()\"", html)
        self.assertIn("onclick=\"btImportPresetJson()\"", html)
        self.assertIn("function btApplyBlocks(", html)
        self.assertIn("function btSetModeRadio(", html)
        self.assertIn("el.checked = (el.value === mode)", html)
        self.assertIn("function btParseBlocksJson(", html)
        self.assertIn("function btValidateBlocksDoc(", html)
        self.assertIn("role=\"status\"", html)
        self.assertIn(ERR_JSON, html)
        self.assertIn(ERR_EMPTY_JSON, html)
        self.assertIn(ERR_NOT_OBJECT, html)
        self.assertIn(ERR_NOT_V1, html)
        self.assertIn(ERR_NAME, html)
        self.assertIn(ERR_CAP, html)
        self.assertIn(ERR_MISSING, html)
        self.assertIn(ERR_STORE, html)
        self.assertIn(ERR_STORAGE, html)
        self.assertIn(ERR_APPLY, html)
        self.assertIn(ERR_STORE_NOT_RULE, html)
        self.assertIn("el.textContent = text", html)

    def test_readme_documents_nav_and_days_scope(self):
        text = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(encoding="utf-8")
        self.assertIn("### 儀表板分區", text)
        self.assertIn("#backtest", text)
        self.assertIn("不受全域天數控制", text)
        self.assertIn("?stock=", text)
        self.assertIn("375px", text)
        self.assertIn("雙指縮放", text)
        self.assertIn("回測規則積木", text)
        self.assertIn("blocks_to_rule", text)
        self.assertIn("weekdays", text)
        self.assertIn("stockalert.bt.presets.v1", text)
        self.assertIn("最多 20 筆", text)
        self.assertIn("不會白屏", text)


if __name__ == "__main__":
    unittest.main()
