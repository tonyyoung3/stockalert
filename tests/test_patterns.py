import unittest
from datetime import date

import pandas as pd

from signals.patterns import (
    check_inside_day,
    check_upper_shadow_reversal,
    classify_pattern,
    last_bar_date,
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

    def test_last_bar_date_uses_index_not_today(self):
        df = _base_df()
        expected = df.index[-1].date()
        self.assertEqual(last_bar_date(df), expected)
        self.assertIsInstance(last_bar_date(df), date)

    def test_last_bar_date_converts_tz(self):
        df = _base_df()
        df.index = df.index.tz_localize("UTC")
        # 2026-07-10 00:00 UTC is still 2026-07-10 in Taipei
        self.assertEqual(last_bar_date(df), df.index[-1].tz_convert("Asia/Taipei").date())


if __name__ == "__main__":
    unittest.main()
