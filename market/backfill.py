#!/usr/bin/env python3
"""回補歷史資料(預設近兩年)
- 大盤指數:證交所「每5秒指數統計」取每個整點 (09:00–13:00) 及收盤 13:30
- 三大法人:T86 一次請求寫外資／投信／自營商;已有的日期跳過

用法:
  python -m market.backfill            # 回補兩年
  python -m market.backfill 90         # 回補近 90 天
  python -m market.backfill index 90   # 只回補指數
  python -m market.backfill foreign 90 # 只回補 T86(外資+投信+自營商)
  python -m market.backfill institutional      # 只補 foreign 已有、trust/dealer 缺少的日期
  python -m market.backfill institutional 14   # 同上,限近 14 個日曆天
  python -m market.backfill institutional --dry-run

Actions cache local foreign_daily is often much shorter than Turso
foreign_daily. When TURSO_* is set, institutional gaps use Turso
(and/or local) foreign dates, then push only the T86 tables.
  python -m market.backfill margin 90  # 只回補融資融券
  python -m market.backfill ohlc       # 只回補大盤日K
  python -m market.backfill stock 2330,2454 730  # 回補指定個股日K
  python -m market.backfill stock              # 回補主檔全部股票日K(量大,會先確認)
  python -m market.backfill stock-daily 14     # 上市+上櫃日K(MI_INDEX + 櫃買,寫進 stock_daily)

支援中斷續跑:已存在的日期會自動跳過。
每次請求間隔 4 秒(證交所有流量限制,請勿調低)。
"""
import sys
import time
import logging
from datetime import date, timedelta, datetime

import requests

from market.collector import (get_conn, fetch_t86, persist_t86,
                       fetch_taiex_ohlc_month,
                       fetch_index_5sec, hourly_ohlc_from_5sec, fetch_margin,
                       fetch_stock_month, fetch_stock_day_all,
                       fetch_tpex_stock_day_all, persist_stock_daily,
                       update_stock_master, MIN_COMBINED_STOCK_DAILY,
                       HEADERS)
from web.tw_calendar import is_tw_trading_day

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SLEEP = 4  # 秒,請求間隔
HOURS = ["09:00:00", "10:00:00", "11:00:00", "12:00:00", "13:00:00"]
# Catch-up only needs dates inside the window; scanning the full table grows with history.
_DATE_TABLES = frozenset({
    "stock_daily",
    "taiex_hourly",
    "taiex_hourly_ohlc",
    "taiex_5sec_open",
    "foreign_daily",
    "trust_daily",
    "dealer_daily",
    "margin_stock",
    "margin_total",
    "taiex_daily",
})


def sample_hours(day: date, rows: list[tuple]) -> list[tuple]:
    """從每5秒資料取各整點與收盤的即時值(給 taiex_hourly 折線圖用)。"""
    out, i = [], 0
    d = day.isoformat()
    for target in HOURS:
        while i < len(rows) and rows[i][0] < target:
            i += 1
        if i >= len(rows):
            break
        out.append((f"{d}T{target[:5]}:00", d, rows[i][1], None, None))
    if rows:  # 收盤(最後一筆,約 13:30)
        out.append((f"{d}T13:30:00", d, rows[-1][1], None, None))
    return out


def existing_dates(conn, table, since: str | None = None) -> set:
    """Distinct trade_date values, optionally only dates on/after `since` (YYYY-MM-DD)."""
    if table not in _DATE_TABLES:
        raise ValueError(f"unknown date table: {table!r}")
    sql = f"SELECT DISTINCT trade_date FROM {table}"
    params: tuple = ()
    if since:
        sql += " WHERE trade_date >= ?"
        params = (since,)
    return {r[0] for r in conn.execute(sql, params)}


def backfill_ohlc(days: int):
    """逐月回補日 K(每月一次請求)。"""
    conn = get_conn()
    start = date.today() - timedelta(days=days)
    month = date(start.year, start.month, 1)
    n = 0
    while month <= date.today():
        try:
            rows = fetch_taiex_ohlc_month(month)
            if rows:
                with conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO taiex_daily VALUES (?,?,?,?,?)", rows)
                n += len(rows)
                log.info("%s 日K %d 筆", month.strftime("%Y-%m"), len(rows))
        except Exception as e:
            log.warning("%s 日K失敗: %s", month.strftime("%Y-%m"), e)
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        time.sleep(SLEEP)
    log.info("日 K 回補完成,共 %d 筆", n)


