"""Yahoo one-minute bars and conservative five-minute rise detection.

The index denotes the minute START. Use completed regular-session candles,
including their low, and compare the final close against that window's low.
This is a minute-bar approximation, not a tick feed or latency guarantee.
"""
from __future__ import annotations

import math
import logging
import time as time_module
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
log = logging.getLogger(__name__)


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


def _parse_minutes(frame, codes):
    result = {code: [] for code in codes}
    if frame is None or frame.empty:
        return result
    if frame.index.tz is None:
        raise ValueError("Yahoo returned timezone-naive intraday data")
    symbols = [code + ".TW" for code in codes]
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
        parsed = []
        for ts, row in rows.iterrows():
            values = (float(row.Low), float(row.Close), float(row.Volume))
            # Multi-symbol frames contain all-NaN columns for tickers that failed.
            # Do not mistake those placeholder rows for a successful download.
            if not all(math.isfinite(value) for value in values):
                continue
            parsed.append(Bar(ts.to_pydatetime().astimezone(TW), *values))
        result[code] = parsed
    return result


def fetch_minutes(codes, *, downloader=None, retry_delay=0.5, sleeper=None):
    """Download one-minute bars and retry only symbols missing from the batch.

    Yahoo occasionally returns valid bars for part of a multi-symbol request while
    yfinance logs the rest as "possibly delisted". A low-concurrency retry avoids
    resetting those symbols' monitor warm-up state for a transient partial failure.
    """
    if downloader is None:
        import yfinance as yf
        downloader = yf.download
        # yfinance logs partial batch failures as ERROR even though it returns a
        # usable DataFrame. The monitor reports a concise warning after its retry.
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    sleeper = sleeper or time_module.sleep
    ordered = sorted(set(codes))
    if not ordered:
        return {}
    symbols = [code + ".TW" for code in ordered]
    frame = downloader(
        symbols, period="1d", interval="1m", auto_adjust=False,
        prepost=False, group_by="ticker", threads=4, progress=False, timeout=10,
    )
    result = _parse_minutes(frame, ordered)
    missing = [code for code in ordered if not result[code]]
    if not missing:
        return result
    if retry_delay > 0:
        sleeper(retry_delay)
    try:
        retry = downloader(
            [code + ".TW" for code in missing], period="1d", interval="1m",
            auto_adjust=False, prepost=False, group_by="ticker", threads=2,
            progress=False, timeout=5,
        )
        recovered = _parse_minutes(retry, missing)
    except Exception as exc:
        log.warning("Yahoo minute retry failed for %d symbols: %s",
                    len(missing), type(exc).__name__)
        return result
    for code, bars in recovered.items():
        if bars:
            result[code] = bars
    remaining = [code for code in missing if not result[code]]
    if remaining:
        sample = ",".join(remaining[:8]) + ("…" if len(remaining) > 8 else "")
        log.warning("Yahoo minute data still missing after retry: %d/%d symbols (%s)",
                    len(remaining), len(ordered), sample)
    elif missing:
        log.info("Yahoo minute retry recovered %d symbols", len(missing))
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
