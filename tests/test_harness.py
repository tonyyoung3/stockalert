import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from alertsdb import store as db
from harness.agent import run_agent
from harness.config import harness_enabled
from harness.cli import main as harness_main
from harness.models import ModelTurn, ScriptedModel, ToolCall
from harness.tools import (
    check_ticker_pattern,
    default_tools,
    execute_tool,
    flatten_ohlcv,
    normalize_ticker,
)
from tests.test_patterns import _inside_day, _upper_shadow_only


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self.tmp.name) / "test.db")
        db.init_db()

    def tearDown(self):
        db.set_db_path(None)
        self.tmp.cleanup()

    def _seed(self):
        today = date.today()
        old = str(today - timedelta(days=40))
        recent = str(today - timedelta(days=3))
        aid = db.save_alert("2330", "upper_shadow_reversal", old, 100.0)
        db.save_alert("2317", "inside_day", recent, 50.0)
        db.save_performance(aid, str(today), 112.0, 12.0)
        return aid

    def test_normalize_ticker(self):
        self.assertEqual(normalize_ticker("2330.TW"), ("2330", "2330.TW"))
        self.assertEqual(normalize_ticker("2330"), ("2330", "2330.TW"))
        self.assertEqual(normalize_ticker("tsla"), ("TSLA", "TSLA"))

    def test_flatten_multiindex_close(self):
        idx = pd.bdate_range("2026-06-01", periods=3)
        df = pd.DataFrame(
            {("Close", "2330.TW"): [1.0, 2.0, 3.0], ("Open", "2330.TW"): [1.0, 2.0, 3.0]},
            index=idx,
        )
        flat = flatten_ohlcv(df)
        self.assertNotIsInstance(flat.columns, pd.MultiIndex)
        self.assertEqual(float(flat["Close"].iloc[-1]), 3.0)

    def test_list_and_summary_tools(self):
        self._seed()
        tools = default_tools()
        recent = execute_tool("list_recent_alerts", {"days": 7}, tools)
        self.assertEqual(recent["count"], 1)
        self.assertEqual(recent["alerts"][0]["ticker"], "2317")

        history = execute_tool("lookup_alert_history", {"ticker": "2330.TW"}, tools)
        self.assertEqual(history["count"], 1)
        self.assertEqual(history["alerts"][0]["return_pct"], 12.0)

        summary = execute_tool("summarize_performance", {}, tools)
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["avg_return_pct"], 12.0)
        self.assertEqual(summary["pending_28d"], 0)

        pending = execute_tool("list_pending_checks", {}, tools)
        self.assertEqual(pending["pending"], 0)

    def test_unknown_tool_and_bad_pattern(self):
        tools = default_tools()
        self.assertIn("unknown tool", execute_tool("drop_table", {}, tools)["error"])
        err = execute_tool("list_recent_alerts", {"pattern_type": "head_and_shoulders"}, tools)
        self.assertIn("pattern_type", err["error"])

    def test_check_ticker_pattern_uses_injected_feed(self):
        result = check_ticker_pattern("2330", fetch_ohlcv=lambda _symbol: _inside_day())
        self.assertEqual(result["pattern"], "inside_day")
        self.assertEqual(result["ticker"], "2330")

        none = check_ticker_pattern("2330", fetch_ohlcv=lambda _symbol: pd.DataFrame())
        self.assertEqual(none["error"], "insufficient_price_data")

        upper = check_ticker_pattern("2330", fetch_ohlcv=lambda _symbol: _upper_shadow_only())
        self.assertEqual(upper["pattern"], "upper_shadow_reversal")

    def test_scripted_agent_loop(self):
        self._seed()
        model = ScriptedModel(
            [
                ModelTurn(
                    tool_calls=[
                        ToolCall(id="call_1", name="summarize_performance", arguments={}),
                    ]
                ),
                ModelTurn(content="已檢查 1 筆，平均報酬 +12%。"),
            ]
        )
        result = run_agent("績效如何？", model, tools=default_tools())
        self.assertEqual(result.stop_reason, "completed")
        self.assertIn("12%", result.answer)
        self.assertEqual(result.steps[0].tool_name, "summarize_performance")
        self.assertEqual(result.steps[0].tool_result["checked"], 1)

    def test_scripted_agent_hits_max_steps(self):
        model = ScriptedModel(
            [
                ModelTurn(tool_calls=[ToolCall(id="1", name="list_pending_checks", arguments={})]),
                ModelTurn(tool_calls=[ToolCall(id="2", name="list_pending_checks", arguments={})]),
            ]
        )
        result = run_agent("pending?", model, tools=default_tools(), max_steps=2)
        self.assertEqual(result.stop_reason, "max_steps")

    def test_cli_tool_mode(self):
        self._seed()
        rc = harness_main(["--tool", "summarize_performance"])
        self.assertEqual(rc, 0)

    def test_db_list_filters(self):
        self._seed()
        rows = db.list_alerts(ticker="2330")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pattern_type"], "upper_shadow_reversal")
        only_inside = db.list_alerts(pattern_type="inside_day")
        self.assertEqual(len(only_inside), 1)
        self.assertEqual(only_inside[0]["ticker"], "2317")

    def test_empty_question_short_circuits(self):
        result = run_agent("   ", ScriptedModel([]), tools=default_tools())
        self.assertEqual(result.stop_reason, "empty_question")

    def test_harness_enabled_flag(self):
        import os

        old = os.environ.pop("HARNESS_ENABLED", None)
        try:
            self.assertFalse(harness_enabled())
            os.environ["HARNESS_ENABLED"] = "1"
            self.assertTrue(harness_enabled())
        finally:
            if old is None:
                os.environ.pop("HARNESS_ENABLED", None)
            else:
                os.environ["HARNESS_ENABLED"] = old


if __name__ == "__main__":
    unittest.main()
