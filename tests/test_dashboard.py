import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from alertsdb import store as alerts_db
from data import market_db
from web import dashboard
from web.tw_calendar import taiwan_today


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
        self.assertIn(">回測此訊號<", dashboard.HTML)
        self.assertIn("alert-bt-btn", dashboard.HTML)
        self.assertNotIn("onclick=\"showStock(", dashboard.HTML)
        self.assertIn("fresh-banner", dashboard.HTML)
        self.assertIn("請跑 python -m market.update_market_data", dashboard.HTML)
        self.assertIn("樣本不足", dashboard.HTML)

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
        today = taiwan_today()
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


class ParseBacktestQueryTests(unittest.TestCase):
    def test_hash_query_stock_and_pattern(self):
        self.assertEqual(
            dashboard.parse_backtest_query(
                "#backtest?stock=2330&pattern=upper_shadow_reversal"
            ),
            {"stock": "2330", "pattern": "upper_shadow_reversal"},
        )
        self.assertEqual(
            dashboard.parse_backtest_query(
                "/?days=90#backtest?stock=2317&pattern=inside_day"
            ),
            {"stock": "2317", "pattern": "inside_day"},
        )
        self.assertEqual(
            dashboard.parse_backtest_query("#section-backtest?stock=00631L"),
            {"stock": "00631L", "pattern": ""},
        )

    def test_search_params_when_hash_is_backtest(self):
        self.assertEqual(
            dashboard.parse_backtest_query("?stock=2330&pattern=inside_day#backtest"),
            {"stock": "2330", "pattern": "inside_day"},
        )
        self.assertEqual(
            dashboard.parse_backtest_query(
                "http://localhost:8765/?bts=2330&btp=inside_day#backtest"
            ),
            {"stock": "2330", "pattern": "inside_day"},
        )

    def test_ignores_stock_page_query(self):
        self.assertIsNone(dashboard.parse_backtest_query("?stock=2330"))
        self.assertIsNone(dashboard.parse_backtest_query("?stock=2330#stock"))
        self.assertIsNone(dashboard.parse_backtest_query("#alerts"))
        self.assertIsNone(dashboard.parse_backtest_query("#backtest"))
        self.assertIsNone(dashboard.parse_backtest_query(""))
        self.assertIsNone(dashboard.parse_backtest_query(None))

    def test_bad_data_does_not_raise(self):
        self.assertIsNone(dashboard.parse_backtest_query("#backtest?stock=<script>"))
        self.assertEqual(
            dashboard.parse_backtest_query("#backtest?stock=<script>&pattern=nope"),
            {"stock": "", "pattern": "nope"},
        )
        self.assertEqual(
            dashboard.parse_backtest_query("#backtest?stock=a&pattern=scanner_foreign_net_z"),
            {"stock": "", "pattern": "scanner_foreign_net_z"},
        )
        self.assertIsNone(dashboard.parse_backtest_query("#backtest?stock=" + "1" * 11))


