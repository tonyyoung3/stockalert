import unittest
from datetime import date

import pandas as pd

from notify.performance_checker import evaluate_row
from data.prices import (
    apply_official_bars,
    calendar_buffer_days,
    drop_incomplete_ohlc,
    extract_ohlcv,
    fill_missing_closes,
    horizon_exit,
    last_close,
    last_bar_needs_close,
    patch_incomplete_closes,
    signal_index,
    taiwan_session_date,
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

    def test_extract_ohlcv_drops_incomplete_last_bar(self):
        idx = pd.bdate_range("2026-06-01", periods=4)
        df = pd.DataFrame(
            {
                ("2330.TW", "Open"): [10.0, 11.0, 12.0, 13.0],
                ("2330.TW", "High"): [10.5, 11.5, 12.5, 13.5],
                ("2330.TW", "Low"): [9.5, 10.5, 11.5, 12.5],
                ("2330.TW", "Close"): [10.0, 11.0, 12.5, float("nan")],
                ("2330.TW", "Volume"): [1_000.0, 1_000.0, 1_000.0, 1_000.0],
            },
            index=idx,
        )
        frame = extract_ohlcv(df, "2330.TW")
        self.assertEqual(len(frame), 3)
        self.assertEqual(float(frame["Close"].iloc[-1]), 12.5)
        self.assertEqual(frame.index[-1].date(), date(2026, 6, 3))

    def test_drop_incomplete_ohlc_keeps_complete_bars(self):
        df = _bars(n=3)
        df.loc[df.index[-1], "Close"] = float("nan")
        cleaned = drop_incomplete_ohlc(df)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(float(cleaned["Close"].iloc[-1]), 101.0)

    def test_taiwan_session_date_from_utc_hourly(self):
        self.assertEqual(
            taiwan_session_date(pd.Timestamp("2026-09-02 05:00:00", tz="UTC")),
            date(2026, 9, 2),
        )

    def test_fill_missing_closes_from_hourly_session(self):
        daily = _bars(n=3)
        daily.loc[daily.index[-1], "Close"] = float("nan")
        session = daily.index[-1].date()
        hourly = pd.DataFrame(
            {
                "Open": [102.0, 106.0],
                "High": [104.0, 108.0],
                "Low": [101.0, 105.0],
                "Close": [103.0, 107.5],
                "Volume": [1_000.0, 1_000.0],
            },
            index=pd.to_datetime(
                [f"{session} 01:00:00", f"{session} 05:00:00"], utc=True
            ),
        )
        filled = fill_missing_closes(daily, hourly)
        self.assertEqual(float(filled["Close"].iloc[-1]), 107.5)
        self.assertEqual(filled.index[-1].date(), session)

    def test_patch_incomplete_closes_uses_hourly_download(self):
        daily = _bars(n=3)
        daily.loc[daily.index[-1], "Close"] = float("nan")
        session = daily.index[-1].date()
        hourly = pd.DataFrame(
            {
                "Open": [102.0],
                "High": [108.0],
                "Low": [101.0],
                "Close": [107.5],
                "Volume": [1_000.0],
            },
            index=pd.to_datetime([f"{session} 05:00:00"], utc=True),
        )

        def fake_download(tickers, **kwargs):
            self.assertEqual(kwargs["interval"], "1h")
            return {tickers[0]: hourly}

        patched = patch_incomplete_closes(
            {"2330.TW": daily}, download=fake_download, official={}
        )
        self.assertEqual(float(patched["2330.TW"]["Close"].iloc[-1]), 107.5)

    def test_apply_official_bars_fills_nan_close(self):
        daily = _bars(n=3)
        session = daily.index[-1].date()
        daily.loc[daily.index[-1], "Close"] = float("nan")
        official = {
            "2330": (
                session.isoformat(),
                "2330",
                "台積電",
                102.0,
                108.0,
                101.0,
                107.5,
                2000,
                200000,
            )
        }
        patched = apply_official_bars({"2330.TW": daily}, official)
        self.assertEqual(float(patched["2330.TW"]["Close"].iloc[-1]), 107.5)
        self.assertEqual(float(patched["2330.TW"]["High"].iloc[-1]), 108.0)
        self.assertFalse(last_bar_needs_close(patched["2330.TW"]))

    def test_apply_official_bars_skips_other_session(self):
        daily = _bars(n=3)
        daily.loc[daily.index[-1], "Close"] = float("nan")
        official = {
            "2330": ("2020-01-01", "2330", "台積電", 1.0, 1.0, 1.0, 9.0, 1, 1)
        }
        patched = apply_official_bars({"2330.TW": daily}, official)
        self.assertTrue(last_bar_needs_close(patched["2330.TW"]))

    def test_patch_incomplete_closes_prefers_official_over_hourly(self):
        daily = _bars(n=3)
        session = daily.index[-1].date()
        daily.loc[daily.index[-1], "Close"] = float("nan")
        official = {
            "2330": (
                session.isoformat(),
                "2330",
                "台積電",
                102.0,
                108.0,
                101.0,
                109.0,
                2000,
                200000,
            )
        }

        def boom(*_args, **_kwargs):
            raise AssertionError("hourly download must not run after official fill")

        patched = patch_incomplete_closes(
            {"2330.TW": daily}, download=boom, official=official
        )
        self.assertEqual(float(patched["2330.TW"]["Close"].iloc[-1]), 109.0)

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
