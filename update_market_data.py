#!/usr/bin/env python3
"""正式環境用的市場資料更新入口。

每個台股交易日收盤後跑一次,把最近 N 天缺的資料補齊。冪等:已有的日期會跳過。
不常駐、不走 collector.py run(那是給本機長駐用的)。

用法:
  python update_market_data.py
  python update_market_data.py --days 14
  python update_market_data.py 30
  python update_market_data.py --days 730          # 第一次回補歷史
  python update_market_data.py --skip-us --skip-taifex
  python update_market_data.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TW = timezone(timedelta(hours=8))
DEFAULT_DAYS = 14
# 16:00 台灣時間之後,當日 T86 / 融資 / 5 秒指數通常已公布
INCLUDE_TODAY_AFTER_HOUR = 16

log = logging.getLogger(__name__)


def taiwan_now(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(TW)


def include_today(now: datetime | None = None) -> bool:
    """GHA runner 是 UTC,必須用台灣時區判斷收盤後。"""
    return taiwan_now(now).hour >= INCLUDE_TODAY_AFTER_HOUR


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Catch up TWSE / TAIFEX / US market data")
    parser.add_argument("days_pos", nargs="?", type=int, help="legacy: positional day count")
    parser.add_argument("--days", type=int, help="calendar days to catch up (default 14)")
    parser.add_argument("--skip-us", action="store_true")
    parser.add_argument("--skip-taifex", action="store_true")
    parser.add_argument("--skip-stocks", action="store_true")
    parser.add_argument(
        "--stocks",
        action="store_true",
        help="legacy flag; stock daily via MI_INDEX is on by default",
    )
    parser.add_argument("--us-hourly-days", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--include-today",
        action="store_true",
        default=None,
        help="force including today even before 16:00 Taiwan",
    )
    parser.add_argument(
        "--exclude-today",
        action="store_true",
        help="never include today (safe during market hours)",
    )
    args = parser.parse_args(argv)
    args.days = args.days or args.days_pos or DEFAULT_DAYS
    if args.us_hourly_days is None:
        args.us_hourly_days = args.days
    if args.exclude_today:
        args.include_today = False
    elif args.include_today is None:
        args.include_today = include_today()
    return args


def planned_jobs(args: argparse.Namespace) -> list[str]:
    jobs = ["ohlc", "index_foreign_margin"]
    if not args.skip_stocks:
        jobs.append("stock_daily")
    if not args.skip_taifex:
        jobs.append("taifex")
    if not args.skip_us:
        jobs.append("us")
    return jobs


def _table_span(conn: sqlite3.Connection, table: str) -> tuple[int, str | None, str | None]:
    try:
        row = conn.execute(
            f"SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM {table}"
        ).fetchone()
        return int(row[0] or 0), row[1], row[2]
    except sqlite3.OperationalError:
        return 0, None, None


def coverage_lines(twse_path: Path, us_path: Path) -> list[str]:
    lines = []
    if twse_path.exists():
        conn = sqlite3.connect(twse_path)
        try:
            for table in (
                "taiex_daily",
                "taiex_hourly_ohlc",
                "taiex_5sec_open",
                "foreign_daily",
                "margin_stock",
                "stock_daily",
                "taifex_fut_oi",
                "taifex_opt_oi",
            ):
                n, lo, hi = _table_span(conn, table)
                lines.append(f"{table}: {n:,} rows, {lo or '–'} ~ {hi or '–'}")
        finally:
            conn.close()
    else:
        lines.append("twse_data.db missing")
    if us_path.exists():
        conn = sqlite3.connect(us_path)
        try:
            for table in ("us_daily", "us_hourly"):
                n, lo, hi = _table_span(conn, table)
                lines.append(f"{table}: {n:,} rows, {lo or '–'} ~ {hi or '–'}")
        finally:
            conn.close()
    else:
        lines.append("us_data.db missing")
    return lines


def run_jobs(args: argparse.Namespace) -> list[str]:
    """Run catch-up jobs. Returns names of jobs that failed."""
    import backfill
    import collector
    import taifex_collector
    import us_collector

    failed: list[str] = []
    today = taiwan_now().date()
    include = bool(args.include_today)
    days = args.days
    log.info(
        "catch-up days=%s include_today=%s today=%s jobs=%s",
        days, include, today, ",".join(planned_jobs(args)),
    )

    try:
        backfill.backfill_ohlc(days)
    except Exception:
        log.exception("ohlc failed")
        failed.append("ohlc")

    try:
        backfill.backfill(
            days,
            do_index=True,
            do_foreign=True,
            do_margin=True,
            today=today,
            include_today=include,
        )
    except Exception:
        log.exception("index/foreign/margin failed")
        failed.append("index_foreign_margin")

    if "stock_daily" in planned_jobs(args):
        try:
            backfill.backfill_stock_daily(days, today=today, include_today=include)
        except Exception:
            log.exception("stock_daily failed")
            failed.append("stock_daily")

    if "taifex" in planned_jobs(args):
        try:
            d1 = today - timedelta(days=days)
            nf, no = taifex_collector.collect_range(d1, today)
            log.info("taifex wrote futures=%s options=%s", nf, no)
        except Exception:
            log.exception("taifex failed")
            failed.append("taifex")

    if "us" in planned_jobs(args):
        try:
            us_collector.save_daily(["SPY", "QQQ", "IWM"], years=15)
            us_collector.save_hourly(["SPY", "QQQ", "IWM"], days=args.us_hourly_days)
        except Exception:
            log.exception("us failed")
            failed.append("us")

    try:
        collector.sync_stock_master()
    except Exception:
        log.exception("stock master sync failed")

    return failed


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    args = parse_args(argv)
    root = Path(__file__).parent
    jobs = planned_jobs(args)
    log.info("jobs: %s", ", ".join(jobs))
    if args.dry_run:
        print("dry-run", " ".join(jobs), f"days={args.days}",
              f"include_today={args.include_today}")
        return 0

    import collector
    import taifex_collector
    import us_collector
    collector.get_conn().close()
    taifex_collector.get_conn().close()
    us_collector.get_conn().close()

    failed = run_jobs(args)
    lines = coverage_lines(root / "twse_data.db", root / "us_data.db")
    print("=== coverage ===")
    for line in lines:
        print(line)
    if failed:
        log.error("failed jobs: %s", ", ".join(failed))
        return 1
    log.info("market data update complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
