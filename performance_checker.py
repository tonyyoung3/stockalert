"""
Evaluate fixed-horizon returns for screener alerts.

Horizons are trading days after the signal bar (T+5 / T+20 / T+60),
not "today's close after 28 calendar days".

Usage:
    python performance_checker.py
"""

from __future__ import annotations

from datetime import date, timedelta

from alertsdb import get_pending_horizon_jobs, init_db, save_performance
from prices import download_history, horizon_exit, parse_date, to_yahoo_symbol

DEFAULT_HORIZONS = (5, 20, 60)


def evaluate_row(row, frame, horizon_td: int) -> dict | None:
    if not row["price_at_alert"]:
        return None
    symbol = to_yahoo_symbol(row["ticker"])
    exit_bar = horizon_exit(frame, row["alert_date"], horizon_td, symbol)
    if exit_bar is None:
        return None
    exit_date, exit_price = exit_bar
    return_pct = (exit_price - row["price_at_alert"]) / row["price_at_alert"] * 100
    return {
        "alert_id": row["id"],
        "ticker": row["ticker"],
        "pattern": row["pattern_type"],
        "alert_date": row["alert_date"],
        "alert_price": row["price_at_alert"],
        "horizon_td": horizon_td,
        "exit_date": str(exit_date),
        "exit_price": exit_price,
        "return_pct": return_pct,
    }


def run_checks(horizons: tuple[int, ...] = DEFAULT_HORIZONS, today: date | None = None) -> list[dict]:
    init_db()
    jobs = get_pending_horizon_jobs(horizons=horizons, today=today)
    if not jobs:
        print(f"No alerts due for horizons {horizons}.")
        return []

    tickers = list(dict.fromkeys(row["ticker"] for row, _ in jobs))
    oldest = min(parse_date(row["alert_date"]) for row, _ in jobs)
    start = oldest - timedelta(days=5)
    end = (today or date.today()) + timedelta(days=1)
    print(f"Checking {len(jobs)} job(s) across {len(tickers)} ticker(s)...\n")
    frames = download_history(tickers, start=start, end=end)

    results = []
    skipped = 0
    for row, horizon_td in jobs:
        frame = frames.get(row["ticker"])
        measured = evaluate_row(row, frame, horizon_td) if frame is not None else None
        if measured is None:
            print(
                f"  [skip] {row['ticker']} T+{horizon_td} "
                f"({row['alert_date']}) — not enough bars or unreadable close"
            )
            skipped += 1
            continue
        save_performance(
            measured["alert_id"],
            measured["exit_date"],
            measured["exit_price"],
            measured["return_pct"],
            horizon_td=horizon_td,
        )
        results.append(measured)

    if not results:
        print(f"No results saved ({skipped} skipped).")
        return results

    header = (
        f"{'Ticker':<10} {'T+':>3} {'Pattern':<22} {'Alert':<12} "
        f"{'Exit':<12} {'Alert $':>9} {'Exit $':>9} {'Return %':>9}"
    )
    print(header)
    print("-" * len(header))
    for r in sorted(results, key=lambda x: (x["horizon_td"], -x["return_pct"])):
        sign = "+" if r["return_pct"] >= 0 else ""
        print(
            f"{r['ticker']:<10} {r['horizon_td']:>3} {r['pattern']:<22} {r['alert_date']:<12} "
            f"{r['exit_date']:<12} {r['alert_price']:>9.2f} {r['exit_price']:>9.2f} "
            f"{sign}{r['return_pct']:>8.2f}%"
        )
    print(f"\nSaved {len(results)} row(s); skipped {skipped}.")
    return results


def main() -> None:
    run_checks()


if __name__ == "__main__":
    main()
