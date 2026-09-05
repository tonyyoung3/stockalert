"""Fixture tests for the stock daily pattern-replay engine (#50). No network."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from signals.patterns import (
    check_inside_day,
    check_upper_shadow_reversal,
    classify_pattern,
    matches_pattern,
    pattern_on_trailing_window,
)
from tests.test_patterns import (
    _base_df,
    _inside_day,
    _set_bar,
    _upper_shadow_only,
)
from web.stock_backtest import (
    ASSUMPTIONS,
    load_stock_daily_yahoo,
    parse_stock_backtest_request,
    run_stock_backtest,
    run_stock_pattern_replay,
    stock_daily_rows_to_yahoo,
)


def _append_days(df: pd.DataFrame, n: int, close_fn, volume=1_000.0) -> pd.DataFrame:
    """Append ``n`` trading days after ``df``; ``close_fn(k, last_close)`` → close."""
    last_close = float(df["Close"].iloc[-1])
    extras = []
    idx = []
    cursor = df.index[-1]
    for k in range(1, n + 1):
        cursor = cursor + pd.Timedelta(days=1)
        while cursor.weekday() >= 5:
            cursor = cursor + pd.Timedelta(days=1)
        close = float(close_fn(k, last_close))
        extras.append(
            {
                "Open": close - 0.2,
                "High": close + 0.3,
                "Low": close - 0.4,
                "Close": close,
                "Volume": volume,
            }
        )
        idx.append(cursor)
        last_close = close
    return pd.concat([df, pd.DataFrame(extras, index=pd.DatetimeIndex(idx))])


def _signal_then(hold_n: int, close_fn=None, **bar_overrides) -> pd.DataFrame:
    df = _upper_shadow_only()
    if close_fn is None:
        close_fn = lambda k, last: last
    out = _append_days(df, hold_n, close_fn)
    if bar_overrides:
        pos = bar_overrides.pop("pos")
        _set_bar(out, pos, **bar_overrides)
    return out


class SharedPatternChecksTests(unittest.TestCase):
    def test_matches_pattern_reuses_live_checks(self):
        up = _upper_shadow_only()
        inside = _inside_day()
        quiet = _base_df()
        self.assertTrue(matches_pattern(up, "upper_shadow_reversal"))
        self.assertFalse(matches_pattern(up, "inside_day"))
        self.assertTrue(matches_pattern(inside, "inside_day"))
        self.assertTrue(matches_pattern(inside, "upper_shadow_reversal"))
        self.assertIsNone(classify_pattern(quiet))
        self.assertFalse(matches_pattern(quiet, "upper_shadow_reversal"))
        self.assertFalse(matches_pattern(up, "not_a_pattern"))

    def test_trailing_window_ignores_future_bars(self):
        df = _upper_shadow_only()
        i = len(df) - 1
        self.assertTrue(pattern_on_trailing_window(df, i, "upper_shadow_reversal"))
        extra = df.iloc[[-1]].copy()
        extra.index = extra.index + pd.Timedelta(days=3)
        extra["Volume"] = 1e9
        extra["Close"] = 1.0
        extra["High"] = 2.0
        grown = pd.concat([df, extra])
        self.assertTrue(pattern_on_trailing_window(grown, i, "upper_shadow_reversal"))
        self.assertEqual(classify_pattern(df), "upper_shadow_reversal")
        self.assertTrue(check_upper_shadow_reversal(df))
        self.assertFalse(check_inside_day(df))


class ReplayEngineTests(unittest.TestCase):
    def test_trigger_then_hold_n_days(self):
        df = _signal_then(5, close_fn=lambda k, last: 107.0 * (1.02 ** k) if k == 0 else 107.0 * (1.02 ** k))
        # Entry is last of the original frame (107). After 5 up-days close ≈ 107*1.02^5.
        result = run_stock_pattern_replay(
            df, "upper_shadow_reversal", hold_days=5, cost_pct=0
        )
        self.assertGreaterEqual(result["n"], 1)
        self.assertNotIn("no_trigger", result)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "max_hold")
        self.assertEqual(trade["hold_days"], 5)
        entry = trade["entry_price"]
        exit_px = trade["exit_price"]
        self.assertAlmostEqual(exit_px / entry - 1, trade["ret_net_pct"] / 100, places=4)
        self.assertEqual(result["avg_return_pct"], result["ev_pct"])
        self.assertGreater(result["win_rate"], 0)

    def test_stop_hits_high_low_touch(self):
        df = _upper_shadow_only()
        entry = float(df["Close"].iloc[-1])
        df = _append_days(df, 5, lambda k, last: entry)
        # Day +2: low punches through a 3% stop.
        _set_bar(
            df,
            len(df) - 4,
            open_=entry,
            high=entry + 0.5,
            low=entry * 0.96,
            close=entry * 0.99,
        )
        result = run_stock_pattern_replay(
            df, "upper_shadow_reversal", hold_days=5, stop_pct=3, cost_pct=0
        )
        self.assertGreaterEqual(result["n"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertAlmostEqual(trade["exit_price"], entry * 0.97, places=4)
        self.assertLess(trade["ret_net_pct"], 0)
        self.assertLess(trade["hold_days"], 5)

    def test_take_profit_hits_high(self):
        df = _upper_shadow_only()
        entry = float(df["Close"].iloc[-1])
        df = _append_days(df, 5, lambda k, last: entry)
        _set_bar(
            df,
            len(df) - 3,
            open_=entry,
            high=entry * 1.06,
            low=entry * 0.99,
            close=entry * 1.01,
        )
        result = run_stock_pattern_replay(
            df,
            "upper_shadow_reversal",
            hold_days=5,
            take_profit_pct=5,
            cost_pct=0,
        )
        self.assertGreaterEqual(result["n"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "take_profit")
        self.assertAlmostEqual(trade["exit_price"], entry * 1.05, places=4)

    def test_same_day_stop_beats_take_profit(self):
        df = _upper_shadow_only()
        entry = float(df["Close"].iloc[-1])
        df = _append_days(df, 3, lambda k, last: entry)
        _set_bar(
            df,
            len(df) - 3,
            open_=entry,
            high=entry * 1.08,
            low=entry * 0.90,
            close=entry,
        )
        result = run_stock_pattern_replay(
            df,
            "upper_shadow_reversal",
            hold_days=3,
            stop_pct=3,
            take_profit_pct=5,
            cost_pct=0,
        )
        self.assertEqual(result["trades"][0]["exit_reason"], "stop")

    def test_no_trigger_is_explicit(self):
        result = run_stock_pattern_replay(_base_df(40), "upper_shadow_reversal", hold_days=5)
        self.assertEqual(result["n"], 0)
        self.assertTrue(result["no_trigger"])
        self.assertIn("沒有觸發", result["error"])
        self.assertIn("上影線反轉", result["error"])

        inside = run_stock_pattern_replay(_base_df(40), "inside_day", hold_days=5)
        self.assertTrue(inside["no_trigger"])
        self.assertIn("Inside Day", inside["error"])

    def test_inside_day_can_trigger(self):
        df = _append_days(_inside_day(), 5, lambda k, last: last * 1.01)
        result = run_stock_pattern_replay(df, "inside_day", hold_days=5, cost_pct=0)
        self.assertGreaterEqual(result["n"], 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "max_hold")

    def test_unresolved_at_tail(self):
        df = _upper_shadow_only()
        result = run_stock_pattern_replay(df, "upper_shadow_reversal", hold_days=5)
        self.assertEqual(result["n"], 0)
        self.assertIn("尾端", result["error"])
        self.assertGreaterEqual(result["unresolved_trades"], 1)
        self.assertNotIn("no_trigger", result)

    def test_too_few_bars(self):
        df = _base_df(10)
        result = run_stock_pattern_replay(df, "upper_shadow_reversal")
        self.assertEqual(result["n"], 0)
        self.assertIn("22", result["error"])


class RequestAndLoadTests(unittest.TestCase):
    def test_parse_rejects_bad_payload(self):
        with self.assertRaises(ValueError):
            parse_stock_backtest_request({"stock_id": "2330"})
        with self.assertRaises(ValueError):
            parse_stock_backtest_request(
                {"stock_id": "x", "pattern": "upper_shadow_reversal"}
            )
        with self.assertRaises(ValueError):
            parse_stock_backtest_request(
                {"stock_id": "2330", "pattern": "ma_cross"}
            )
        got = parse_stock_backtest_request(
            {"stock_id": "2330", "pattern": "inside_day", "hold_days": 10}
        )
        self.assertEqual(got["hold_days"], 10)
        self.assertIsNone(got["stop_pct"])
        self.assertEqual(got["pattern"], "inside_day")

    def test_maps_stock_daily_columns(self):
        rows = [
            ("2026-06-01", 100.0, 101.0, 99.0, 100.5, 1000, "台積電"),
            ("2026-06-02", 100.5, 102.0, 100.0, 101.0, 1100, "台積電"),
        ]
        df, name = stock_daily_rows_to_yahoo(rows)
        self.assertEqual(name, "台積電")
        self.assertEqual(list(df.columns), ["Open", "High", "Low", "Close", "Volume"])
        self.assertAlmostEqual(df["Close"].iloc[-1], 101.0)
        self.assertEqual(df.index[0], pd.Timestamp("2026-06-01"))

    def test_load_and_run_from_sqlite_no_network(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "twse.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE stock_daily ("
            "trade_date TEXT, stock_id TEXT, stock_name TEXT, "
            "open REAL, high REAL, low REAL, close REAL, volume INTEGER, turnover INTEGER)"
        )
        df = _append_days(_upper_shadow_only(), 5, lambda k, last: last * 1.01)
        for ts, row in df.iterrows():
            conn.execute(
                "INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ts.strftime("%Y-%m-%d"),
                    "2330",
                    "台積電",
                    row["Open"],
                    row["High"],
                    row["Low"],
                    row["Close"],
                    int(row["Volume"]),
                    0,
                ),
            )
        conn.commit()
        loaded, name = load_stock_daily_yahoo(conn, "2330")
        self.assertEqual(name, "台積電")
        self.assertEqual(len(loaded), len(df))
        self.assertEqual(list(loaded.columns), ["Open", "High", "Low", "Close", "Volume"])

        result = run_stock_backtest(
            conn,
            {
                "stock_id": "2330",
                "pattern": "upper_shadow_reversal",
                "hold_days": 5,
                "cost_pct": 0,
            },
        )
        self.assertEqual(result["universe"], "stock")
        self.assertEqual(result["dataset"], "stock_daily")
        self.assertFalse(result["has_intraday_path"])
        self.assertEqual(result["stock_id"], "2330")
        self.assertGreaterEqual(result["n"], 1)
        self.assertIn("日K", result["assumptions"])
        self.assertIn("小時", ASSUMPTIONS)
        self.assertNotIn("taiex_hourly", result["assumptions"])

        missing = run_stock_backtest(
            conn, {"stock_id": "9999", "pattern": "inside_day", "hold_days": 5}
        )
        self.assertIn("查無此股", missing["error"])
        conn.close()

    def test_assumptions_document_touch_and_daily_only(self):
        self.assertIn("低點", ASSUMPTIONS)
        self.assertIn("高點", ASSUMPTIONS)
        self.assertIn("先停損", ASSUMPTIONS)
        self.assertIn("stock_daily", ASSUMPTIONS)
        self.assertIn("無個股小時", ASSUMPTIONS)


if __name__ == "__main__":
    unittest.main()
