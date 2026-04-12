"""
performance_checker.py

Run this script manually (or on a schedule) to evaluate the 1-month return
for any alerts that were raised 28–35 trading days ago and haven't been
checked yet.

Usage:
    python performance_checker.py
"""

import yfinance as yf
from datetime import date
from db import init_db, get_pending_alerts, save_performance


def fetch_latest_close(ticker: str) -> float | None:
    """Return the most recent daily close for a ticker, or None on failure."""
    # Append .TW suffix if the ticker looks like a Taiwan stock (all digits)
    symbol = f"{ticker}.TW" if ticker.isdigit() else ticker
    try:
        df = yf.download(symbol, period="5d", interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as e:
        print(f"  [warn] Could not fetch price for {ticker}: {e}")
        return None


def main() -> None:
    init_db()

    pending = get_pending_alerts()
    if not pending:
        print("No alerts due for performance check (28–35 days window).")
        return

    print(f"Checking {len(pending)} alert(s)...\n")

    results = []
    for row in pending:
        ticker        = row["ticker"]
        pattern_type  = row["pattern_type"]
        alert_date    = row["alert_date"]
        price_at_alert = row["price_at_alert"]
        alert_id      = row["id"]

        current_price = fetch_latest_close(ticker)
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

    # Print summary table
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
