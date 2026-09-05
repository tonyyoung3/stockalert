"""#41: v1 blocks schema compiles to the existing flat backtest rule."""
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from web import backtest_engine
from web.strategy_blocks import (
    CLOSE_DECIDED_INTRADAY_ERROR,
    FILTER_TYPES,
    BlocksError,
    blocks_to_rule,
    coerce_rule,
    default_filters,
    filter_block_is_close_decided,
    is_blocks_payload,
    rule_to_blocks,
    summarize_blocks,
)


def _intraday_blocks(**kwargs):
    doc = {
        "version": 1,
        "dataset": "2y_hourly",
        "mode": "intraday",
        "filters": [
            {"type": "weekdays", "params": {"days": [0, 1, 2, 3]}},
            {"type": "trend", "params": {"value": "above_ma20"}},
            {"type": "gap", "params": {"dir": "down", "abs_min_pct": 0.3}},
        ],
        "entry": {
            "direction": "long",
            "reference": "first_hour_high",
            "offset_pct": -0.5,
            "trigger": "touch_from_below",
            "earliest_hour": 10,
        },
        "exit": {
            "exit_hour": 13,
            "stop_enabled": True,
            "stop_reference": "day_open",
            "stop_offset_pct": -0.8,
        },
        "cost_pct": 0.03,
    }
    doc.update(kwargs)
    return doc


def _overnight_blocks(**kwargs):
    doc = {
        "version": 1,
        "dataset": "2y_hourly",
        "mode": "overnight",
        "filters": [
            {"type": "weekdays", "params": {"days": [0, 1, 2, 3]}},
            {"type": "prev_day", "params": {"value": "up"}},
        ],
        "entry": {"direction": "long"},
        "exit": {
            "hold_to": "next_hour",
            "hold_to_hour": 10,
            "skip_weekend": True,
        },
        "cost_pct": 0.03,
    }
    doc.update(kwargs)
    return doc


def _swing_blocks(**kwargs):
    doc = {
        "version": 1,
        "dataset": "15y_daily",
        "mode": "swing",
        "filters": [
            {"type": "ma_cross", "params": {"value": "golden"}},
            {"type": "breakout", "params": {"kind": "n_day_high", "window": 20}},
            {"type": "oi_ratio", "params": {"mode": "below_pctile", "pctile": 25, "window": 60}},
        ],
        "entry": {"direction": "long"},
        "exit": {
            "stop_pct": 2,
            "max_hold_days": 20,
            "take_profit_on": True,
            "take_profit_pct": 5,
        },
        "cost_pct": 0.03,
    }
    doc.update(kwargs)
    return doc