def backfill_stocks(stock_ids: list[str] | None, days: int):
    """逐月回補個股日K(STOCK_DAY,每檔每月一次請求)。
    stock_ids 為 None 時回補 stocks 主檔的全部股票(會先要求確認)。
    已涵蓋回補區間的股票自動跳過,中斷後可續跑。"""
    conn = get_conn()
    start = date.today() - timedelta(days=days)
    if stock_ids is None:
        stock_ids = [r[0] for r in conn.execute(
            "SELECT stock_id FROM stocks ORDER BY stock_id")]
        if not stock_ids:
            raise SystemExit("stocks 主檔是空的,請先執行: python -m market.collector sync")
        months = days // 30 + 1
        est = len(stock_ids) * months
        print(f"將回補 {len(stock_ids)} 檔 × 約 {months} 個月 = 約 {est:,} 次請求,"
              f"以 {SLEEP} 秒間隔預估 {est*SLEEP/3600:.1f} 小時(可中斷續跑)")
        if input("確定執行?(y/N) ").strip().lower() != "y":
            raise SystemExit("已取消")
    # 續跑:已涵蓋區間(頭尾都有資料)的股票跳過
    done = {r[0] for r in conn.execute(
        "SELECT stock_id FROM stock_daily GROUP BY stock_id "
        "HAVING MIN(trade_date) <= ? AND MAX(trade_date) >= ?",
        ((start + timedelta(days=40)).isoformat(),
         (date.today() - timedelta(days=40)).isoformat()))}
    skip = [s for s in stock_ids if s in done]
    if skip:
        log.info("跳過已完成的 %d 檔", len(skip))
    n = 0
    for sid in [s for s in stock_ids if s not in done]:
        month = date(start.year, start.month, 1)
        while month <= date.today():
            try:
                rows = fetch_stock_month(sid, month)
                if rows:
                    with conn:
                        conn.executemany(
                            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)", rows)
                    n += len(rows)
                    log.info("%s %s %d 筆", sid, month.strftime("%Y-%m"), len(rows))
            except Exception as e:
                log.warning("%s %s 失敗: %s", sid, month.strftime("%Y-%m"), e)
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            time.sleep(SLEEP)
    log.info("個股日K回補完成,共 %d 筆", n)


def window_end(today: date, include_today: bool) -> date:
    """盤中回補預設不含今天(5 秒資料還不完整);收盤後的正式排程要含今天。"""
    return today if include_today else today - timedelta(days=1)


def stock_daily_counts(conn, since: str | None = None) -> dict[str, int]:
    """Per-date row counts, optionally only dates on/after `since` (YYYY-MM-DD)."""
    sql = "SELECT trade_date, COUNT(*) FROM stock_daily"
    params: tuple = ()
    if since:
        sql += " WHERE trade_date >= ?"
        params = (since,)
    sql += " GROUP BY trade_date"
    return {
        row[0]: int(row[1])
        for row in conn.execute(sql, params)
    }


def backfill_stock_daily(
    days: int,
    today: date | None = None,
    include_today: bool = False,
    min_combined: int = MIN_COMBINED_STOCK_DAILY,
):
    """上市 MI_INDEX + 上櫃櫃買收盤行情,寫進既有 stock_daily。

    已有足夠檔數(上市+上櫃)的日期跳過。只有上市的日期會再抓上櫃,不會重抓上市。
    交易日有上市、上櫃卻抓空或拋錯時 raise,讓正式排程變紅並打 Slack。
    Catch-up counts are limited to the window so we do not GROUP BY the full table.
    """
    conn = get_conn()
    today = today or date.today()
    start = today - timedelta(days=days)
    counts = stock_daily_counts(conn, since=start.isoformat())
    end = window_end(today, include_today)
    n = 0
    twse_days = tpex_days = 0
    problems: list[str] = []
    day = start
    while day <= end:
        if is_tw_trading_day(day):
            ds = day.isoformat()
            have = counts.get(ds, 0)
            need_twse = have == 0
            need_tpex = have < min_combined
            if need_twse or need_tpex:
                twse_rows: list[tuple] = []
                tpex_rows: list[tuple] = []
                twse_err = tpex_err = None
                if need_twse:
                    try:
                        twse_rows = fetch_stock_day_all(day)
                    except Exception as e:
                        twse_err = e
                        log.warning("%s 上市日K失敗: %s", ds, e)
                    time.sleep(SLEEP)
                if need_tpex:
                    try:
                        tpex_rows = fetch_tpex_stock_day_all(day)
                    except Exception as e:
                        tpex_err = e
                        log.warning("%s 上櫃日K失敗: %s", ds, e)
                    time.sleep(SLEEP)
                rows = twse_rows + tpex_rows
                if rows:
                    with conn:
                        persist_stock_daily(conn, rows)
                    n += 1
                    if twse_rows:
                        twse_days += 1
                    if tpex_rows:
                        tpex_days += 1
                    counts[ds] = have + len(rows)
                    log.info(
                        "個股日K %s:上市 %d 檔、上櫃 %d 檔",
                        ds, len(twse_rows) if need_twse else have, len(tpex_rows),
                    )
                if twse_err:
                    problems.append(f"{ds} twse:{twse_err}")
                if tpex_err:
                    problems.append(f"{ds} tpex:{tpex_err}")
                elif need_tpex and not tpex_rows and (twse_rows or have > 0):
                    problems.append(f"{ds} tpex:empty")
            else:
                log.info("個股日K %s: already complete (%d names), skip", ds, have)
        day += timedelta(days=1)
    log.info(
        "個股日K(上市+上櫃)回補完成:%d 天 (twse_days=%d tpex_days=%d)",
        n, twse_days, tpex_days,
    )
    if problems:
        raise RuntimeError("stock_daily incomplete: " + "; ".join(problems))
    return n


