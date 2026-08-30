import unittest
from unittest.mock import patch

import pandas as pd

from prices import (
    download_history,
    extract_ohlcv,
    fetch_latest_close,
    fetch_latest_closes,
    last_close,
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

    def test_close_as_dataframe_single_column(self):
        idx = pd.bdate_range("2026-08-01", periods=2)
        wide = pd.DataFrame({("Close", "2330.TW"): [100.0, 105.5]}, index=idx)
        self.assertEqual(last_close(wide, "2330.TW"), 105.5)

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

    def test_ticker_last_level(self):
        idx = pd.bdate_range("2026-08-01", periods=2)
        df = pd.DataFrame(
            {
                ("Open", "2330.TW"): [10.0, 11.0],
                ("High", "2330.TW"): [10.5, 11.5],
                ("Low", "2330.TW"): [9.5, 10.5],
                ("Close", "2330.TW"): [10.2, 11.2],
                ("Volume", "2330.TW"): [1.0, 2.0],
            },
            index=idx,
        )
        out = extract_ohlcv(df, "2330.TW")
        self.assertEqual(out["Close"].iloc[-1], 11.2)


class FetchClosesTests(unittest.TestCase):
    def test_batch_reads_group_by_ticker_frame(self):
        a = _flat_ohlcv((100.0, 101.0), ticker="2330.TW")
        b = _flat_ohlcv((50.0, 55.0), ticker="1101.TW")
        data = pd.concat([a, b], axis=1)

        with patch("prices.yf.download", return_value=data):
            prices = fetch_latest_closes(["2330", "1101"])
        self.assertEqual(prices["2330"], 101.0)
        self.assertEqual(prices["1101"], 55.0)

    def test_batch_failure_falls_back_one_by_one(self):
        flat = _flat_ohlcv((9.0, 9.5))

        def fake_download(symbols, **kwargs):
            if isinstance(symbols, list):
                raise RuntimeError("yahoo down")
            return flat

        with patch("prices.yf.download", side_effect=fake_download):
            prices = fetch_latest_closes(["2330"])
        self.assertEqual(prices["2330"], 9.5)

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
            frames = download_history(["2330.TW"])
        self.assertIn("2330.TW", frames)
        self.assertEqual(frames["2330.TW"]["Close"].iloc[-1], 2.0)


if __name__ == "__main__":
    unittest.main()
