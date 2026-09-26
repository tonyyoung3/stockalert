"""Yahoo one-minute bars and conservative five-minute rise detection.

The index denotes the minute START. Use completed regular-session candles,
including their low, and compare the final close against that window's low.
This is a minute-bar approximation, not a tick feed or latency guarantee.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from web.tw_calendar import HOLIDAY_YEARS, TW, is_tw_trading_day, taiwan_now

MAX_DELAY = 120
WINDOW_MINUTES = 5
DEFAULT_THRESHOLD_PCT = Decimal("2")
SESSION_START = time(9)
DATA_END = time(12)
MONITOR_END = time(12, 2)


@dataclass(frozen=True)
class Bar:
    start: datetime
    low: float
    close: float
    volume: float


def monitoring_session(now):
    now = taiwan_now(now)
    # Two minutes after the requested noon cutoff allow the 11:59 candle to arrive.
    return (now.year in HOLIDAY_YEARS and is_tw_trading_day(now.date())
            and SESSION_START <= now.time() < MONITOR_END)


def fetch_minutes(codes, *, downloader=None):
    import yfinance as yf
    symbols = [code + ".TW" for code in sorted(codes)]
    frame = (downloader or yf.download)(
        symbols, period="1d", interval="1m", auto_adjust=False,
        prepost=False, group_by="ticker", threads=4, progress=False, timeout=10,
    )
    result = {code: [] for code in codes}
    if frame is None or frame.empty:
        return result
    if frame.index.tz is None:
        raise ValueError("Yahoo returned timezone-naive intraday data")
    for code in result:
        symbol = code + ".TW"
        if getattr(frame.columns, "nlevels", 1) > 1:
            if symbol not in frame.columns.get_level_values(0):
                continue
            rows = frame[symbol]
        elif len(symbols) == 1:
            rows = frame
        else:
            continue
        if not {"Low", "Close", "Volume"}.issubset(rows.columns):
            continue
        result[code] = [Bar(ts.to_pydatetime().astimezone(TW),
                            float(row.Low), float(row.Close), float(row.Volume))
                        for ts, row in rows.iterrows()]
    return result


def evaluate(bars, now, threshold_pct=DEFAULT_THRESHOLD_PCT):
    now = taiwan_now(now)
    threshold = Decimal(str(threshold_pct))
    if not Decimal("0.1") <= threshold <= Decimal("20"):
        raise ValueError("threshold_pct must be between 0.1 and 20")
    if not monitoring_session(now):
        return {"status": "off_hours"}
    eligible = {}
    for bar in bars:
        if bar.start.tzinfo is None:
            continue
        start = bar.start.astimezone(TW)
        if (start.date() != now.date() or not SESSION_START <= start.time() < DATA_END
                or start.second or start.microsecond or start + timedelta(minutes=1) > now):
            continue
        eligible[start] = Bar(start, bar.low, bar.close, bar.volume)
    if not eligible:
        return {"status": "missing"}
    newest = max(eligible)
    end = newest + timedelta(minutes=1)
    delay = (now - end).total_seconds()
    if delay > MAX_DELAY:
        return {"status": "stale", "delay_seconds": delay}
    window = [eligible.get(newest - timedelta(minutes=i))
              for i in reversed(range(WINDOW_MINUTES))]
    if any(bar is None for bar in window):
        return {"status": "incomplete", "delay_seconds": delay}
    if any(not all(math.isfinite(v) and v > 0 for v in (b.low, b.close, b.volume))
           or b.low > b.close for b in window):
        return {"status": "invalid", "delay_seconds": delay}
    low_bar = min(window, key=lambda bar: bar.low)
    low, close = Decimal(str(low_bar.low)), Decimal(str(window[-1].close))
    rise = (close / low - 1) * 100
    return {"status": "ok", "triggered": close >= low * (1 + threshold / 100),
            "rise_pct": float(rise), "low": float(low), "close": float(close),
            "threshold_pct": float(threshold),
            "baseline_minute": low_bar.start.isoformat(),
            "window_start": window[0].start.isoformat(), "bar_end": end.isoformat(),
            "delay_seconds": delay}
