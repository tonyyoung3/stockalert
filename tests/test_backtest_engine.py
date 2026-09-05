"""Dedicated unit tests for web.backtest_engine (#71).

In-memory sqlite fixtures only — no network, no Turso.
"""
import sqlite3
import unittest
from datetime import date, timedelta

import pandas as pd

from web import backtest_engine


def _create_tables(conn):
    conn.execute(
        "CREATE TABLE taiex_daily "
        "(trade_date TEXT, open REAL, high REAL, low REAL, close REAL)"
    )
    conn.execute(
        "CREATE TABLE taiex_hourly_ohlc "
        "(trade_date TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL)"
    )
    conn.execute(
        "CREATE TABLE taifex_fut_oi "
        "(trade_date TEXT, product TEXT, investor TEXT, oi_net_lots INTEGER)"
    )


def _weekdays(n, start=date(2024, 1, 8)):
    days = []
    day = start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _synthetic_hours(ds, open_, high, low, close):
    """Five hourly bars that respect the daily OHLC envelope."""
    pts = [
        open_,
        open_ + (close - open_) * 0.3,
        open_ + (close - open_) * 0.5,
        open_ + (close - open_) * 0.8,
        close,
    ]
    rows = []
    for i, hr in enumerate((9, 10, 11, 12, 13)):
        bar_o = pts[i - 1] if i else open_
        bar_c = pts[i]
        bar_h = max(bar_o, bar_c)
        bar_l = min(bar_o, bar_c)
        if hr == 10:
            bar_h = max(bar_h, high)
        if hr == 11:
            bar_l = min(bar_l, low)
        bar_h = max(bar_h, bar_o, bar_c)
        bar_l = min(bar_l, bar_o, bar_c)
        rows.append((ds, f"{ds} {hr:02d}:00:00", bar_o, bar_h, bar_l, bar_c))
    return rows


def _insert_session(conn, day, open_, high, low, close, hours="auto"):
    ds = day.isoformat() if hasattr(day, "isoformat") else day
    conn.execute(
        "INSERT INTO taiex_daily VALUES (?,?,?,?,?)",
        (ds, open_, high, low, close),
    )
    if hours == "auto":
        hours = _synthetic_hours(ds, open_, high, low, close)
    if hours:
        for row in hours:
            conn.execute(
                "INSERT INTO taiex_hourly_ohlc VALUES (?,?,?,?,?,?)", row
            )


def _seed_rising(conn, n_days=10, start_price=18000.0, hourly=True):
    """Mildly rising tape. Lows stay well above a 2% swing stop."""
    price = start_price
    for i, day in enumerate(_weekdays(n_days)):
        close = price
        open_ = price * 0.999
        high = price * 1.006
        low = price * 0.997
        _insert_session(
            conn,
            day,
            open_,
            high,
            low,
            close,
            hours="auto" if hourly else [],
        )
        price *= 1.002
    conn.commit()


def _intraday_rule(**kwargs):
    rule = {
        "dataset": "2y_hourly",
        "mode": "intraday",
        "filters": {},
        "entry": {
            "direction": "long",
            "reference": "day_open",
            "offset_pct": 0,
            "trigger": "touch_from_above",
            "earliest_hour": 9,
        },
        "exit_hour": 13,
        "stop": {"enabled": False},
        "cost_pct": 0,
    }
    rule.update(kwargs)
    return rule


def _overnight_rule(**kwargs):
    rule = {
        "dataset": "2y_hourly",
        "mode": "overnight",
        "filters": {},
        "direction": "long",
        "hold_to": "next_close",
        "cost_pct": 0,
    }
    rule.update(kwargs)
    return rule


def _swing_rule(**kwargs):
    rule = {
        "dataset": "15y_daily",
        "mode": "swing",
        "filters": {},
        "direction": "long",
        "stop_pct": 5,
        "max_hold_days": 3,
        "cost_pct": 0,
    }
    rule.update(kwargs)
    return rule


class _EngineCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        _create_tables(self.conn)

    def tearDown(self):
        self.conn.close()