class DashboardNavTests(unittest.TestCase):
    def test_html_has_section_tabs_defaulting_to_market(self):
        html = dashboard.HTML
        self.assertIn("role=\"tablist\"", html)
        self.assertIn(">市場</a>", html)
        self.assertIn(">個股</a>", html)
        self.assertIn(">掃描</a>", html)
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
        self.assertIn("id=\"section-scanner\"", html)
        self.assertIn("id=\"section-alerts\"", html)
        self.assertIn("id=\"section-backtest\"", html)
        self.assertIn("id=\"stock-lookup\"", html)
        self.assertIn("href=\"#market\"", html)
        self.assertIn("href=\"#stock\"", html)
        self.assertIn("href=\"#scanner\"", html)
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
        self.assertIn(
            "id=\"section-scanner\" class=\"page-section\" role=\"tabpanel\" "
            "aria-labelledby=\"tab-scanner\" hidden>",
            html,
        )
        self.assertNotIn(
            "id=\"section-market\" class=\"page-section\" role=\"tabpanel\" "
            "aria-labelledby=\"tab-market\" hidden>",
            html,
        )
        self.assertLess(html.index('id="section-market"'), html.index('id="section-scanner"'))
        self.assertLess(html.index('id="section-scanner"'), html.index('id="section-backtest"'))
        self.assertIn("PAGE_SECTIONS = ['market','stock','scanner','alerts','backtest']", html)

    def test_html_clarifies_global_days_vs_foreign_ranking(self):
        html = dashboard.HTML
        self.assertIn("全域天數影響指數／籌碼／個股圖；外資排行用自己的日期區間。", html)
        self.assertIn("此區間只控制排行，與上方全域天數無關", html)
        self.assertIn("id=\"days\"", html)
        self.assertIn("id=\"top-preset\"", html)

    def test_html_broker_branch_shell_is_separate_from_t86(self):
        html = dashboard.HTML
        self.assertIn("id=\"broker-branch-card\"", html)
        self.assertIn("id=\"bb-title\"", html)
        self.assertIn("熱門股分點動向", html)
        self.assertIn("買超分點 Top", html)
        self.assertIn("賣超分點 Top", html)
        self.assertIn("尚未接上 FinMind token", html)
        self.assertIn("示範資料（本機 fixture）", html)
        self.assertIn("依成交額前 N 檔彙總，非全市場", html)
        self.assertIn("/api/broker_branch/top", html)
        self.assertIn("/api/broker_branch/freshness", html)
        self.assertIn("function loadBrokerBranch(", html)
        self.assertIn("loadBrokerBranch();", html)
        self.assertIn("id=\"bb-preset\"", html)
        self.assertIn("id=\"bb-empty\"", html)
        self.assertIn("id=\"bb-lists\"", html)
        self.assertIn("id=\"bb-fresh\"", html)
        self.assertIn("淨額(張)", html)
        self.assertIn("分點（名稱＋代號）", html)
        self.assertIn(".bb-lists{grid-template-columns:1fr}", html)
        self.assertIn(".bb-list{max-height:280px}", html)
        self.assertIn(".bb-lists[hidden],.bb-warn[hidden],.bb-empty[hidden]{display:none!important}", html)
        self.assertIn("此區間只控制熱門股分點，與上方外資排行、全域天數無關", html)
        self.assertLess(html.index("外資買賣超排行"), html.index("id=\"broker-branch-card\""))
        self.assertLess(html.index("id=\"c-buy\""), html.index("id=\"broker-branch-card\""))
        bb_card = html[html.index("id=\"broker-branch-card\""):html.index("id=\"section-stock\"")]
        self.assertNotIn("<canvas", bb_card)
        self.assertNotIn("ticker-link", bb_card)
        self.assertNotIn("全市場分點", bb_card)
        self.assertNotIn("selectStock(", bb_card)
        self.assertIn("bbSafeTitle", html)
        self.assertIn("empty_awaiting_token", html)
        title_tag = html[html.index("id=\"bb-title\""):html.index("id=\"bb-title\"") + 40]
        self.assertIn("熱門股分點動向", title_tag)

    def test_pm_locked_copy_gates_issue_55(self):
        """PM copy/gates for #55: hot-N title, token empty, not T86, not 全市場."""
        html = dashboard.HTML
        card = html[html.index("id=\"broker-branch-card\""):html.index("id=\"section-stock\"")]
        self.assertIn(">熱門股分點動向</h3>", card)
        self.assertIn("依成交額前 N 檔彙總，非全市場", card)
        self.assertIn("尚未接上 FinMind token", card)
        self.assertIn("empty_awaiting_token", html)
        self.assertIn("買超分點 Top", card)
        self.assertIn("賣超分點 Top", card)
        self.assertIn("台灣時間", html)
        self.assertIn("21:00", card)
        self.assertLess(html.index("外資買賣超排行"), html.index("id=\"broker-branch-card\""))
        self.assertIn(".bb-lists{grid-template-columns:1fr}", html)
        self.assertNotIn("全市場分點", card)
        self.assertNotIn("全市場分點買賣超", card)
        self.assertNotIn("api.finmindtrade.com", html)
        self.assertNotIn("live_ingest: true", html)
        from market import broker_branch
        self.assertFalse(broker_branch.ingest_status({}).get("live_ingest"))
        self.assertTrue(broker_branch.ingest_status({"FINMIND_TOKEN": "x"}).get("live_ingest"))
        self.assertEqual(broker_branch.market_title("empty"), "熱門股分點動向")
        self.assertEqual(broker_branch.market_title("hot_n"), "熱門股分點動向")
        self.assertNotIn("全市場", broker_branch.market_title("empty"))
        self.assertNotIn("全市場", broker_branch.market_title("hot_n"))

    def test_html_broker_branch_drill_issue_56(self):
        html = dashboard.HTML
        card = html[html.index("id=\"broker-branch-card\""):html.index("id=\"section-stock\"")]
        self.assertIn("id=\"bb-drill\"", card)
        self.assertIn("id=\"bb-drill-title\"", card)
        self.assertIn("id=\"bb-drill-empty\"", card)
        self.assertIn("id=\"bb-drill-list\"", card)
        self.assertIn("id=\"bb-drill-hint\"", card)
        self.assertIn("此切片未支援", card)
        self.assertIn("熱門前 N 檔內的當日貢獻標的", card)
        self.assertIn("function openBbDrill(", html)
        self.assertIn("function closeBbDrill(", html)
        self.assertIn("function renderBbDrillList(", html)
        self.assertIn("/api/broker_branch/broker?broker_id=", html)
        self.assertIn("BB_DRILL_UNSUPPORTED", html)
        self.assertIn("BB_DRILL_EMPTY", html)
        self.assertIn("BB_DRILL_ERROR", html)
        self.assertIn("renderBbList('bb-buy', top.buy||[], {clickable:true})", html)
        self.assertIn("renderBbList('bb-sell', top.sell||[], {clickable:true})", html)
        self.assertIn("renderBbList('sbb-buy', buy)", html)
        self.assertIn("renderBbList('sbb-sell', sell)", html)
        self.assertIn("if(bbSelectedDays()>1)", html)
        self.assertIn("loadBrokerBranch();", html)
        self.assertIn("買超分點 Top", card)
        self.assertIn("賣超分點 Top", card)
        self.assertIn("熱門股分點動向", card)
        self.assertNotIn("全市場分點", card)
        self.assertNotIn("全市場分點買賣超", card)
        self.assertNotIn("selectStock(", card)
        self.assertNotIn("ticker-link", card)
        stock = html[html.index('id="section-stock"'):html.index('id="section-scanner"')]
        self.assertNotIn("/api/broker_branch/broker", stock)
        self.assertNotIn("id=\"bb-drill\"", stock)

    def test_pm_locked_copy_gates_issue_56(self):
        """PM copy/gates for #56: hot-N drill, unsupported/empty, no fake ranks."""
        html = dashboard.HTML
        card = html[html.index("id=\"broker-branch-card\""):html.index("id=\"section-stock\"")]
        self.assertIn(">熱門股分點動向</h3>", card)
        self.assertIn("依成交額前 N 檔彙總，非全市場", card)
        self.assertIn("此切片未支援", card)
        self.assertIn("BB_DRILL_UNSUPPORTED = '此切片未支援。下鑽只看當日已入庫熱門前 N 檔", html)
        self.assertIn("BB_DRILL_EMPTY = '此分點在熱門前 N 檔內沒有貢獻標的", html)
        self.assertIn("BB_DRILL_ERROR = '無法載入此分點標的列表", html)
        self.assertIn("熱門股貢獻標的", html)
        self.assertIn("買進(張)", html)
        self.assertIn("賣出(張)", html)
        self.assertIn("淨額(張)", html)
        self.assertIn("分點（名稱＋代號）", html)
        self.assertIn("/api/broker_branch/top", html)
        self.assertIn("/api/broker_branch/broker?broker_id=", html)
        self.assertIn("empty_awaiting_token", html)
        self.assertIn("尚未接上 FinMind token", card)
        self.assertIn(".bb-row-click", html)
        self.assertIn(".bb-drill[hidden]", html)
        self.assertIn(".bb-lists{grid-template-columns:1fr}", html)
        self.assertNotIn("全市場分點", card)
        self.assertNotIn("全市場分點買賣超", card)
        self.assertNotIn("api.finmindtrade.com", html)
        self.assertNotIn("live_ingest: true", html)
        self.assertNotIn("selectStock(", card)
        from market import broker_branch
        self.assertEqual(broker_branch.market_title("empty"), "熱門股分點動向")
        self.assertEqual(broker_branch.market_title("hot_n"), "熱門股分點動向")
        self.assertNotIn("全市場", broker_branch.market_title("empty"))
        self.assertNotIn("全市場", broker_branch.market_title("hot_n"))

    def test_html_stock_broker_branch_shell_issue_57(self):
        html = dashboard.HTML
        stock = html[html.index('id="section-stock"'):html.index('id="section-scanner"')]
        self.assertIn('id="stock-broker-branch-card"', stock)
        self.assertIn(">券商分點買賣超</h3>", stock)
        self.assertIn("買超分點 Top", stock)
        self.assertIn("賣超分點 Top", stock)
        self.assertIn("id=\"sbb-empty\"", stock)
        self.assertIn("id=\"sbb-lists\"", stock)
        self.assertIn("id=\"sbb-fresh\"", stock)
        self.assertIn("id=\"sbb-date\"", stock)
        self.assertIn("請先選股", stock)
        self.assertIn("?stock=", stock)
        self.assertIn("熱門前 N", stock)
        self.assertIn("非全市場", stock)
        self.assertIn("21:00", stock)
        self.assertIn("不是 T86", stock)
        self.assertIn("尚未接上 FinMind token", html)
        self.assertIn("/api/broker_branch/stock?id=", html)
        self.assertIn("function loadStockBrokerBranch(", html)
        self.assertIn("loadStock(); loadStockBrokerBranch();", html)
        self.assertIn("if(stockId) loadStock();", html)
        self.assertIn("loadStockBrokerBranch();", html)
        self.assertIn("sbbSafeTitle", html)
        self.assertIn("empty_awaiting_token", html)
        self.assertIn("SBB_EMPTY_NODATA", html)
        self.assertIn("SBB_EMPTY_PICK", html)
        self.assertIn("BB_EMPTY_TOKEN", html)
        self.assertLess(stock.index('id="stock-lookup"'), stock.index('id="stock-broker-branch-card"'))
        self.assertNotIn("<canvas", stock[stock.index("id=\"stock-broker-branch-card\""):])
        self.assertNotIn("ticker-link", stock[stock.index("id=\"stock-broker-branch-card\""):])
        self.assertNotIn("/api/broker_branch/broker", stock)
        self.assertNotIn("全市場分點", stock)
        self.assertNotIn("全市場分點買賣超", stock)
        self.assertNotIn("api.finmindtrade.com", html)
        self.assertNotIn("live_ingest: true", html)

    def test_pm_locked_copy_gates_issue_57(self):
        """PM copy/gates for #57: stock-tab card, token empty, hot-N empty, no 全市場."""
        html = dashboard.HTML
        stock = html[html.index('id="section-stock"'):html.index('id="section-scanner"')]
        card = stock[stock.index('id="stock-broker-branch-card"'):]
        self.assertIn(">券商分點買賣超</h3>", card)
        self.assertIn("該檔讀已入庫熱門前 N 列", card)
        self.assertIn("非全市場", card)
        self.assertIn("FinMind 分點（約 21:00）", card)
        self.assertIn("不是證交所 T86 外資排行", card)
        self.assertIn("請先選股", card)
        self.assertIn("?stock=", card)
        self.assertIn("熱門前 N 檔才會入庫", html)
        self.assertIn("不是空白圖表", html)
        self.assertIn("尚未接上 FinMind token", html)
        self.assertIn("empty_awaiting_token", html)
        self.assertIn("function sbbEmptyCopy(", html)
        self.assertIn("if(!mode || mode==='empty_awaiting_token') return BB_EMPTY_TOKEN;", html)
        self.assertIn("return SBB_EMPTY_NODATA;", html)
        self.assertIn("示範資料（本機 fixture）", card)
        self.assertIn("買超分點 Top", card)
        self.assertIn("賣超分點 Top", card)
        self.assertIn(".bb-lists{grid-template-columns:1fr}", html)
        self.assertNotIn("全市場分點", card)
        self.assertNotIn("全市場分點買賣超", card)
        self.assertNotIn("/api/broker_branch/broker", card)
        self.assertNotIn("api.finmindtrade.com", html)
        self.assertNotIn("live_ingest: true", html)
        from market import broker_branch
        self.assertFalse(broker_branch.ingest_status({}).get("live_ingest"))
        self.assertEqual(broker_branch.market_title("single_stock"), "個股分點買賣超")
        self.assertNotIn("全市場", broker_branch.market_title("single_stock"))

    def test_js_hash_and_selectstock_switch_sections(self):
        html = dashboard.HTML
        self.assertIn("const PAGE_SECTIONS", html)
        self.assertIn("function parseSectionHash(", html)
        self.assertIn("function resolveSection(", html)
        self.assertIn("function showSection(", html)
        self.assertIn("addEventListener('hashchange'", html)
        self.assertIn("parseStockQuery(location.search)", html)
        self.assertIn("parseScannerQuery()", html)
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
        self.assertIn(">掃描</a>", html)
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
        self.assertIn("/api/backtest/stock", text)
        self.assertIn("stock_daily", text)
        self.assertIn("個股小時／日內路徑", text)
        self.assertIn("個股 pattern", text)
        self.assertIn("不自動執行", text)
        self.assertIn("回測此訊號", text)
        self.assertIn("與日內／隔夜／波段不同層", text)
        self.assertIn("#scanner", text)
        self.assertIn("掃描", text)
        self.assertIn("/api/scanner/chip_zscore", text)
        self.assertIn("/api/scanner/broker_main_force", text)
        self.assertIn("stockalert.sc.watchlist.v1", text)
        self.assertIn("一鍵套用", text)
        self.assertIn("?sc=", text)
        self.assertIn("過長", text)


