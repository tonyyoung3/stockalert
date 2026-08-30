import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from performance_checker import evaluate_row
from prices import (
    calendar_buffer_days,
    download_history,
    extract_ohlcv,
    fetch_latest_close,
    fetch_latest_closes,
    horizon_exit,
    last_close,
    signal_index,
    to_yahoo_symbol,
)


def _flat_ohlcv(closes=(10.0, 11.0, 12.5), ticker=None) -> pd.DataFrame:
    idx = pd.bdate_range("2026-08-01", periods=len(closes))
    frame = pd.DataFrame(
        {
            "Open": [c - 0.2 for c in closes],
            "High": [c + 0.3 for c in closes],
            "Low": [c - 0.4 for c in closes],
            "Close": list(closes),
            "Volume": [1_000.0] * len(closes),
        },
        index=idx,
    )
    if ticker:
        frame.columns = pd.MultiIndex.from_product([[ticker], frame.columns])
    return frame


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


class SymbolTests(unittest.TestCase):
    def test_digit_ticker_gets_tw_suffix(self):
        self.assertEqual(to_yahoo_symbol("2330"), "2330.TW")

    def test_already_suffixed_is_unchanged(self):
        self.assertEqual(to_yahoo_symbol("2330.TW"), "2330.TW")
        self.assertEqual(to_yahoo_symbol("6488.TWO"), "6488.TWO")

    def test_non_digit_passthrough(self):
        self.assertEqual(to_yahoo_symbol("TSLA"), "TSLA")


class LastCloseTests(unittest.TestCase):
    def test_flat_columns(self):
        self.assertEqual(last_close(_flat_ohlcv()), 12.5)

    def test_group_by_ticker_multiindex(self):
        df = _flat_ohlcv(ticker="2330.TW")
        self.assertEqual(last_close(df, "2330.TW"), 12.5)

    def test_price_then_ticker_multiindex(self):
        idx = pd.bdate_range("2026-08-01", periods=3)
        df = pd.DataFrame(
            {
                ("Close", "2330.TW"): [10.0, 11.0, 13.0],
                ("Close", "1101.TW"): [20.0, 21.0, 22.0],
                ("Open", "2330.TW"): [9.0, 10.0, 12.0],
            },
            index=idx,
        )
        self.assertEqual(last_close(df, "2330.TW"), 13.0)
        self.assertEqual(last_close(df, "1101.TW"), 22.0)

    def test_trailing_nan_uses_previous_bar(self):
        df = _flat_ohlcv((10.0, 11.0, float("nan")))
        self.assertEqual(last_close(df), 11.0)

    def test_empty_and_missing(self):
        self.assertIsNone(last_close(pd.DataFrame()))
        self.assertIsNone(last_close(None))
        self.assertIsNone(last_close(pd.DataFrame({"Open": [1.0]})))


class ExtractOhlcvTests(unittest.TestCase):
    def test_flat_frame_passthrough(self):
        df = _flat_ohlcv()
        out = extract_ohlcv(df, "2330.TW")
        self.assertEqual(list(out["Close"]), [10.0, 11.0, 12.5])

    def test_ticker_first_level(self):
        df = _flat_ohlcv(ticker="2330.TW")
        out = extract_ohlcv(df, "2330.TW")
        self.assertIn("Close", out.columns)
        self.assertEqual(out["Close"].iloc[-1], 12.5)


class FetchClosesTests(unittest.TestCase):
    def test_batch_reads_group_by_ticker_frame(self):
        a = _flat_ohlcv((100.0, 101.0), ticker="2330.TW")
        b = _flat_ohlcv((50.0, 55.0), ticker="1101.TW")
        data = pd.concat([a, b], axis=1)

        with patch("prices.yf.download", return_value=data):
            prices = fetch_latest_closes(["2330", "1101"])
        self.assertEqual(prices["2330"], 101.0)
        self.assertEqual(prices["1101"], 55.0)

    def test_single_close_uses_last_close_helper(self):
        wide = pd.DataFrame(
            {("Close", "2330.TW"): [80.0, 82.0]},
            index=pd.bdate_range("2026-08-01", periods=2),
        )
        with patch("prices.yf.download", return_value=wide):
            self.assertEqual(fetch_latest_close("2330"), 82.0)

    def test_download_history_maps_original_tickers(self):
        a = _flat_ohlcv((1.0, 2.0), ticker="2330.TW")
        with patch("prices.yf.download", return_value=a):
            frames = download_history(["2330.TW"], period="2mo")
        self.assertIn("2330.TW", frames)
        self.assertEqual(frames["2330.TW"]["Close"].iloc[-1], 2.0)


class HorizonTests(unittest.TestCase):
    def test_horizon_exit_is_trading_days_after_signal(self):
        df = _bars()
        alert = date(2026, 4, 1)
        exit_bar = horizon_exit(df, alert, 5)
        self.assertIsNotNone(exit_bar)
        exit_date, exit_px = exit_bar
        self.assertEqual(exit_date, df.index[5].date())
        self.assertEqual(exit_px, 105.0)

        weekend_df = _bars(n=5, start="2026-04-03")
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