class SchemaAndCompileTests(unittest.TestCase):
    def test_catalog_is_v1_epic_set(self):
        self.assertEqual(
            FILTER_TYPES,
            (
                "weekdays",
                "trend",
                "prev_day",
                "gap",
                "day_return",
                "ma_cross",
                "breakout",
                "oi_ratio",
            ),
        )

    def test_is_blocks_payload_uses_filters_list(self):
        self.assertTrue(is_blocks_payload({"version": 1, "filters": []}))
        self.assertFalse(is_blocks_payload({"filters": default_filters()}))
        self.assertFalse(is_blocks_payload({"mode": "intraday"}))

    def test_intraday_compiles_to_engine_fields(self):
        rule = blocks_to_rule(_intraday_blocks())
        self.assertEqual(rule["dataset"], "2y_hourly")
        self.assertEqual(rule["mode"], "intraday")
        self.assertEqual(rule["filters"]["weekdays"], [0, 1, 2, 3])
        self.assertEqual(rule["filters"]["trend"], "above_ma20")
        self.assertEqual(rule["filters"]["gap_dir"], "down")
        self.assertEqual(rule["filters"]["gap_abs_min_pct"], 0.3)
        self.assertEqual(rule["filters"]["day_ret_dir"], "any")
        self.assertEqual(rule["entry"]["reference"], "first_hour_high")
        self.assertEqual(rule["entry"]["offset_pct"], -0.5)
        self.assertEqual(rule["entry"]["trigger"], "touch_from_below")
        self.assertEqual(rule["exit_hour"], 13)
        self.assertTrue(rule["stop"]["enabled"])
        self.assertEqual(rule["stop"]["offset_pct"], -0.8)

    def test_overnight_compiles_to_engine_fields(self):
        rule = blocks_to_rule(_overnight_blocks())
        self.assertEqual(rule["mode"], "overnight")
        self.assertEqual(rule["direction"], "long")
        self.assertEqual(rule["hold_to"], "next_hour")
        self.assertEqual(rule["hold_to_hour"], 10)
        self.assertTrue(rule["skip_weekend"])
        self.assertEqual(rule["filters"]["prev_day"], "up")

    def test_swing_compiles_to_engine_fields(self):
        rule = blocks_to_rule(_swing_blocks())
        self.assertEqual(rule["mode"], "swing")
        self.assertEqual(rule["dataset"], "15y_daily")
        self.assertEqual(rule["stop_pct"], 2)
        self.assertEqual(rule["max_hold_days"], 20)
        self.assertTrue(rule["take_profit_on"])
        self.assertEqual(rule["take_profit_pct"], 5)
        self.assertEqual(rule["filters"]["ma_cross"], "golden")
        self.assertEqual(rule["filters"]["breakout"], "n_day_high")
        self.assertEqual(rule["filters"]["breakout_window"], 20)
        self.assertEqual(rule["filters"]["oi_ratio_mode"], "below_pctile")

    def test_round_trip_intraday_overnight_swing(self):
        for doc in (_intraday_blocks(), _overnight_blocks(), _swing_blocks()):
            rule = blocks_to_rule(doc)
            again = blocks_to_rule(rule_to_blocks(rule))
            self.assertEqual(rule, again)

    def test_rule_to_blocks_omits_inactive_filters(self):
        rule = {
            "dataset": "2y_hourly",
            "mode": "overnight",
            "filters": default_filters(),
            "direction": "short",
            "hold_to": "next_close",
            "hold_to_hour": 10,
            "skip_weekend": False,
            "cost_pct": 0.03,
        }
        blocks = rule_to_blocks(rule)
        self.assertEqual(blocks["filters"], [])
        self.assertEqual(blocks["entry"]["direction"], "short")
        self.assertEqual(blocks["exit"]["hold_to"], "next_close")
        self.assertFalse(blocks["exit"]["skip_weekend"])

    def test_close_decided_rejected_in_intraday(self):
        cases = [
            [{"type": "day_return", "params": {"dir": "up", "min_pct": 1}}],
            [{"type": "ma_cross", "params": {"value": "golden"}}],
            [{"type": "breakout", "params": {"kind": "n_day_high", "window": 20}}],
            [{"type": "trend", "params": {"value": "above_ma20_today"}}],
        ]
        for filters in cases:
            with self.subTest(filters=filters):
                self.assertTrue(filter_block_is_close_decided(filters[0]))
                with self.assertRaises(BlocksError) as ctx:
                    blocks_to_rule(_intraday_blocks(filters=filters))
                self.assertEqual(str(ctx.exception), CLOSE_DECIDED_INTRADAY_ERROR)

    def test_close_decided_allowed_in_overnight_and_swing(self):
        overnight = _overnight_blocks(
            filters=[{"type": "day_return", "params": {"dir": "down", "min_pct": 1}}]
        )
        swing = _swing_blocks()
        self.assertEqual(blocks_to_rule(overnight)["filters"]["day_ret_dir"], "down")
        self.assertEqual(blocks_to_rule(swing)["filters"]["ma_cross"], "golden")

    def test_duplicate_and_unknown_filter_rejected(self):
        with self.assertRaises(BlocksError):
            blocks_to_rule(_intraday_blocks(filters=[
                {"type": "gap", "params": {"dir": "up"}},
                {"type": "gap", "params": {"dir": "down"}},
            ]))
        with self.assertRaises(BlocksError):
            blocks_to_rule(_intraday_blocks(filters=[{"type": "or_group", "params": {}}]))

    def test_coerce_rule_passthrough_and_compile(self):
        flat = {"dataset": "2y_hourly", "mode": "overnight", "filters": {"prev_day": "down"}}
        self.assertIs(coerce_rule(flat), flat)
        compiled = coerce_rule(_overnight_blocks())
        self.assertEqual(compiled["mode"], "overnight")
        self.assertIsInstance(compiled["filters"], dict)

    def test_summarize_has_if_then_chips(self):
        chips = summarize_blocks(_intraday_blocks())
        self.assertIn("2年小時K", chips)
        self.assertIn("日內", chips)
        self.assertTrue(any(c.startswith("若 ") for c in chips))
        self.assertTrue(any(c.startswith("則進場") for c in chips))
        self.assertTrue(any(c.startswith("則出場") for c in chips))