class DashboardScannerScatterTests(unittest.TestCase):
    def test_scanner_tab_is_separate_workbench(self):
        html = dashboard.HTML
        self.assertIn('id="tab-scanner"', html)
        self.assertIn(">掃描</a>", html)
        self.assertIn('id="section-scanner"', html)
        self.assertIn('id="scanner-workbench"', html)
        self.assertIn("多檔橫向比較工作台", html)
        self.assertIn("不是</b>個股分頁", html)
        self.assertIn("不是回測「個股 pattern」宇宙", html)
        self.assertIn("也不是全市場一次掃描", html)
        self.assertNotIn(
            'id="section-scanner"',
            html[html.find('class="bt-universe"'):html.find('id="bt-index-panel"')],
        )
        nav = html[html.find('class="page-nav"'):html.find('id="section-market"')]
        self.assertIn(">掃描</a>", nav)
        self.assertNotIn("個股 pattern", nav)

    def test_scanner_wires_chip_zscore_search_and_selectstock(self):
        html = dashboard.HTML
        self.assertIn("/api/scanner/chip_zscore?", html)
        self.assertIn("function loadScanner(", html)
        self.assertIn("function renderScannerChart(", html)
        self.assertIn("function addScannerPick(", html)
        self.assertIn("function submitScannerSearch(", html)
        self.assertIn("fetchStockHits(q)", html)
        self.assertIn("/api/stocks", html)
        self.assertIn("selectStock(pt.stock_id, pt.stock_name||'')", html)
        self.assertIn("selectStock(tr.dataset.ticker, tr.dataset.name||'')", html)
        self.assertIn('id="sc-x"', html)
        self.assertIn('id="sc-y"', html)
        self.assertIn('id="sc-window"', html)
        self.assertIn('id="sc-min-periods"', html)
        self.assertIn('id="sc-asof"', html)
        self.assertIn('id="sc-empty"', html)
        self.assertIn('id="sc-loading"', html)
        self.assertIn('id="sc-error"', html)
        self.assertIn('id="sc-insufficient"', html)
        self.assertIn("至少 2 檔", html)
        self.assertIn("載入中…", html)
        self.assertIn("樣本不足", html)
        self.assertIn("沒有假資料", html)
        self.assertIn("credentials: 'same-origin'", html)
        self.assertIn("{key:'foreign_net_z', label:'外資買賣超 z'}", html)
        self.assertIn("{key:'trust_net_z', label:'投信買賣超 z'}", html)
        self.assertIn("{key:'dealer_net_z', label:'自營商買賣超 z'}", html)
        self.assertIn("{key:'close', label:'收盤價'}", html)
        self.assertIn("{key:'volume', label:'成交量'}", html)
        self.assertIn("{key:'turnover', label:'成交金額'}", html)
        self.assertIn("type:'scatter'", html)
        self.assertIn("窄螢幕請用下方表格點進個股", html)
        self.assertIn("scPicks.length < 2", html)
        src = Path(__file__).resolve().parents[1].joinpath("web/dashboard.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stock_chips_daily", src)
        self.assertIn("/api/scanner/chip_zscore", src)


