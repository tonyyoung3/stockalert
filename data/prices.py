"""Yahoo Finance helpers that tolerate MultiIndex columns and compute T+N exits."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf

# Match screener alert prices (unadjusted). Checker must use the same basis.
AUTO_ADJUST = False


def to_yahoo_symbol(ticker: str) -> str:
    ticker = (ticker or "").strip()
    if not ticker:
        return ticker
    if "." in ticker:
        return ticker
    return f"{ticker}.TW" if ticker.isdigit() else ticker


def parse_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_float(value) -> float | None:
    try:
        px = float(value)
    except (TypeError, ValueError):
        return None
    if px != px:  # NaN
        return None
    return px


def _series_from_close(close) -> pd.Series | None:
    if close is None:
        return None
    if isinstance(close, pd.DataFrame):
        if close.empty:
            return None
        if close.shape[1] == 1:
            return close.iloc[:, 0]
        return None
    if isinstance(close, pd.Series):
        return close
    return None


def _close_from_multiindex(df: pd.DataFrame, symbol: str | None) -> pd.Series | None:
    cols = df.columns
    if symbol:
        for key in ((symbol, "Close"), ("Close", symbol)):
            if key in cols:
                return _series_from_close(df[key])
    for level in range(cols.nlevels):
        if "Close" not in cols.get_level_values(level):
            continue
        try:
            sub = df.xs("Close", axis=1, level=level)
        except (KeyError, ValueError):
            continue
        series = _series_from_close(sub)
        if series is not None:
            return series
        if isinstance(sub, pd.DataFrame) and symbol and symbol in sub.columns:
            return _series_from_close(sub[symbol])
    return None


def close_series(df: pd.DataFrame | None, symbol: str | None = None) -> pd.Series | None:
    if df is None or getattr(df, "empty", True):
        return None
    series = None
    if isinstance(df.columns, pd.MultiIndex):
        series = _close_from_multiindex(df, symbol)
    if series is None and "Close" in df.columns:
        series = _series_from_close(df["Close"])
        if series is None and symbol is not None:
            close = df["Close"]
            if isinstance(close, pd.DataFrame) and symbol in close.columns:
                series = _series_from_close(close[symbol])
    return series


def last_close(df: pd.DataFrame | None, symbol: str | None = None) -> float | None:
    series = close_series(df, symbol)
    if series is None:
        return None
    series = series.dropna()
    if series.empty:
        return None
    return _as_float(series.iloc[-1])


def drop_incomplete_ohlc(df: pd.DataFrame | None) -> pd.DataFrame:
    """Drop bars missing Open/High/Low/Close.

    After market close Yahoo often keeps the TW daily bar but sets Close=NaN
    until the official print lands. Callers should fill that Close from hourly
    bars first; this drop is the fallback so a leftover NaN candle is not scored.
    """
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame() if df is None else df
    needed = [c for c in ("Open", "High", "Low", "Close") if c in df.columns]
    if not needed:
        return df
    return df.loc[df[needed].notna().all(axis=1)].copy()


def extract_ohlcv(
    data: pd.DataFrame | None,
    symbol: str,
    *,
    complete_only: bool = True,
) -> pd.DataFrame:
    if data is None or getattr(data, "empty", True):
        return pd.DataFrame()

    frame = pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        level0 = data.columns.get_level_values(0)
        if symbol in level0:
            extracted = data[symbol]
            frame = extracted.copy() if isinstance(extracted, pd.DataFrame) else pd.DataFrame()
        else:
            last_level = data.columns.nlevels - 1
            if symbol in data.columns.get_level_values(last_level):
                try:
                    frame = data.xs(symbol, axis=1, level=last_level).copy()
                except (KeyError, ValueError):
                    frame = pd.DataFrame()
    else:
        needed = {"Open", "High", "Low", "Close"}
        if needed.issubset(set(data.columns)):
            frame = data.copy()
    return drop_incomplete_ohlc(frame) if complete_only else frame


def taiwan_session_date(ts) -> date:
    """Trading date in Taiwan. Naive timestamps are treated as UTC."""
    stamp = pd.Timestamp(ts)
    if stamp.tz is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("Asia/Taipei").date()


def last_hourly_close_by_session(hourly: pd.DataFrame | None) -> dict[date, float]:
    """Last 1h close for each Taiwan session (13:00 UTC+8 bar includes 13:30 close)."""
    series = close_series(hourly)
    if series is None and hourly is not None and "Close" in getattr(hourly, "columns", []):
        series = hourly["Close"]
    if series is None:
        return {}
    out: dict[date, float] = {}
    for ts, val in series.items():
        px = _as_float(val)
        if px is None:
            continue
        out[taiwan_session_date(ts)] = px
    return out


def fill_missing_closes(daily: pd.DataFrame | None, hourly: pd.DataFrame | None) -> pd.DataFrame:
    """Fill NaN daily Close from the last hourly bar of the same Taiwan session."""
    if daily is None or getattr(daily, "empty", True) or "Close" not in daily.columns:
        return pd.DataFrame() if daily is None else daily
    session_closes = last_hourly_close_by_session(hourly)
    if not session_closes:
        return daily
    filled = daily.copy()
    close_col = filled.columns.get_loc("Close")
    for i, day in enumerate(bar_dates(filled)):
        if _as_float(filled.iloc[i, close_col]) is not None:
            continue
        px = session_closes.get(day)
        if px is None:
            continue
        filled.iat[i, close_col] = px
    return filled


def patch_incomplete_closes(
    frames: dict[str, pd.DataFrame],
    *,
    chunk_size: int = 50,
    download=None,
) -> dict[str, pd.DataFrame]:
    """Re-fetch 1h bars for tickers whose last daily Close is NaN and fill it in."""
    need = [
        ticker
        for ticker, df in frames.items()
        if df is not None
        and not getattr(df, "empty", True)
        and "Close" in df.columns
        and _as_float(df["Close"].iloc[-1]) is None
    ]
    if not need:
        return frames
    fetch = download if download is not None else download_history
    hourly_map = fetch(need, period="5d", interval="1h", chunk_size=chunk_size)
    out = dict(frames)
    filled_n = 0
    for ticker in need:
        hourly = hourly_map.get(ticker)
        if hourly is None or getattr(hourly, "empty", True):
            continue
        patched = fill_missing_closes(out[ticker], hourly)
        if _as_float(patched["Close"].iloc[-1]) is not None:
            filled_n += 1
        out[ticker] = patched
    print(f"Filled {filled_n}/{len(need)} daily Close=NaN bars from hourly Yahoo data")
    return out


def bar_dates(df: pd.DataFrame) -> list[date]:
    out: list[date] = []
    for ts in df.index:
        stamp = pd.Timestamp(ts)
        if stamp.tz is not None:
            stamp = stamp.tz_convert("Asia/Taipei")
        out.append(stamp.date())
    return out


def signal_index(dates: list[date], alert_date: date) -> int | None:
    """Index of the last bar on or before alert_date (handles weekend alert dates)."""
    idx = None
    for i, day in enumerate(dates):
        if day <= alert_date:
            idx = i
        else:
            break
    return idx


def horizon_exit(
    df: pd.DataFrame,
    alert_date: date | str,
    horizon_td: int,
    symbol: str | None = None,
) -> tuple[date, float] | None:
    """Close of the bar `horizon_td` trading days after the signal bar."""
    if df is None or df.empty or horizon_td < 1:
        return None
    dates = bar_dates(df)
    start = signal_index(dates, parse_date(alert_date))
    if start is None:
        return None
    end = start + horizon_td
    if end >= len(dates):
        return None
    series = close_series(df, symbol)
    if series is None:
        return None
    px = _as_float(series.iloc[end])
    if px is None:
        return None
    return dates[end], px


def calendar_buffer_days(horizon_td: int) -> int:
    """Minimum calendar age before we even try to fetch a T+N exit."""
    return max(horizon_td + 5, int(horizon_td * 7 / 5) + 7)


def _download(
    symbols: list[str] | str,
    *,
    period: str | None = None,
    start: str | date | None = None,
    end: str | date | None = None,
    group_by: str | None = None,
    interval: str = "1d",
):
    kwargs: dict = {
        "interval": interval,
        "threads": True,
        "progress": False,
        "auto_adjust": AUTO_ADJUST,
    }
    if period:
        kwargs["period"] = period
    if start is not None:
        kwargs["start"] = str(start)
    if end is not None:
        kwargs["end"] = str(end)
    if group_by:
        kwargs["group_by"] = group_by
    return yf.download(symbols, **kwargs)


def fetch_latest_close(ticker: str) -> float | None:
    symbol = to_yahoo_symbol(ticker)
    try:
        df = _download(symbol, period="5d")
        return last_close(df, symbol)
    except Exception as exc:
        print(f"  [warn] Could not fetch price for {ticker}: {exc}")
        return None


def fetch_latest_closes(tickers: list[str], chunk_size: int = 50) -> dict[str, float]:
    prices: dict[str, float] = {}
    unique = list(dict.fromkeys(tickers))
    for i in range(0, len(unique), chunk_size):
        chunk = unique[i : i + chunk_size]
        symbols = [to_yahoo_symbol(t) for t in chunk]
        symbol_to_ticker = dict(zip(symbols, chunk))
        try:
            data = _download(symbols, period="5d", group_by="ticker")
        except Exception as exc:
            print(f"  [warn] Batch download failed ({chunk[0]}…): {exc}")
            for ticker in chunk:
                px = fetch_latest_close(ticker)
                if px is not None:
                    prices[ticker] = px
            continue
        for symbol, ticker in symbol_to_ticker.items():
            frame = extract_ohlcv(data, symbol)
            px = last_close(frame, symbol) or last_close(data, symbol)
            if px is None:
                print(f"  [warn] Could not read price for {ticker}")
                continue
            prices[ticker] = px
    return prices


def download_history(
    tickers: list[str],
    *,
    start: str | date | None = None,
    end: str | date | None = None,
    period: str | None = None,
    interval: str = "1d",
    chunk_size: int = 50,
) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    unique = list(dict.fromkeys(tickers))
    for i in range(0, len(unique), chunk_size):
        chunk = unique[i : i + chunk_size]
        symbols = [to_yahoo_symbol(t) for t in chunk]
        symbol_to_ticker = dict(zip(symbols, chunk))
        try:
            data = _download(
                symbols,
                period=period,
                start=start,
                end=end,
                group_by="ticker",
                interval=interval,
            )
        except Exception as exc:
            print(f"  [warn] History download failed ({chunk[0]}…): {exc}")
            for ticker, symbol in zip(chunk, symbols):
                try:
                    one = _download(
                        symbol,
                        period=period,
                        start=start,
                        end=end,
                        interval=interval,
                    )
                    frame = extract_ohlcv(one, symbol)
                    if not frame.empty:
                        frames[ticker] = frame
                except Exception as exc2:
                    print(f"  [warn] Could not download {ticker}: {exc2}")
            continue
        for symbol, ticker in symbol_to_ticker.items():
            frame = extract_ohlcv(data, symbol)
            if frame.empty:
                print(f"  [warn] No OHLCV for {ticker}")
                continue
            frames[ticker] = frame
    return frames