def backfill(days: int, do_index=True, do_foreign=True, do_margin=True,
             today: date | None = None, include_today: bool = False):
    conn = get_conn()
    today = today or date.today()
    start = today - timedelta(days=days)
    since = start.isoformat()
    have_idx = (existing_dates(conn, "taiex_hourly", since=since)
                & existing_dates(conn, "taiex_hourly_ohlc", since=since)
                & existing_dates(conn, "taiex_5sec_open", since=since)) if do_index else set()
    have_for = existing_dates(conn, "foreign_daily", since=since) if do_foreign else set()
    have_trust = existing_dates(conn, "trust_daily", since=since) if do_foreign else set()
    have_dealer = existing_dates(conn, "dealer_daily", since=since) if do_foreign else set()
    have_mar = existing_dates(conn, "margin_stock", since=since) if do_margin else set()
    end = window_end(today, include_today)
    day = start
    n_idx = n_for = n_mar = 0
    while day <= end:
        if is_tw_trading_day(day):
            ds = day.isoformat()
            if do_index and ds not in have_idx:
                try:
                    raw = fetch_index_5sec(day)
                    if raw:
                        sampled = sample_hours(day, raw)
                        ohlc = hourly_ohlc_from_5sec(day, raw)
                        from market.collector import save_open_5sec
                        with conn:
                            conn.executemany(
                                "INSERT OR REPLACE INTO taiex_hourly VALUES (?,?,?,?,?)", sampled)
                            conn.executemany(
                                "INSERT OR REPLACE INTO taiex_hourly_ohlc VALUES (?,?,?,?,?,?)", ohlc)
                            save_open_5sec(conn, day, raw)
                        n_idx += 1
                        log.info("%s 指數 %d 筆 + 小時K %d 根", ds, len(sampled), len(ohlc))
                except Exception as e:
                    log.warning("%s 指數失敗: %s", ds, e)
                time.sleep(SLEEP)
            # One T86 call fills foreign + trust + dealer. Skip only when all three exist.
            if do_foreign and (ds not in have_for or ds not in have_trust or ds not in have_dealer):
                try:
                    tables = fetch_t86(day)
                    if tables.foreign:
                        with conn:
                            persist_t86(conn, tables)
                        n_for += 1
                        have_for.add(ds)
                        have_trust.add(ds)
                        have_dealer.add(ds)
                        log.info(
                            "%s T86 外資 %d / 投信 %d / 自營商 %d 檔",
                            ds, len(tables.foreign), len(tables.trust), len(tables.dealer),
                        )
                except Exception as e:
                    log.warning("%s T86 失敗: %s", ds, e)
                time.sleep(SLEEP)
            if do_margin and ds not in have_mar:
                try:
                    totals, stocks = fetch_margin(day)
                    if stocks:
                        with conn:
                            conn.executemany(
                                "INSERT OR REPLACE INTO margin_total VALUES (?,?,?,?,?,?,?)", totals)
                            conn.executemany(
                                "INSERT OR REPLACE INTO margin_stock VALUES (?,?,?,?,?,?,?,?,?)", stocks)
                        n_mar += 1
                        log.info("%s 融資融券 %d 檔", ds, len(stocks))
                except Exception as e:
                    log.warning("%s 融資融券失敗: %s", ds, e)
                time.sleep(SLEEP)
        day += timedelta(days=1)
    log.info("完成:指數回補 %d 天、T86 回補 %d 天、融資融券回補 %d 天", n_idx, n_for, n_mar)


INSTITUTIONAL_TABLES = ("foreign_daily", "trust_daily", "dealer_daily")


