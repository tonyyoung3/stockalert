"""
performance_checker.py

Evaluate the return for alerts that are at least 28 calendar days old and
have not been checked yet. Safe to run daily: missing a window no longer
drops alerts.

Usage:
    python performance_checker.py
"""

from datetime import date

from db import init_db, get_pending_alerts, save_performance
from prices import fetch_latest_closes


def main() -> None:
    init_db()

    pending = get_pending_alerts()
    if not pending:
        print("No alerts due for performance check (age >= 28 days, unchecked).")
        return

    print(f"Checking {len(pending)} alert(s)...\n")
    prices = fetch_latest_closes([row["ticker"] for row in pending])
    print(f"Retrieved {len(prices)} / {len({row['ticker'] for row in pending})} unique ticker price(s).\n")

    results = []
    skipped = 0
    for row in pending:
        ticker = row["ticker"]
        pattern_type = row["pattern_type"]
        alert_date = row["alert_date"]
        price_at_alert = row["price_at_alert"]
        alert_id = row["id"]

        current_price = prices.get(ticker)
        if current_price is None:
            print(f"  [skip] {ticker} — could not retrieve current price")
            skipped += 1
            continue
        if not price_at_alert:
            print(f"  [skip] {ticker} — alert price is zero, cannot compute return")
            skipped += 1
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
        print(f"No results saved ({skipped} skipped).")
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
    print(f"\nSaved {len(results)} performance row(s); skipped {skipped}.")


if __name__ == "__main__":
    main()