class HappyPathTests(_EngineCase):
    def test_intraday_touch_and_exit_hour(self):
        _seed_rising(self.conn)
        result = backtest_engine.run_backtest(self.conn, _intraday_rule())
        self.assertNotIn("error", result)
        self.assertGreaterEqual(result["n"], 1)
        self.assertEqual(result["mode"], "intraday")
        self.assertEqual(result["dataset"], "2y_hourly")
        self.assertEqual(result["direction"], "long")
        self.assertFalse(result["stale_open_warning"])
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "exit_hour")
        self.assertIsNotNone(trade["entry_time"])
        self.assertIsNotNone(trade["exit_time"])
        self.assertGreater(trade["entry_price"], 0)
        self.assertGreater(result["days_passed_filter"], 0)

    def test_overnight_close_to_next_close(self):
        _seed_rising(self.conn)
        result = backtest_engine.run_backtest(self.conn, _overnight_rule())
        self.assertNotIn("error", result)
        self.assertGreaterEqual(result["n"], 1)
        self.assertEqual(result["mode"], "overnight")
        self.assertFalse(result["stale_open_warning"])
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "hold_to")
        self.assertIn("收盤", trade["entry_time"])
        self.assertGreater(trade["exit_price"], 0)
        # Rising tape + next_close → first trade should be profitable (cost=0).
        self.assertGreater(trade["ret_net_pct"], 0)

    def test_swing_exits_at_max_hold(self):
        _seed_rising(self.conn, hourly=False)
        result = backtest_engine.run_backtest(self.conn, _swing_rule())
        self.assertNotIn("error", result)
        self.assertGreaterEqual(result["n"], 1)
        self.assertEqual(result["mode"], "swing")
        self.assertEqual(result["dataset"], "15y_daily")
        self.assertFalse(result["stale_open_warning"])
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "max_hold")
        self.assertEqual(trade["hold_days"], 3)
        self.assertIn("unresolved_trades", result)
        self.assertIn("avg_hold_days", result)


class CloseDecidedFilterTests(_EngineCase):
    def test_flat_rule_close_decided_rejected_in_intraday(self):
        _seed_rising(self.conn)
        cases = [
            {"day_ret_dir": "up"},
            {"day_ret_min_pct": 1},
            {"trend": "above_ma20_today"},
            {"breakout": "n_day_high"},
            {"ma_cross": "golden"},
        ]
        for filters in cases:
            with self.subTest(filters=filters):
                result = backtest_engine.run_backtest(
                    self.conn, _intraday_rule(filters=filters)
                )
                self.assertIn("error", result)
                self.assertIn("收盤", result["error"])
                self.assertIn("日內", result["error"])
                self.assertNotIn("n", result)

    def test_daily_dataset_rejects_intraday_mode(self):
        _seed_rising(self.conn, hourly=False)
        result = backtest_engine.run_backtest(
            self.conn, _intraday_rule(dataset="15y_daily")
        )
        self.assertIn("error", result)
        self.assertIn("小時", result["error"])

    def test_close_decided_ok_in_overnight(self):
        _seed_rising(self.conn)
        result = backtest_engine.run_backtest(
            self.conn, _overnight_rule(filters={"day_ret_dir": "up"})
        )
        self.assertNotIn("error", result)
        self.assertGreaterEqual(result["n"], 1)


class EmptyResultTests(_EngineCase):
    def test_weekday_filter_matches_nothing(self):
        _seed_rising(self.conn)
        result = backtest_engine.run_backtest(
            self.conn, _overnight_rule(filters={"weekdays": [6]})
        )
        self.assertEqual(result["n"], 0)
        self.assertEqual(result["days_passed_filter"], 0)
        self.assertIn("沒有任何交易", result["error"])

    def test_intraday_trigger_never_touches(self):
        _seed_rising(self.conn)
        result = backtest_engine.run_backtest(
            self.conn,
            _intraday_rule(
                entry={
                    "direction": "long",
                    "reference": "day_open",
                    "offset_pct": 50,
                    "trigger": "touch_from_above",
                    "earliest_hour": 9,
                }
            ),
        )
        self.assertEqual(result["n"], 0)
        self.assertGreater(result["days_passed_filter"], 0)
        self.assertIn("沒有任何交易", result["error"])
        self.assertEqual(result["mode"], "intraday")


