"""Yahoo Finance helpers that tolerate the various yfinance DataFrame layouts."""

from __future__ import annotations

import pandas as pd
import yfinance as yf


def to_yahoo_symbol(ticker: str) -> str:
    """Map a stored ticker (often bare digits like 2330) to a Yahoo symbol."""
    ticker = ticker.strip()
    if not ticker:
        return ticker
    if "." in ticker:
        return ticker
    return f"{ticker}.TW" if ticker.isdigit() else ticker


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


def last_close(df: pd.DataFrame | None, symbol: str | None = None) -> float | None:
    """Last non-null Close from a yfinance-like frame, or None if unreadable."""
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
    if series is None:
        return None

    series = series.dropna()
    if series.empty:
        return None
    return _as_float(series.iloc[-1])


def extract_ohlcv(data: pd.DataFrame | None, symbol: str) -> pd.DataFrame:
    """Extract a single-ticker OHLCV frame from a yfinance download result."""
    if data is None or getattr(data, "empty", True):
        return pd.DataFrame()

    if isinstance(data.columns, pd.MultiIndex):
        level0 = data.columns.get_level_values(0)
        if symbol in level0:
            frame = data[symbol]
            return frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        last_level = data.columns.nlevels - 1
        if symbol in data.columns.get_level_values(last_level):
            try:
                return data.xs(symbol, axis=1, level=last_level).copy()
            except (KeyError, ValueError):
                return pd.DataFrame()
        return pd.DataFrame()

    needed = {"Open", "High", "Low", "Close"}
    if needed.issubset(set(data.columns)):
        return data.copy()
    return pd.DataFrame()


def _download(symbols: list[str] | str, period: str, group_by: str | None = None):
    kwargs = dict(
        period=period,
        interval="1d",
        threads=True,
        progress=False,
        auto_adjust=True,
    )
    if group_by:
        kwargs["group_by"] = group_by
    return yf.download(symbols, **kwargs)


def fetch_latest_closes(tickers: list[str], chunk_size: int = 50) -> dict[str, float]:
    """Batch-download latest closes. Falls back to one-by-one on a failed chunk."""
    prices: dict[str, float] = {}
    unique = list(dict.fromkeys(tickers))
    for i in range(0, len(unique), chunk_size):
        chunk = unique[i : i + chunk_size]
        symbols = [to_yahoo_symbol(t) for t in chunk]
        symbol_to_ticker = dict(zip(symbols, chunk))
        try:
            data = _download(symbols, period="5d", group_by="ticker")
        except Exception as e:
            print(f"  [warn] Batch download failed ({chunk[0]}…): {e}")
            for ticker, symbol in zip(chunk, symbols):
                px = fetch_latest_close(ticker)
                if px is not None:
                    prices[ticker] = px
            continue

        for symbol, ticker in symbol_to_ticker.items():
            frame = extract_ohlcv(data, symbol)
            px = last_close(frame, symbol)
            if px is None:
                px = last_close(data, symbol)
            if px is None:
                print(f"  [warn] Could not read price for {ticker}")
                continue
            prices[ticker] = px
    return prices


def fetch_latest_close(ticker: str) -> float | None:
    """Return the most recent daily close for a ticker, or None on failure."""
    symbol = to_yahoo_symbol(ticker)
    try:
        df = _download(symbol, period="5d")
        return last_close(df, symbol)
    except Exception as e:
        print(f"  [warn] Could not fetch price for {ticker}: {e}")
        return None


def download_history(
    tickers: list[str],
    period: str = "2mo",
    chunk_size: int = 80,
) -> dict[str, pd.DataFrame]:
    """Download daily OHLCV for many tickers, chunked to reduce Yahoo failures."""
    frames: dict[str, pd.DataFrame] = {}
    unique = list(dict.fromkeys(tickers))
    for i in range(0, len(unique), chunk_size):
        chunk = unique[i : i + chunk_size]
        try:
            data = _download(chunk, period=period, group_by="ticker")
        except Exception as e:
            print(f"  [warn] History download failed ({chunk[0]}…): {e}")
            for ticker in chunk:
                try:
                    one = _download(ticker, period=period)
                    frame = extract_ohlcv(one, ticker)
                    if not frame.empty:
                        frames[ticker] = frame
                except Exception as e2:
                    print(f"  [warn] Could not download {ticker}: {e2}")
            continue

        for ticker in chunk:
            frame = extract_ohlcv(data, ticker)
            if frame.empty:
                print(f"  [warn] No OHLCV for {ticker}")
                continue
            frames[ticker] = frame
    return frames