def _seed_market(conn, n_days=80):
    conn.execute(
        "CREATE TABLE taiex_daily (trade_date TEXT, open REAL, high REAL, low REAL, close REAL)"
    )
    conn.execute(
        "CREATE TABLE taiex_hourly_ohlc "
        "(trade_date TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL)"
    )
    conn.execute(
        "CREATE TABLE taifex_fut_oi "
        "(trade_date TEXT, product TEXT, investor TEXT, oi_net_lots INTEGER)"
    )
    start = date(2024, 1, 2)
    price = 18000.0
    added = 0
    day = start
    while added < n_days:
        if day.weekday() < 5:
            close = price
            open_ = price * 0.999
            high = price * 1.006
            low = price * 0.994
            ds = day.isoformat()
            conn.execute(
                "INSERT INTO taiex_daily VALUES (?,?,?,?,?)",
                (ds, open_, high, low, close),
            )
            for hr, frac in ((9, 0.0), (10, 0.002), (11, -0.001), (12, 0.003), (13, 0.0)):
                o = close * (1 + frac)
                conn.execute(
                    "INSERT INTO taiex_hourly_ohlc VALUES (?,?,?,?,?,?)",
                    (ds, f"{ds} {hr:02d}:00:00", o, o * 1.003, o * 0.997, o * 1.001),
                )
            conn.execute(
                "INSERT INTO taifex_fut_oi VALUES (?,?,?,?)",
                (ds, "臺股期貨", "外資及陸資", 10000 + added * 10),
            )
            conn.execute(
                "INSERT INTO taifex_fut_oi VALUES (?,?,?,?)",
                (ds, "臺股期貨", "投信", -2000 - added),
            )
            price *= 1.001 if added % 3 else 0.999
            added += 1
        day += timedelta(days=1)
    conn.commit()


class CompileEquivalenceBacktestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "m.db"
        self.conn = sqlite3.connect(self.path)
        _seed_market(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _cmp(self, blocks):
        rule = blocks_to_rule(blocks)
        a = backtest_engine.run_backtest(self.conn, rule)
        b = backtest_engine.run_backtest(self.conn, blocks)
        self.assertEqual(a.get("n"), b.get("n"))
        self.assertEqual(a.get("ev_pct"), b.get("ev_pct"))
        self.assertEqual(a.get("win_rate"), b.get("win_rate"))
        self.assertEqual(a.get("days_passed_filter"), b.get("days_passed_filter"))
        self.assertEqual(a.get("error"), b.get("error"))
        return a

    def test_intraday_blocks_match_flat_rule(self):
        result = self._cmp(_intraday_blocks(filters=[]))
        self.assertGreater(result["n"], 0)

    def test_overnight_blocks_match_flat_rule(self):
        result = self._cmp(_overnight_blocks())
        self.assertGreater(result["n"], 0)

    def test_swing_blocks_match_flat_rule(self):
        result = self._cmp(_swing_blocks(filters=[], dataset="2y_hourly"))
        self.assertGreater(result["n"], 0)

    def test_run_backtest_rejects_close_decided_blocks_in_intraday(self):
        blocks = _intraday_blocks(
            filters=[{"type": "day_return", "params": {"dir": "up", "min_pct": 1}}]
        )
        result = backtest_engine.run_backtest(self.conn, blocks)
        self.assertIn("收盤", result["error"])
        self.assertIn("日內", result["error"])


if __name__ == "__main__":
    unittest.main()