class StopLossTests(_EngineCase):
    def test_intraday_stop_hits_after_entry_bar(self):
        day = date(2024, 1, 8)
        hours = [
            ("2024-01-08", "2024-01-08 09:00:00", 10000, 10040, 9960, 10020),
            ("2024-01-08", "2024-01-08 10:00:00", 10020, 10030, 9880, 9920),
            ("2024-01-08", "2024-01-08 11:00:00", 9920, 9950, 9900, 9930),
            ("2024-01-08", "2024-01-08 12:00:00", 9930, 9960, 9910, 9940),
            ("2024-01-08", "2024-01-08 13:00:00", 9940, 9970, 9920, 9950),
        ]
        _insert_session(self.conn, day, 10000, 10040, 9880, 9950, hours=hours)
        self.conn.commit()
        result = backtest_engine.run_backtest(
            self.conn,
            _intraday_rule(
                stop={
                    "enabled": True,
                    "reference": "day_open",
                    "offset_pct": -1,
                }
            ),
        )
        self.assertEqual(result["n"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertAlmostEqual(trade["entry_price"], 10000, places=4)
        self.assertAlmostEqual(trade["exit_price"], 9900, places=4)
        self.assertLess(trade["ret_net_pct"], 0)
        self.assertEqual(result["stopped_rate"], 100.0)

    def test_swing_stop_hits_next_day_low(self):
        days = _weekdays(3)
        _insert_session(self.conn, days[0], 10000, 10050, 9960, 10000, hours=[])
        _insert_session(self.conn, days[1], 10000, 10020, 9700, 9800, hours=[])
        _insert_session(self.conn, days[2], 9800, 9900, 9750, 9850, hours=[])
        self.conn.commit()
        result = backtest_engine.run_backtest(
            self.conn,
            _swing_rule(stop_pct=2, max_hold_days=5),
        )
        self.assertGreaterEqual(result["n"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertAlmostEqual(trade["entry_price"], 10000, places=4)
        self.assertAlmostEqual(trade["exit_price"], 9800, places=4)
        self.assertEqual(trade["hold_days"], 1)
        self.assertLess(trade["ret_net_pct"], 0)


class StaleOpenWarningTests(_EngineCase):
    def test_daily_overnight_sets_stale_open_warning(self):
        _seed_rising(self.conn, hourly=False)
        result = backtest_engine.run_backtest(
            self.conn, _overnight_rule(dataset="15y_daily", hold_to="next_close")
        )
        self.assertGreaterEqual(result["n"], 1)
        self.assertTrue(result["stale_open_warning"])

    def test_daily_swing_clears_stale_open_warning(self):
        _seed_rising(self.conn, hourly=False)
        result = backtest_engine.run_backtest(self.conn, _swing_rule())
        self.assertGreaterEqual(result["n"], 1)
        self.assertFalse(result["stale_open_warning"])

    def test_hourly_modes_do_not_warn(self):
        _seed_rising(self.conn)
        intra = backtest_engine.run_backtest(self.conn, _intraday_rule())
        over = backtest_engine.run_backtest(self.conn, _overnight_rule())
        swing = backtest_engine.run_backtest(
            self.conn, _swing_rule(dataset="2y_hourly")
        )
        self.assertFalse(intra["stale_open_warning"])
        self.assertFalse(over["stale_open_warning"])
        self.assertFalse(swing["stale_open_warning"])


class EngineGuardTests(_EngineCase):
    def test_unknown_dataset(self):
        result = backtest_engine.run_backtest(
            self.conn, {"dataset": "99y_weekly", "mode": "overnight"}
        )
        self.assertIn("未知資料集", result["error"])

    def test_invalid_blocks_surface_as_error(self):
        result = backtest_engine.run_backtest(
            self.conn,
            {
                "version": 1,
                "dataset": "2y_hourly",
                "mode": "overnight",
                "filters": [{"type": "or_group", "params": {}}],
                "entry": {"direction": "long"},
                "exit": {"hold_to": "next_close"},
            },
        )
        self.assertIn("error", result)
        self.assertNotIn("n", result)

    def test_apply_filters_prev_day_gap_and_weekdays(self):
        df = pd.DataFrame(
            {
                "dow": [0, 1, 2],
                "prev_close": [100.0, 100.0, 100.0],
                "prev_ma20": [99.0, 99.0, 99.0],
                "prev_ma60": [98.0, 98.0, 98.0],
                "prev_ret": [0.01, -0.01, 0.02],
                "gap": [0.004, -0.003, 0.001],
                "day_ret": [0.01, -0.01, 0.0],
                "day_close": [101.0, 99.0, 100.0],
                "ma20": [100.0, 100.0, 100.0],
                "ma60": [99.0, 99.0, 99.0],
                "day_high": [102.0, 100.0, 101.0],
                "day_low": [99.0, 97.0, 99.0],
            }
        )
        weekdays = backtest_engine.apply_filters(df, {"weekdays": [0, 1]})
        self.assertListEqual(weekdays.tolist(), [True, True, False])
        prev_up = backtest_engine.apply_filters(df, {"prev_day": "up"})
        self.assertListEqual(prev_up.tolist(), [True, False, True])
        gap_down = backtest_engine.apply_filters(df, {"gap_dir": "down"})
        self.assertListEqual(gap_down.tolist(), [False, True, False])


if __name__ == "__main__":
    unittest.main()