def _table_span(conn, table: str) -> tuple[int, str | None, str | None]:
    row = conn.execute(
        f"SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM {table}"
    ).fetchone()
    return int(row[0] or 0), row[1], row[2]


def load_remote_institutional_dates(
    since: str | None = None,
    remote_dates: dict[str, set[str]] | None = None,
    remote=None,
) -> dict[str, set[str]]:
    """Per-table distinct trade_date sets from Turso, or an injected mock.

    Empty sets if TURSO_* is unset and no mock / remote is given.
    """
    if remote_dates is not None:
        out = {}
        for table in INSTITUTIONAL_TABLES:
            dates = set(remote_dates.get(table) or ())
            if since:
                dates = {d for d in dates if d >= since}
            out[table] = dates
        return out
    from data import cloud_db
    if remote is None and not cloud_db.configured():
        return {table: set() for table in INSTITUTIONAL_TABLES}
    close = False
    if remote is None:
        remote = cloud_db.connect_remote()
        close = True
    try:
        return {
            table: cloud_db.remote_distinct_dates(table, since=since, remote=remote)
            for table in INSTITUTIONAL_TABLES
        }
    finally:
        if close:
            try:
                remote.close()
            except Exception:
                pass


def institutional_coverage(
    conn,
    remote_dates: dict[str, set[str]] | None = None,
) -> list[str]:
    lines = []
    for table in INSTITUTIONAL_TABLES:
        n, lo, hi = _table_span(conn, table)
        n_dates = len(existing_dates(conn, table))
        lines.append(
            f"local {table}: {n:,} rows, {n_dates} dates, {lo or '–'} ~ {hi or '–'}"
        )
    if remote_dates is not None:
        for table in INSTITUTIONAL_TABLES:
            dates = remote_dates.get(table) or set()
            lo = min(dates) if dates else None
            hi = max(dates) if dates else None
            lines.append(
                f"turso {table}: {len(dates)} dates, {lo or '–'} ~ {hi or '–'}"
            )
    return lines


def missing_institutional_dates(
    conn,
    since: str | None = None,
    remote_dates: dict[str, set[str]] | None = None,
) -> list[str]:
    """Foreign dates that still need a T86 fetch for trust/dealer.

    Local-only (`remote_dates is None`): dates in local foreign_daily that
    lack local trust_daily or dealer_daily.

    Turso-aware (`remote_dates` injected, typically from Turso): missing =
    distinct trade_date in Turso foreign_daily and/or local foreign_daily
    that lack both trust and dealer on local *and* lack both on Turso.
    Dates already complete on the source of truth are skipped:

    - Turso already has trust and dealer → skip (prefer Turso).
    - Local already has trust and dealer → skip fetch (push will copy).

    Do not treat Actions-cache local foreign_daily as the full history.
    That cache is often ~85 days while Turso foreign_daily is ~510 dates.
    """
    local_foreign = existing_dates(conn, "foreign_daily", since=since)
    local_trust = existing_dates(conn, "trust_daily", since=since)
    local_dealer = existing_dates(conn, "dealer_daily", since=since)
    if remote_dates is None:
        return sorted(
            d for d in local_foreign if d not in local_trust or d not in local_dealer
        )
    remote_foreign = set(remote_dates.get("foreign_daily") or ())
    remote_trust = set(remote_dates.get("trust_daily") or ())
    remote_dealer = set(remote_dates.get("dealer_daily") or ())
    if since:
        remote_foreign = {d for d in remote_foreign if d >= since}
        remote_trust = {d for d in remote_trust if d >= since}
        remote_dealer = {d for d in remote_dealer if d >= since}
    foreign = local_foreign | remote_foreign
    missing = []
    for ds in foreign:
        if ds in remote_trust and ds in remote_dealer:
            continue
        if ds in local_trust and ds in local_dealer:
            continue
        missing.append(ds)
    return sorted(missing)