class DashboardScannerWatchlistTests(unittest.TestCase):
    """#81: local watchlist → same sc-picks / chip_zscore path; no second fetch."""

    def test_watchlist_ui_and_localstorage_wiring(self):
        html = dashboard.HTML
        self.assertIn('id="sc-watch"', html)
        self.assertIn('id="sc-watch-list"', html)
        self.assertIn('id="sc-watch-add"', html)
        self.assertIn('id="sc-watch-apply"', html)
        self.assertIn('id="sc-watch-save"', html)
        self.assertIn('id="sc-watch-clear"', html)
        self.assertIn('id="sc-watch-msg"', html)
        self.assertIn(">加入追蹤</button>", html)
        self.assertIn(">套用到掃描</button>", html)
        self.assertIn(">存目前標的</button>", html)
        self.assertIn(">清空追蹤</button>", html)
        self.assertIn("stockalert.sc.watchlist.v1", html)
        self.assertIn("localStorage.getItem(SC_WATCH_KEY)", html)
        self.assertIn("localStorage.setItem(SC_WATCH_KEY", html)
        self.assertIn("function applyScannerWatchlist(", html)
        self.assertIn("function addScannerWatch(", html)
        self.assertIn("function removeScannerWatch(", html)
        self.assertIn("function saveScannerPicksToWatchlist(", html)
        self.assertIn("function clearScannerWatchlist(", html)
        self.assertIn("function submitScannerWatchAdd(", html)
        self.assertIn("function resolveScannerSearch(", html)
        self.assertIn("function scReadWatchlist(", html)
        self.assertIn("function renderScannerWatchlist(", html)
        self.assertIn("renderScannerWatchlist();", html)
        self.assertIn("一鍵套用", html)
        self.assertIn("重整後仍在", html)
        self.assertIn("也可從追蹤清單一鍵套用", html)
        scanner = html[html.index('id="section-scanner"'):html.index('id="section-alerts"')]
        self.assertNotIn("stockalert.bt.presets.v1", scanner)
        watch_js = html[html.index("const SC_WATCH_KEY"):html.index("function scHideChart(")]
        self.assertNotIn("stockalert.bt.presets.v1", watch_js)
        self.assertNotIn("BT_PRESET_KEY", watch_js)
        src = Path(__file__).resolve().parents[1].joinpath("web/dashboard.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stock_chips_daily", src)
        self.assertNotIn("watchlist", src)

    def test_apply_reuses_picks_and_single_chip_zscore(self):
        html = dashboard.HTML
        self.assertEqual(html.count("j('/api/scanner/chip_zscore?"), 1)
        apply = html[html.index("function applyScannerWatchlist("):html.index("function saveScannerPicksToWatchlist(")]
        self.assertIn("scPicks = store.tickers", apply)
        self.assertIn("renderScannerPicks()", apply)
        self.assertIn("scPicks.length >= 2", apply)
        self.assertIn("loadScanner()", apply)
        self.assertNotIn("/api/scanner/chip_zscore", apply)
        self.assertNotIn("await j(", apply)
        add = html[html.index("function addScannerWatch("):html.index("function removeScannerWatch(")]
        self.assertIn("fetchStockHits", html[html.index("async function resolveScannerSearch("):html.index("async function submitScannerSearch(")])
        self.assertIn("/api/stocks", html[html.index("async function fetchStockHits("):html.index("function renderStockMenu(")])
        self.assertNotIn("/api/scanner/chip_zscore", add)
        watch_add = html[html.index("async function submitScannerWatchAdd("):html.index("function scHideChart(")]
        self.assertIn("resolveScannerSearch()", watch_add)
        self.assertIn("addScannerWatch(item)", watch_add)
        self.assertNotIn("/api/scanner/chip_zscore", watch_add)
        load = html[html.index("async function loadScanner("):html.index("async function loadAlerts(")]
        self.assertEqual(load.count("/api/scanner/chip_zscore"), 1)
        self.assertIn("scPicks.map(p=>p.id)", load)


