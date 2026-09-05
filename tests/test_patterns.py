import unittest
from datetime import date

import pandas as pd

from data.prices import drop_incomplete_ohlc, fill_missing_closes
from signals.patterns import (
    check_inside_day,
    check_upper_shadow_reversal,
    classify_pattern,
    last_bar_date,
    matches_pattern,
    pattern_on_trailing_window,
)


def _base_df(n: int = 30, start_close: float = 90.0) -> pd.DataFrame:
    """Quiet uptrend so the last close sits above MA20."""
    idx = pd.bdate_range("2026-06-01", periods=n)
    closes = [start_close + i * 0.3 for i in range(n)]
    return pd.DataFrame(
        {
            "Open": [c - 0.2 for c in closes],
            "High": [c + 0.3 for c in closes],
            "Low": [c - 0.4 for c in closes],
            "Close": closes,
            "Volume": [1_000.0] * n,
        },
        index=idx,
    )


def _set_bar(df: pd.DataFrame, pos: int, open_, high, low, close, volume=5_000.0) -> None:
    i = pos if pos >= 0 else len(df) + pos
    df.iloc[i, df.columns.get_loc("Open")] = open_
    df.iloc[i, df.columns.get_loc("High")] = high
    df.iloc[i, df.columns.get_loc("Low")] = low
    df.iloc[i, df.columns.get_loc("Close")] = close
    df.iloc[i, df.columns.get_loc("Volume")] = volume


def _upper_shadow_only() -> pd.DataFrame:
    """Matches upper-shadow reversal on (-2, -1), but not Inside Day."""
    df = _base_df()
    _set_bar(df, -2, open_=100.0, high=106.0, low=99.0, close=101.0)
    _set_bar(df, -1, open_=102.0, high=108.0, low=101.0, close=107.0)
    return df


def _inside_day() -> pd.DataFrame:
    """Two consecutive reversals; last close is the 3-day high and above MA20."""
    df = _base_df()
    _set_bar(df, -3, open_=100.0, high=108.0, low=99.0, close=101.0)
    _set_bar(df, -2, open_=108.0, high=116.0, low=107.0, close=109.0)
    _set_bar(df, -1, open_=110.0, high=119.0, low=109.0, close=118.0)
    return df


class PatternTests(unittest.TestCase):
    def test_upper_shadow_reversal_matches(self):
        self.assertTrue(check_upper_shadow_reversal(_upper_shadow_only()))

    def test_upper_shadow_only_is_not_inside_day(self):
        self.assertFalse(check_inside_day(_upper_shadow_only()))

    def test_inside_day_also_matches_upper_shadow(self):
        df = _inside_day()
        self.assertTrue(check_inside_day(df))
        self.assertTrue(check_upper_shadow_reversal(df))

    def test_classify_prefers_inside_day(self):
        self.assertEqual(classify_pattern(_inside_day()), "inside_day")
        self.assertEqual(classify_pattern(_upper_shadow_only()), "upper_shadow_reversal")

    def test_classify_none_on_quiet_tape(self):
        self.assertIsNone(classify_pattern(_base_df()))

    def test_incomplete_last_bar_does_not_hide_match(self):
        df = _upper_shadow_only()
        self.assertEqual(classify_pattern(df), "upper_shadow_reversal")
        incomplete = df.copy()
        extra = incomplete.iloc[[-1]].copy()
        extra.index = extra.index + pd.Timedelta(days=1)
        extra["Close"] = float("nan")
        incomplete = pd.concat([incomplete, extra])
        self.assertIsNone(classify_pattern(incomplete))
        cleaned = drop_incomplete_ohlc(incomplete)
        self.assertEqual(classify_pattern(cleaned), "upper_shadow_reversal")
        self.assertEqual(last_bar_date(cleaned), last_bar_date(df))

    def test_hourly_fill_keeps_same_session_when_daily_close_is_nan(self):
        df = _upper_shadow_only()
        expected_date = last_bar_date(df)
        expected_close = float(df["Close"].iloc[-1])
        incomplete = df.copy()
        incomplete.loc[incomplete.index[-1], "Close"] = float("nan")
        self.assertIsNone(classify_pattern(incomplete))
        hourly = pd.DataFrame(
            {
                "Open": [expected_close],
                "High": [expected_close + 1],
                "Low": [expected_close - 1],
                "Close": [expected_close],
                "Volume": [1_000.0],
            },
            index=pd.to_datetime([f"{expected_date} 05:00:00"], utc=True),
        )
        filled = fill_missing_closes(incomplete, hourly)
        self.assertEqual(classify_pattern(filled), "upper_shadow_reversal")
        self.assertEqual(last_bar_date(filled), expected_date)

    def test_last_bar_date_uses_index_not_today(self):
        df = _base_df()
        expected = df.index[-1].date()
        self.assertEqual(last_bar_date(df), expected)
        self.assertIsInstance(last_bar_date(df), date)

    def test_matches_pattern_keeps_live_semantics(self):
        self.assertTrue(matches_pattern(_upper_shadow_only(), "upper_shadow_reversal"))
        self.assertEqual(
            matches_pattern(_inside_day(), "inside_day"),
            check_inside_day(_inside_day()),
        )
        i = len(_upper_shadow_only()) - 1
        self.assertTrue(pattern_on_trailing_window(_upper_shadow_only(), i, "upper_shadow_reversal"))

    def test_last_bar_date_converts_tz(self):
        df = _base_df()
        df.index = df.index.tz_localize("UTC")
        # 2026-07-10 00:00 UTC is still 2026-07-10 in Taipei
        self.assertEqual(last_bar_date(df), df.index[-1].tz_convert("Asia/Taipei").date())


if __name__ == "__main__":
    unittest.main()
