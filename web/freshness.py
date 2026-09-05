"""Dashboard data-freshness helpers.

Taiwan trading days are weekdays that are not TWSE holidays
(web.tw_calendar). After 16:00 Taiwan time on a trading day, that day is
the expected last trade date — same cutoff as
market.update_market_data.include_today.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from web.tw_calendar import is_tw_trading_day, taiwan_now as _taiwan_now

TW = timezone(timedelta(hours=8))
INCLUDE_TODAY_AFTER_HOUR = 16
CALENDAR = "tw_trading_days"
CALENDAR_NOTE = (
    "週末與國定假日不計過期；靜態表 2025–2026（證交所市場開休市日期），"
    "2027 尚未公告先當平日；平日 16:00 台灣時間後才把當日視為應有資料"
)
HEALTH_NOTE = (
    "HTTP 200 表示行程活著。資料過期或空白見 freshness.stale／empty，"
    "不會因此回 503（Cloud Run 探活可之後再依 payload 延伸）。"
)

_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")

KEY_TABLES = ("foreign_daily", "stock_daily", "taifex", "alerts")


def taiwan_now(now: datetime | None = None) -> datetime:
    """Asia/Taipei now. Patch this in tests; taiwan_today follows it."""
    return _taiwan_now(now)


def taiwan_today(now: datetime | None = None) -> date:
    """Asia/Taipei calendar date. Never use date.today() for TW business days."""
    return taiwan_now(now).date()


def previous_tw_trading_day(today: date) -> date:
    """Last TWSE trading day strictly before `today` (weekends and holidays)."""
    d = today - timedelta(days=1)
    for _ in range(31):
        if is_tw_trading_day(d):
            return d
        d -= timedelta(days=1)
    raise ValueError(f"no TW trading day in 31 days before {today.isoformat()}")


def expected_tw_trade_date(
    now: datetime | None = None,
    after_hour: int = INCLUDE_TODAY_AFTER_HOUR,
) -> date:
    """Latest Taiwan trading day we expect rows for.

    `after_hour` is local Taiwan time. T86 / stock_daily use 16:00
    (INCLUDE_TODAY_AFTER_HOUR). FinMind 分點 SecIdAgg is documented ~21:00
    and is served from GET /api/broker_branch/freshness — not /api/freshness.
    """
    tw = taiwan_now(now)
    today = tw.date()
    if is_tw_trading_day(today) and tw.hour >= after_hour:
        return today
    return previous_tw_trading_day(today)


def parse_ymd(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()[:10]
    if not _YMD.fullmatch(s):
        return None
    y, m, d = (int(p) for p in s.split("-"))
    return date(y, m, d)


def days_ago(last: date | None, today: date) -> int | None:
    if last is None:
        return None
    return (today - last).days


def table_status(table: str, last_date, today: date, expected: date) -> dict:
    last = parse_ymd(last_date)
    empty = last is None
    stale = empty or last < expected
    return {
        "table": table,
        "last_date": last.isoformat() if last else None,
        "days_ago": days_ago(last, today),
        "stale": stale,
        "empty": empty,
    }
