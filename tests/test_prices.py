import unittest
from datetime import date

import pandas as pd

from notify.performance_checker import evaluate_row
from data.prices import (
    calendar_buffer_days,
    extract_ohlcv,
    horizon_exit,
    last_close,
    signal_index,
)


def _bars(n: int = 40, start="2026-04-01", start_px: float = 100.0) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=n)
    closes = [start_px + i for i in range(n)]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [1_000.0] * n,
        },
        index=idx,
    )


class PricesTests(unittest.TestCase):
    def test_last_close_reads_multiindex(self):
        idx = pd.bdate_range("2026-06-01", periods=3)
        df = pd.DataFrame(
            {("2330.TW", "Close"): [10.0, 11.0, 12.5], ("2330.TW", "Open"): [10.0, 11.0, 12.0]},
            index=idx,
        )
        self.assertEqual(last_close(df, "2330.TW"), 12.5)
        self.assertFalse(extract_ohlcv(df, "2330.TW").empty)

    def test_horizon_exit_is_trading_days_after_signal(self):
        df = _bars()
        alert = date(2026, 4, 1)  # first bar
        exit_bar = horizon_exit(df, alert, 5)
        self.assertIsNotNone(exit_bar)
        exit_date, exit_px = exit_bar
        self.assertEqual(exit_date, df.index[5].date())
        self.assertEqual(exit_px, 105.0)

        # Weekend alert date still maps to the Friday signal bar.
        weekend_df = _bars(n=5, start="2026-04-03")  # Friday
        mapped = horizon_exit(weekend_df, date(2026, 4, 4), 1)
        self.assertEqual(mapped[0], date(2026, 4, 6))

    def test_horizon_exit_none_until_enough_bars(self):
        df = _bars(n=6)
        self.assertIsNone(horizon_exit(df, date(2026, 4, 1), 20))
        self.assertIsNotNone(horizon_exit(df, date(2026, 4, 1), 5))

    def test_signal_index_skips_future_alert(self):
        dates = [date(2026, 4, 1), date(2026, 4, 2)]
        self.assertIsNone(signal_index(dates, date(2026, 3, 1)))

    def test_evaluate_row_uses_alert_price_not_todays_close(self):
        df = _bars()
        row = {
            "id": 1,
            "ticker": "2330",
            "pattern_type": "upper_shadow_reversal",
            "alert_date": str(df.index[0].date()),
            "price_at_alert": 100.0,
        }
        measured = evaluate_row(row, df, 20)
        self.assertEqual(measured["horizon_td"], 20)
        self.assertEqual(measured["exit_date"], str(df.index[20].date()))
        self.assertAlmostEqual(measured["return_pct"], 20.0)

    def test_calendar_buffer_grows_with_horizon(self):
        self.assertLess(calendar_buffer_days(5), calendar_buffer_days(20))
        self.assertLess(calendar_buffer_days(20), calendar_buffer_days(60))


if __name__ == "__main__":
    unittest.main()