def institutional_push_days(
    conn,
    today: date,
    remote_dates: dict[str, set[str]] | None = None,
    default: int = 14,
) -> int:
    """Calendar days so a Turso push covers still-incomplete T86 dates.

    When Turso date sets are present, the window starts at the oldest date
    that Turso foreign (or local) has but Turso trust/dealer still lacks.
    That is the filled span to push — not the generic 14-day catch-up, and
    not "whatever happens to be in the Actions cache."
    """
    local_foreign = existing_dates(conn, "foreign_daily")
    local_trust = existing_dates(conn, "trust_daily")
    local_dealer = existing_dates(conn, "dealer_daily")
    if remote_dates is not None:
        remote_foreign = set(remote_dates.get("foreign_daily") or ())
        remote_trust = set(remote_dates.get("trust_daily") or ())
        remote_dealer = set(remote_dates.get("dealer_daily") or ())
        candidates = local_foreign | local_trust | local_dealer | remote_foreign
        need = [
            ds for ds in candidates
            if ds not in remote_trust or ds not in remote_dealer
        ]
    else:
        need = [
            ds for ds in local_foreign
            if ds not in local_trust or ds not in local_dealer
        ]
        if not need:
            need = list(local_foreign | local_trust | local_dealer)
    if not need:
        return default
    try:
        lo = min(need)
        return max(default, (today - date.fromisoformat(lo)).days + 7)
    except ValueError:
        return default


def backfill_institutional_gaps(
    days: int | None = None,
    today: date | None = None,
    dry_run: bool = False,
    remote_dates: dict[str, set[str]] | None = None,
) -> int:
    """Fetch T86 only for foreign dates missing trust or dealer.

    One T86 call writes foreign+trust+dealer. Rate limit: SLEEP seconds.

    When TURSO_* is configured (or `remote_dates` is injected), missing
    dates come from Turso foreign_daily ∪ local foreign_daily. Dates that
    already have both trust and dealer on Turso, or already have both
    locally, are not re-downloaded.
    """
    conn = get_conn()
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat() if days is not None else None
    loaded = None
    try:
        if remote_dates is not None:
            loaded = load_remote_institutional_dates(since=since, remote_dates=remote_dates)
        else:
            from data import cloud_db
            if cloud_db.configured():
                loaded = load_remote_institutional_dates(since=since)
                log.info(
                    "institutional gaps: using Turso foreign dates "
                    "(cache-local foreign span is not the Turso span)"
                )
    except Exception:
        log.exception(
            "Turso date lookup failed; falling back to local foreign_daily"
        )
        loaded = None
    missing = missing_institutional_dates(conn, since=since, remote_dates=loaded)
    source = "turso+local foreign_daily" if loaded is not None else "local foreign_daily"
    log.info(
        "institutional gaps: %d dates missing vs %s%s",
        len(missing),
        source,
        f" (since {since})" if since else " (full foreign span)",
    )
    for line in institutional_coverage(conn, remote_dates=loaded):
        log.info("coverage %s", line)
    if dry_run:
        if missing:
            log.info("dry-run would fetch %s .. %s", missing[0], missing[-1])
        return 0
    n = 0
    for ds in missing:
        day = date.fromisoformat(ds)
        try:
            tables = fetch_t86(day)
            if tables.foreign or tables.trust or tables.dealer:
                with conn:
                    persist_t86(conn, tables)
                n += 1
                log.info(
                    "%s T86 外資 %d / 投信 %d / 自營商 %d 檔",
                    ds, len(tables.foreign), len(tables.trust), len(tables.dealer),
                )
            else:
                log.warning("%s T86 無資料(foreign_daily 有此日,可能假日或尚未公布)", ds)
        except Exception as e:
            log.warning("%s T86 失敗: %s", ds, e)
        time.sleep(SLEEP)
    log.info("institutional gap fill: wrote %d days", n)
    for line in institutional_coverage(conn, remote_dates=loaded):
        log.info("coverage %s", line)
    return n


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = "all"
    if args and args[0] in (
        "index", "foreign", "institutional", "ohlc", "margin", "stock", "stock-daily",
    ):
        mode = args.pop(0)
    if mode == "institutional":
        dry = "--dry-run" in args
        args = [a for a in args if a != "--dry-run"]
        days = int(args[0]) if args else None
        backfill_institutional_gaps(days=days, dry_run=dry)
        raise SystemExit
    if mode == "stock-daily":
        days = int(args[0]) if args else 14
        backfill_stock_daily(days)
        raise SystemExit
    if mode == "stock":
        # 用法: python backfill.py stock                → 全部股票, 730天
        #      python backfill.py stock all 365        → 全部股票, 指定天數
        #      python backfill.py stock 2330,2454 [天數] → 指定股票
        ids = None
        days = 730
        if args:
            first = args.pop(0)
            if first != "all":
                ids = [s.strip() for s in first.split(",") if s.strip()]
            if args:
                days = int(args[0])
        backfill_stocks(ids, days)
        raise SystemExit
    days = int(args[0]) if args else 730
    if mode in ("all", "ohlc"):
        backfill_ohlc(days)
    if mode != "ohlc":
        backfill(days, do_index=mode in ("all", "index"),
                 do_foreign=mode in ("all", "foreign"),
                 do_margin=mode in ("all", "margin"))