class DashboardScannerUrlTests(unittest.TestCase):
    """#82: encode scanner workbench into ?sc= query (same family as ?stock=)."""

    def test_url_state_wiring_and_restore(self):
        html = dashboard.HTML
        self.assertIn("function parseScannerQuery(", html)
        self.assertIn("function applyScannerQuery(", html)
        self.assertIn("function syncScannerUrl(", html)
        self.assertIn("function scEncodeIntoUrl(", html)
        self.assertIn("function scCollectState(", html)
        self.assertIn("function scParamsFromLocation(", html)
        self.assertIn("history.replaceState(null, '', u.pathname + u.search + u.hash)", html)
        self.assertIn("qs.set('sc'", html)
        self.assertIn("qs.set('scx'", html)
        self.assertIn("qs.set('scy'", html)
        self.assertIn("qs.set('scw'", html)
        self.assertIn("qs.set('scmp'", html)
        self.assertIn("qs.set('scd'", html)
        self.assertIn('id="sc-url-note"', html)
        self.assertIn("?sc=", html)
        self.assertIn("複製即可分享或重整還原", html)
        self.assertIn("parseScannerQuery()", html)
        self.assertIn("applyScannerQuery(scFromUrl)", html)
        self.assertIn("if(scShouldLoad) loadScanner()", html)
        self.assertIn("syncScannerUrl()", html)
        boot = html[html.index("(function(){"):html.rindex("})();")]
        self.assertIn("parseScannerQuery()", boot)
        self.assertIn("applyScannerQuery(scFromUrl)", boot)
        self.assertIn("if(scShouldLoad) loadScanner()", boot)
        self.assertIn("renderScannerWatchlist()", boot)
        add = html[html.index("function addScannerPick("):html.index("function removeScannerPick(")]
        self.assertIn("syncScannerUrl()", add)
        self.assertIn("loadScanner()", add)
        self.assertNotIn("/api/scanner/chip_zscore", add)
        apply = html[html.index("function applyScannerWatchlist("):html.index("function saveScannerPicksToWatchlist(")]
        self.assertIn("syncScannerUrl()", apply)
        self.assertIn("loadScanner()", apply)
        self.assertNotIn("/api/scanner/chip_zscore", apply)
        load = html[html.index("async function loadScanner("):html.index("async function loadAlerts(")]
        self.assertEqual(load.count("/api/scanner/chip_zscore"), 1)
        self.assertIn("syncScannerUrl()", load)
        self.assertIn("scRefreshPickNames", load)
        self.assertEqual(html.count("j('/api/scanner/chip_zscore?"), 1)
        src = Path(__file__).resolve().parents[1].joinpath("web/dashboard.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stock_chips_daily", src)
        self.assertNotIn("scEncodeIntoUrl", src)

    def test_overlong_url_drops_optional_then_truncates_tickers(self):
        html = dashboard.HTML
        self.assertIn("const SC_URL_BUDGET = 1800", html)
        self.assertIn("dropOptional", html)
        self.assertIn("dropWindow", html)
        self.assertIn("dropAxes", html)
        self.assertIn("picks.pop()", html)
        self.assertIn("網址過長，已省略部分可選參數。", html)
        self.assertIn("網址過長，已省略部分標的／可選參數。", html)
        self.assertIn("No shortener", html)
        self.assertIn("location.hash.replace(/^#/,'').split('?')[1]", html)
        resolve = html[html.index("function resolveSection("):html.index("function resizeCharts(")]
        self.assertIn("parseStockQuery(location.search)", resolve)
        self.assertIn("parseScannerQuery()", resolve)
        self.assertIn("return 'scanner'", resolve)
        self.assertIn("return 'stock'", resolve)
        self.assertIn("return 'market'", resolve)
        self.assertIn(".split('?')[0]", html[html.index("function parseSectionHash("):html.index("function resolveSection(")])

    def test_url_state_does_not_break_watchlist_or_scatter(self):
        html = dashboard.HTML
        self.assertIn("stockalert.sc.watchlist.v1", html)
        self.assertIn("function applyScannerWatchlist(", html)
        self.assertIn("function renderScannerChart(", html)
        self.assertIn("function renderScannerTable(", html)
        self.assertEqual(html.count("j('/api/scanner/chip_zscore?"), 1)
        self.assertIn("scLastPayload", html)
        watch = html[html.index("const SC_WATCH_KEY"):html.index("function scHideChart(")]
        self.assertNotIn("/api/scanner/chip_zscore", watch)
        self.assertIn("localStorage.getItem(SC_WATCH_KEY)", watch)
        table = html[html.index("function renderScannerTable("):html.index("function renderScannerChart(")]
        self.assertNotIn("/api/scanner/chip_zscore", table)
        self.assertIn("const payload = scLastPayload", table)
        src = Path(__file__).resolve().parents[1].joinpath("web/dashboard.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("watchlist", src)


class DashboardTwRangeTests(unittest.TestCase):
    """#83: scanner + 市場排行 share TwRange; calendar is #73 tw_calendar."""

    def test_html_loads_shared_helper_and_wires_both_surfaces(self):
        html = dashboard.HTML
        self.assertIn('src="/static/tw_range.js"', html)
        self.assertIn("function scReadWindowAsof(", html)
        self.assertIn("function onScWindowPreset(", html)
        self.assertIn("function onScWindowInput(", html)
        self.assertIn("function topReadRange(", html)
        self.assertIn("TwRange.toTopQuery(", html)
        self.assertIn("TwRange.clampWindow(", html)
        self.assertIn("TwRange.snapInput(", html)
        self.assertIn("TwRange.normalize({mode:'custom'", html)
        self.assertIn('id="sc-window-preset"', html)
        self.assertIn(">近 20 日</option>", html)
        self.assertIn(">自訂窗格</option>", html)
        self.assertIn("與外資排行同一套時間模型", html)
        self.assertIn("與掃描窗格同一套模型", html)
        self.assertIn("非交易日會對齊到前一交易日", html)
        load = html[html.index("async function loadScanner("):html.index("async function loadAlerts(")]
        self.assertIn("scReadWindowAsof()", load)
        self.assertIn("TwRange.isYmd(wa.asof)", load)
        self.assertEqual(load.count("/api/scanner/chip_zscore"), 1)
        top = html[html.index("function topReadRange("):html.index("async function loadTop(")]
        self.assertIn("TwRange.toTopQuery(topReadRange())", top)
        self.assertIn("TwRange.snapInput(document.getElementById('top-start'))", top)
        sbb = html[html.index("function sbbDateParam("):html.index("function onSbbDate(")]
        self.assertIn("TwRange.snapInput(document.getElementById('sbb-date'))", sbb)
        self.assertEqual(html.count("j('/api/scanner/chip_zscore?"), 1)
        self.assertIn("stockalert.sc.watchlist.v1", html)
        self.assertIn("function parseScannerQuery(", html)
        self.assertIn("qs.set('scw'", html)
        src = Path(__file__).resolve().parents[1].joinpath("web/dashboard.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stock_chips_daily", src)
        self.assertNotIn("TwRange", src)
        js = Path(__file__).resolve().parents[1].joinpath("web/static/tw_range.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("web/tw_calendar.py", js)
        self.assertIn("taiwan_today", js)
        self.assertIn("#73", js)
        self.assertNotIn("#84", html[html.index("function scReadWindowAsof("):html.index("function topReadRange(")])

    def test_url_and_watchlist_gates_still_hold(self):
        html = dashboard.HTML
        self.assertIn("function syncScannerUrl(", html)
        self.assertIn("function applyScannerWatchlist(", html)
        self.assertEqual(html.count("j('/api/scanner/chip_zscore?"), 1)
        parse = html[html.index("function parseScannerQuery("):html.index("function applyScannerQuery(")]
        self.assertIn("TwRange.clampWindow(qs.get('scw')", parse)
        self.assertIn("TwRange.onOrBefore(asofRaw)", parse)
        collect = html[html.index("function scCollectState("):html.index("function scReadWindowAsof(")]
        self.assertIn("scReadWindowAsof()", collect)
        self.assertIn("wa.window", collect)
        self.assertIn("wa.asof", collect)


class DashboardScannerOverlayTests(unittest.TestCase):
    """#84: second price-overlay chart; scatter / chip_zscore path stays one fetch."""

    def test_overlay_is_second_chart_reusing_picks_and_stock_ohlc(self):
        html = dashboard.HTML
        self.assertIn('src="/static/sc_overlay.js"', html)
        self.assertIn('id="sc-overlay-panel"', html)
        self.assertIn('id="c-scanner-overlay"', html)
        self.assertIn('id="sc-ov-legend"', html)
        self.assertIn('id="sc-ov-mode"', html)
        self.assertIn(">正規化（首日=100）</option>", html)
        self.assertIn(">絕對價</option>", html)
        self.assertIn(">股價疊圖</h3>", html)
        self.assertIn("function loadScannerOverlay(", html)
        self.assertIn("function renderScannerOverlay(", html)
        self.assertIn("function removeOverlaySeries(", html)
        self.assertIn("function restoreOverlaySeries(", html)
        self.assertIn("function scHideOverlay(", html)
        self.assertIn("function renderOverlayLegend(", html)
        self.assertIn("/api/stock_ohlc?id=", html)
        self.assertIn("從疊圖移除", html)
        self.assertIn("不改掃描標的", html)
        self.assertIn("type:'line'", html[html.index("function renderScannerOverlay("):html.index("async function loadScannerOverlay(")])
        self.assertIn("legend:{display:true}", html[html.index("function renderScannerOverlay("):html.index("async function loadScannerOverlay(")])
        load = html[html.index("async function loadScanner("):html.index("async function loadAlerts(")]
        self.assertIn("renderScannerChart()", load)
        self.assertIn("loadScannerOverlay()", load)
        self.assertEqual(load.count("/api/scanner/chip_zscore"), 1)
        self.assertIn("/api/stock_ohlc?id=", load)
        self.assertNotIn("renderScannerTable()", load)
        ov = html[html.index("async function loadScannerOverlay("):html.index("async function loadAlerts(")]
        self.assertIn("/api/stock_ohlc?id=", ov)
        self.assertNotIn("/api/scanner/chip_zscore", ov)
        self.assertIn("ScOverlay.sliceToWindow", ov)
        self.assertIn("scPicks.map", ov)
        self.assertEqual(html.count("j('/api/scanner/chip_zscore?"), 1)
        self.assertIn("type:'scatter'", html[html.index("function renderScannerChart("):html.index("async function loadScanner(")])
        src = Path(__file__).resolve().parents[1].joinpath("web/dashboard.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stock_chips_daily", src)
        self.assertIn("/api/stock_ohlc", src)
        scanner = html[html.index('id="section-scanner"'):html.index('id="section-alerts"')]
        self.assertIn('id="c-scanner"', scanner)
        self.assertIn('id="c-scanner-overlay"', scanner)
        stock = html[html.index('id="section-stock"'):html.index('id="section-scanner"')]
        self.assertNotIn("c-scanner-overlay", stock)
        self.assertNotIn("sc-overlay-panel", stock)

    def test_remove_series_does_not_touch_picks_or_chip_zscore(self):
        html = dashboard.HTML
        remove = html[html.index("function removeOverlaySeries("):html.index("function restoreOverlaySeries(")]
        self.assertIn("scOvHidden[id]", remove)
        self.assertIn("renderScannerOverlay()", remove)
        self.assertNotIn("removeScannerPick", remove)
        self.assertNotIn("scPicks.splice", remove)
        self.assertNotIn("loadScanner()", remove)
        self.assertNotIn("/api/scanner/chip_zscore", remove)
        self.assertNotIn("await j(", remove)
        restore = html[html.index("function restoreOverlaySeries("):html.index("function renderOverlayLegend(")]
        self.assertIn("scOvHidden = {}", restore)
        self.assertIn("renderScannerOverlay()", restore)
        self.assertNotIn("/api/scanner/chip_zscore", restore)
        empty = html[html.index("function scShowEmpty("):html.index("function scShowLoading(")]
        self.assertIn("scHideOverlay()", empty)
        loading = html[html.index("function scShowLoading("):html.index("function scShowError(")]
        self.assertIn("scHideOverlay()", loading)
        self.assertIn("stockalert.sc.watchlist.v1", html)
        self.assertIn("function syncScannerUrl(", html)
        watch = html[html.index("const SC_WATCH_KEY"):html.index("function scHideChart(")]
        self.assertNotIn("/api/stock_ohlc", watch)
        self.assertNotIn("/api/scanner/chip_zscore", watch)


class DashboardScannerTableTests(unittest.TestCase):
    """#80: sortable/filterable results table on the same chip_zscore payload."""

    def test_table_is_companion_to_scatter_same_payload(self):
        html = dashboard.HTML
        self.assertIn('id="sc-list"', html)
        self.assertIn('id="sc-table-panel"', html)
        self.assertIn(">掃描結果</h3>", html)
        self.assertIn("function renderScannerTable(", html)
        self.assertIn("function sortScannerTable(", html)
        self.assertIn("function scSortRows(", html)
        self.assertIn("function onScannerFilter(", html)
        self.assertIn("一次查詢", html)
        self.assertIn("與散布圖同一批資料", html)
        self.assertIn("沒有第二條資料徑", html)
        self.assertIn("scLastPayload", html)
        self.assertIn("renderScannerTable()", html)
        self.assertEqual(html.count("j('/api/scanner/chip_zscore?"), 1)
        load = html[html.index("async function loadScanner("):html.index("async function loadAlerts(")]
        self.assertIn("scLastPayload = r", load)
        self.assertIn("renderScannerChart()", load)
        self.assertNotIn("renderScannerTable()", load)
        self.assertEqual(load.count("/api/scanner/chip_zscore"), 1)
        chart = html[html.index("function renderScannerChart("):html.index("async function loadScanner(")]
        self.assertIn("renderScannerTable()", chart)
        table = html[html.index("function renderScannerTable("):html.index("function renderScannerChart(")]
        self.assertIn("const payload = scLastPayload", table)
        self.assertNotIn("/api/scanner/chip_zscore", table)
        self.assertNotIn("await j(", table)
        sort = html[html.index("function sortScannerTable("):html.index("function renderScannerTable(")]
        self.assertIn("renderScannerTable()", sort)
        self.assertNotIn("/api/scanner/chip_zscore", sort)
        self.assertNotIn("await j(", sort)

    def test_sortable_key_columns_and_filter(self):
        html = dashboard.HTML
        self.assertIn("{key:'stock_id', label:'代號'", html)
        self.assertIn("{key:'stock_name', label:'名稱'", html)
        self.assertIn("add('close', '收盤價', true)", html)
        self.assertIn("add('volume', '成交量', true)", html)
        self.assertIn("add(xKey, scAxisLabel(xKey), true)", html)
        self.assertIn("add(yKey, scAxisLabel(yKey), true)", html)
        self.assertIn("data-sort=", html)
        self.assertIn("aria-sort=", html)
        self.assertIn("button.sc-sort", html)
        self.assertIn("scSortKey", html)
        self.assertIn("scSortDir", html)
        self.assertIn('id="sc-filter-q"', html)
        self.assertIn('id="sc-filter-status"', html)
        self.assertIn("篩選代號／名稱", html)
        self.assertIn(">可畫圖</option>", html)
        self.assertIn(">樣本不足</option>", html)
        self.assertIn("沒有符合篩選的列", html)
        self.assertIn("selectStock(tr.dataset.ticker, tr.dataset.name||'')", html)
        self.assertIn("function scShowLoading(", html)
        self.assertIn("function scClearTable(", html)
        self.assertIn("scShowLoading()", html)
        self.assertIn("scClearTable()", html)
        for state_id in ("sc-empty", "sc-loading", "sc-error", "sc-insufficient"):
            self.assertIn(f'id="{state_id}"', html)
        self.assertIn("才會載入掃描散布圖與結果表格", html)
        self.assertIn("載入中…", html)
        self.assertIn("掃描資料載入失敗", html)
        src = Path(__file__).resolve().parents[1].joinpath("web/dashboard.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stock_chips_daily", src)


class DashboardStockBacktestUITests(unittest.TestCase):
    def test_universe_switch_is_separate_from_taiex_blocks(self):
        html = dashboard.HTML
        self.assertIn(">大盤策略</button>", html)
        self.assertIn(">個股 pattern</button>", html)
        self.assertIn('id="bt-index-panel"', html)
        self.assertIn('id="bt-stock-panel"', html)
        self.assertIn('id="bt-stock-form"', html)
        self.assertIn("/api/backtest/stock", html)
        self.assertIn("function runStockBacktest(", html)
        self.assertIn("function btShowUniverse(", html)
        self.assertIn("fetchStockHits(q)", html)
        self.assertIn('id="bt-sid"', html)
        self.assertIn('id="bt-stock-pattern"', html)
        self.assertIn(">上影線反轉</option>", html)
        self.assertIn(">Inside Day</option>", html)
        self.assertIn("持有 N 交易日", html)
        self.assertIn("日K；非大盤回測", html)
        self.assertIn("個股 · '+id+' · '+label", html)
        self.assertIn("尚未執行。先選標的與 pattern，再按執行。", html)
        self.assertIn("這段日K沒有觸發", html)
        self.assertIn("stockalert.bt.presets.v1", html)
        self.assertIn("不寫 stockalert.bt.presets.v1", html)
        self.assertIn("data-bt-fold=\"stock-ticker\"", html)
        self.assertIn("data-bt-fold=\"stock-pattern\"", html)
        self.assertIn("data-bt-fold=\"stock-exit\"", html)
        self.assertIn("低點≤停損價", html)
        self.assertIn("保守假設先停損", html)
        self.assertIn("與日內／隔夜／波段不同層", html)
        self.assertIn("不是組合部位", html)
        self.assertIn("不是全市場一次回測", html)
        self.assertIn(">型態</summary>", html)
        self.assertIn("function prefillStockBacktest(", html)
        self.assertIn("目前是個股 pattern（stock_daily 日K）", html)
        self.assertIn("目前是大盤指數積木", html)
        uni = html[html.index('class="bt-universe"'):html.index('id="bt-index-panel"')]
        self.assertNotIn('name="bt-mode"', uni)
        index = html[html.index('id="bt-index-panel"'):html.index('id="bt-stock-panel"')]
        self.assertIn('name="bt-mode"', index)
        self.assertIn("新增積木", index)
        self.assertIn("參數為第二層", index)
        stock = html[html.index('id="bt-stock-panel"'):html.index("id=\"bt-stock-results\"")]
        self.assertNotIn("localStorage.setItem(BT_PRESET_KEY", stock)
        self.assertNotIn("btBuildBlocks()", stock)
        self.assertNotIn("stockalert.bt.presets.v1", stock)
        self.assertNotIn("新增積木", stock)
        self.assertNotIn('name="bt-mode"', stock)
        self.assertLess(stock.index('id="bt-sid"'), stock.index('id="bt-stock-pattern"'))
        self.assertLess(stock.index('id="bt-stock-pattern"'), stock.index('id="bt-stock-hold"'))
        self.assertLess(stock.index('id="bt-stock-hold"'), stock.index('id="bt-stock-summary"'))
        self.assertLess(stock.index('id="bt-stock-summary"'), stock.index("runStockBacktest()"))


class DashboardAlertBacktestPrefillTests(unittest.TestCase):
    """#52: alert row → #backtest prefill; no auto POST; keep #50/#51 IA."""

    def test_cta_and_deep_link_wiring(self):
        html = dashboard.HTML
        self.assertIn(">回測此訊號<", html)
        self.assertIn("alert-bt-btn", html)
        self.assertIn("data-bt-stock=", html)
        self.assertIn("data-bt-pattern=", html)
        self.assertIn("function alertBacktestHref(", html)
        self.assertIn("function parseBacktestQuery(", html)
        self.assertIn("function prefillStockBacktest(", html)
        self.assertIn("function applyBacktestPrefillFromLocation(", html)
        self.assertIn("function onAlertBacktestClick(", html)
        self.assertIn("#backtest", html)
        self.assertIn("qs.set('stock'", html)
        self.assertIn("qs.set('pattern'", html)
        self.assertIn("btShowUniverse('stock')", html)
        self.assertIn("此 pattern 尚未支援回測", html)
        self.assertIn('id="bt-stock-prefill-msg"', html)
        self.assertIn('id="bt-stock-run"', html)
        self.assertIn("applyBacktestPrefillFromLocation()", html)
        load = html[html.index("async function loadAlerts("):html.index("async function loadPerformance(")]
        self.assertIn(">回測此訊號<", load)
        self.assertIn("alertBacktestHref(", load)
        self.assertIn("onAlertBacktestClick", load)
        self.assertNotIn("/api/backtest/stock", load)
        self.assertNotIn("runStockBacktest()", load)

    def test_prefill_does_not_auto_run_or_touch_index_presets(self):
        html = dashboard.HTML
        prefill = html[html.index("function prefillStockBacktest("):html.index("function applyBacktestPrefillFromLocation(")]
        self.assertIn("btShowUniverse('stock')", prefill)
        self.assertIn("btPickStock(", prefill)
        self.assertIn("此 pattern 尚未支援回測", prefill)
        self.assertIn("已預填，尚未執行", prefill)
        self.assertNotIn("runStockBacktest()", prefill)
        self.assertNotIn("fetch(", prefill)
        self.assertNotIn("/api/backtest", prefill)
        self.assertNotIn("localStorage.setItem(BT_PRESET_KEY", prefill)
        self.assertNotIn("btBuildBlocks()", prefill)
        apply = html[html.index("function applyBacktestPrefillFromLocation("):html.index("function onAlertBacktestClick(")]
        self.assertIn("parseBacktestQuery()", apply)
        self.assertIn("prefillStockBacktest(", apply)
        self.assertNotIn("runStockBacktest()", apply)
        self.assertNotIn("fetch(", apply)
        run = html[html.index("async function runStockBacktest("):html.index("function loadAll(")]
        self.assertIn("此 pattern 尚未支援回測", run)
        self.assertIn("btPrefillUnsupported", run)
        hash_l = html[html.index("window.addEventListener('hashchange'"):html.index("function setChartEmpty(")]
        self.assertIn("applyBacktestPrefillFromLocation()", hash_l)
        boot = html[html.rindex("(function(){"):]
        self.assertIn("applyBacktestPrefillFromLocation()", boot)
        alerts = html[html.index('id="section-alerts"'):html.index('id="section-backtest"')]
        self.assertIn("「回測此訊號」", alerts)
        self.assertIn("不自動執行", alerts)
        stock = html[html.index('id="bt-stock-panel"'):html.index("id=\"bt-stock-results\"")]
        self.assertNotIn("stockalert.bt.presets.v1", stock)
        self.assertNotIn("localStorage.setItem(BT_PRESET_KEY", stock)

    def test_unsupported_and_bad_data_guards(self):
        html = dashboard.HTML
        self.assertIn("function btNormalizePattern(", html)
        self.assertIn("BT_REPLAY_PATTERNS", html)
        self.assertIn("function btEnsureUnsupportedOption(", html)
        self.assertIn("btSetStockRunEnabled(false)", html)
        self.assertIn("catch(e)", html[html.index("function prefillStockBacktest("):html.index("function applyBacktestPrefillFromLocation(")])
        self.assertIn("parseStockId(ticker)", html)
        parse = html[html.index("function parseBacktestQuery("):html.index("function btNormalizePattern(")]
        self.assertIn("catch(e){ return null; }", parse)
        self.assertIn("parseSectionHash() !== 'backtest'", parse)


class DashboardScannerMainForceTests(unittest.TestCase):
    """#101: separate hot-N main-force panel; same tickers+asof, not chip_zscore scatter."""

    def test_panel_is_separate_from_scatter_and_uses_existing_api(self):
        html = dashboard.HTML
        self.assertIn('id="sc-mf-panel"', html)
        self.assertIn('id="sc-mf-title"', html)
        self.assertIn('id="sc-mf-note"', html)
        self.assertIn('id="sc-mf-empty"', html)
        self.assertIn('id="sc-mf-error"', html)
        self.assertIn('id="sc-mf-loading"', html)
        self.assertIn('id="sc-mf-list"', html)
        self.assertIn('id="sc-mf-k"', html)
        self.assertIn(">熱門股分點動向</h3>", html)
        self.assertIn("買超集中度", html)
        self.assertIn("賣超集中度", html)
        self.assertIn("龍頭分點淨額", html)
        self.assertIn("coverage: ", html)
        self.assertIn("coverage: hot_n", html)
        self.assertIn("function loadScannerMainForce(", html)
        self.assertIn("function renderScannerMainForce(", html)
        self.assertIn("function scHideMainForce(", html)
        self.assertIn("function scMfTitle(", html)
        self.assertIn("/api/scanner/broker_main_force?", html)
        self.assertIn("不是籌碼 z-score", html)
        self.assertIn("不是</b>全市場", html)
        self.assertEqual(html.count("j('/api/scanner/chip_zscore?"), 1)
        self.assertEqual(html.count("j('/api/scanner/broker_main_force?"), 1)
        load = html[html.index("async function loadScanner("):html.index("async function loadAlerts(")]
        self.assertIn("loadScannerOverlay()", load)
        self.assertIn("loadScannerMainForce()", load)
        self.assertEqual(load.count("/api/scanner/chip_zscore"), 1)
        self.assertIn("/api/scanner/broker_main_force?", load)
        self.assertNotIn("renderScannerTable()", load)
        mf = html[html.index("function scMfTitle("):html.index("async function loadAlerts(")]
        self.assertIn("/api/scanner/broker_main_force?", mf)
        self.assertNotIn("/api/scanner/chip_zscore", mf)
        self.assertIn("indexOf('全市場')", mf)
        self.assertIn("熱門股分點動向", mf)
        self.assertIn("coverage: ", mf)
        self.assertIn("buy_concentration", mf)
        self.assertIn("sell_concentration", mf)
        self.assertIn("lead_branch_net", mf)
        self.assertIn("in_hot_n", mf)
        title_fn = html[html.index("function scMfTitle("):html.index("function scMfReadK(")]
        self.assertNotIn("全市場分點", title_fn)
        scanner = html[html.index('id="section-scanner"'):html.index('id="section-alerts"')]
        self.assertIn('id="sc-mf-panel"', scanner)
        self.assertIn('id="c-scanner"', scanner)
        panel = scanner[scanner.index('id="sc-mf-panel"'):]
        self.assertIn(">熱門股分點動向</h3>", panel)
        self.assertNotIn("全市場分點", panel)
        self.assertNotIn("全市場分點主力", panel)
        stock = html[html.index('id="section-stock"'):html.index('id="section-scanner"')]
        self.assertNotIn("sc-mf-panel", stock)
        self.assertNotIn("loadScannerMainForce", stock)
        src = Path(__file__).resolve().parents[1].joinpath("web/dashboard.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stock_chips_daily", src)
        self.assertIn("/api/scanner/broker_main_force", src)

    def test_empty_error_and_same_universe_gates(self):
        html = dashboard.HTML
        empty = html[html.index("function scShowEmpty("):html.index("function scShowLoading(")]
        self.assertIn("scHideMainForce()", empty)
        self.assertIn("scHideOverlay()", empty)
        loading = html[html.index("function scShowLoading("):html.index("function scShowError(")]
        self.assertIn("scHideMainForce()", loading)
        err = html[html.index("function scShowError("):html.index("function scAxisValue(")]
        self.assertIn("scHideMainForce()", err)
        load_mf = html[html.index("async function loadScannerMainForce("):html.index("async function loadAlerts(")]
        self.assertIn("scPicks.length < 2", load_mf)
        self.assertIn("scPicks.map(p=>p.id)", load_mf)
        self.assertIn("scReadWindowAsof()", load_mf)
        self.assertIn("TwRange.isYmd(wa.asof)", load_mf)
        self.assertIn("主力指標載入失敗", load_mf)
        self.assertIn("沒有假資料可畫", load_mf)
        self.assertNotIn("/api/scanner/chip_zscore", load_mf)
        self.assertNotIn("/api/scanner/alert_profile", load_mf)
        render = html[html.index("function renderScannerMainForce("):html.index("async function loadScannerMainForce(")]
        self.assertIn("scMfLastPayload", render)
        self.assertNotIn("/api/scanner/chip_zscore", render)
        self.assertNotIn("await j(", render)
        self.assertIn("selectStock(tr.dataset.ticker, tr.dataset.name||'')", render)
        k_change = html[html.index('id="sc-mf-k"'):html.index('id="sc-mf-k"') + 280]
        self.assertIn("loadScannerMainForce()", k_change)
        self.assertNotIn("loadScanner()", k_change)
        self.assertEqual(html.count("j('/api/scanner/chip_zscore?"), 1)
        watch = html[html.index("const SC_WATCH_KEY"):html.index("function scHideChart(")]
        self.assertNotIn("/api/scanner/broker_main_force", watch)
        alert = html[html.index("async function saveScannerAlertProfile("):html.index("function scHideChart(")]
        self.assertNotIn("/api/scanner/broker_main_force", alert)
        self.assertNotIn("loadScannerMainForce()", alert)


class DashboardScannerAlertTests(unittest.TestCase):
    """#86: one saved profile → schedule; UI save does not add a chip_zscore fetch."""

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

    def test_ui_save_panel_reuses_picks_and_single_chip_zscore(self):
        html = dashboard.HTML
        self.assertIn('id="sc-alert"', html)
        self.assertIn('id="sc-alert-field"', html)
        self.assertIn('id="sc-alert-min"', html)
        self.assertIn('id="sc-alert-save"', html)
        self.assertIn(">存成每日告警</button>", html)
        self.assertIn("function saveScannerAlertProfile(", html)
        self.assertIn("function scCollectAlertProfile(", html)
        self.assertIn("function loadScannerAlertProfile(", html)
        self.assertIn("/api/scanner/alert_profile", html)
        self.assertIn("不是 DSL", html)
        self.assertIn("也不下單", html)
        self.assertEqual(html.count("j('/api/scanner/chip_zscore?"), 1)
        save = html[html.index("async function saveScannerAlertProfile("):html.index("function scHideChart(")]
        self.assertIn("method:'POST'", save)
        self.assertIn("/api/scanner/alert_profile", save)
        self.assertNotIn("/api/scanner/chip_zscore", save)
        self.assertNotIn("loadScanner()", save)
        load = html[html.index("async function loadScanner("):html.index("async function loadAlerts(")]
        self.assertEqual(load.count("/api/scanner/chip_zscore"), 1)
        scanner = html[html.index('id="section-scanner"'):html.index('id="section-alerts"')]
        self.assertIn('id="sc-alert"', scanner)
        stock = html[html.index('id="section-stock"'):html.index('id="section-scanner"')]
        self.assertNotIn("sc-alert-save", stock)
        src = Path(__file__).resolve().parents[1].joinpath("web/dashboard.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stock_chips_daily", src)
        self.assertIn("/api/scanner/alert_profile", src)
        self.assertIn("indexOf('scanner_')===0", html)

    def test_get_falls_back_to_repo_file_without_db(self):
        got = dashboard.api_scanner_alert_profile()
        self.assertIn(got["source"], ("file", "empty", "db"))
        self.assertIn("tickers", got["profile"])
        self.assertIn("field", got["profile"])

    def test_post_saves_one_profile_and_rejects_dsl(self):
        saved = dashboard.save_scanner_alert_profile({
            "tickers": ["2330", "2454"],
            "window": 20,
            "field": "foreign_net_z",
            "min": 1.5,
        })
        self.assertTrue(saved["saved"])
        self.assertEqual(saved["source"], "db")
        self.assertEqual(saved["profile"]["tickers"], ["2330", "2454"])
        got = dashboard.api_scanner_alert_profile()
        self.assertEqual(got["source"], "db")
        self.assertEqual(got["profile"]["tickers"], ["2330", "2454"])
        with self.assertRaises(ValueError) as ctx:
            dashboard.save_scanner_alert_profile({
                "tickers": ["2330"],
                "expr": "foreign_net_z > 2",
            })
        self.assertIn("unsupported_condition", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
