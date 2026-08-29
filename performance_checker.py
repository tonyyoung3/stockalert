"""
performance_checker.py

Evaluate the return for alerts that are at least 28 calendar days old and
have not been checked yet. Safe to run daily: missing a window no longer
drops alerts.

Usage:
    python performance_checker.py
"""

import yfinance as yf
from datetime import date
from db import init_db, get_pending_alerts, save_performance


def _to_symbol(ticker: str) -> str:
    return f"{ticker}.TW" if ticker.isdigit() else ticker


def fetch_latest_close(ticker: str) -> float | None:
    """Return the most recent daily close for a ticker, or None on failure."""
    symbol = _to_symbol(ticker)
    try:
        df = yf.download(symbol, period="5d", interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as e:
        print(f"  [warn] Could not fetch price for {ticker}: {e}")
        return None


def fetch_latest_closes(tickers: list[str], chunk_size: int = 50) -> dict[str, float]:
    """Batch-download latest closes. Falls back to one-by-one on a failed chunk."""
    prices: dict[str, float] = {}
    unique = list(dict.fromkeys(tickers))
    for i in range(0, len(unique), chunk_size):
        chunk = unique[i:i + chunk_size]
        symbols = [_to_symbol(t) for t in chunk]
        symbol_to_ticker = dict(zip(symbols, chunk))
        try:
            data = yf.download(
                symbols,
                period="5d",
                interval="1d",
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=True,
            )
        except Exception as e:
            print(f"  [warn] Batch download failed ({chunk[0]}…): {e}")
            for ticker in chunk:
                px = fetch_latest_close(ticker)
                if px is not None:
                    prices[ticker] = px
            continue

        for symbol, ticker in symbol_to_ticker.items():
            try:
                part = data[symbol] if len(symbols) > 1 else data
                if part.empty:
                    continue
                prices[ticker] = float(part["Close"].iloc[-1])
            except Exception as e:
                print(f"  [warn] Could not read price for {ticker}: {e}")
    return prices


def main() -> None:
    init_db()

    pending = get_pending_alerts()
    if not pending:
        print("No alerts due for performance check (age >= 28 days, unchecked).")
        return

    print(f"Checking {len(pending)} alert(s)...\n")
    prices = fetch_latest_closes([row["ticker"] for row in pending])

    results = []
    for row in pending:
        ticker = row["ticker"]
        pattern_type = row["pattern_type"]
        alert_date = row["alert_date"]
        price_at_alert = row["price_at_alert"]
        alert_id = row["id"]

        current_price = prices.get(ticker)
        if current_price is None:
            print(f"  [skip] {ticker} — could not retrieve current price")
            continue

        check_date = str(date.today())
        return_pct = (current_price - price_at_alert) / price_at_alert * 100

        save_performance(alert_id, check_date, current_price, return_pct)
        results.append({
            "ticker":        ticker,
            "pattern":       pattern_type,
            "alert_date":    alert_date,
            "alert_price":   price_at_alert,
            "current_price": current_price,
            "return_pct":    return_pct,
        })

    if not results:
        print("No results saved.")
        return

    header = f"{'Ticker':<10} {'Pattern':<25} {'Alert Date':<12} {'Alert $':>9} {'Now $':>9} {'Return %':>9}"
    print(header)
    print("-" * len(header))
    for r in sorted(results, key=lambda x: x["return_pct"], reverse=True):
        sign = "+" if r["return_pct"] >= 0 else ""
        print(
            f"{r['ticker']:<10} {r['pattern']:<25} {r['alert_date']:<12} "
            f"{r['alert_price']:>9.2f} {r['current_price']:>9.2f} "
            f"{sign}{r['return_pct']:>8.2f}%"
        )


if __name__ == "__main__":
    main()
