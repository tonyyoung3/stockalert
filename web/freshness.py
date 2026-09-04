"""Dashboard data-freshness helpers.

Taiwan trading calendar is weekdays only (no holiday list). After 16:00
Taiwan time on a weekday, that day is the expected last trade date — same
cutoff as market.update_market_data.include_today.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))
INCLUDE_TODAY_AFTER_HOUR = 16
CALENDAR = "weekdays_only"
CALENDAR_NOTE = "週末／未內建國定假日；平日 16:00 台灣時間後才把當日視為應有資料"
HEALTH_NOTE = (
    "HTTP 200 表示行程活著。資料過期或空白見 freshness.stale／empty，"
    "不會因此回 503（Cloud Run 探活可之後再依 payload 延伸）。"
)

_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")

KEY_TABLES = ("foreign_daily", "stock_daily", "taifex", "alerts")


def taiwan_now(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(TW)


def previous_tw_trading_day(today: date) -> date:
    """Last weekday strictly before `today` (週末 skipped; no holidays)."""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def expected_tw_trade_date(now: datetime | None = None) -> date:
    """Latest Taiwan weekday we expect market rows for."""
    tw = taiwan_now(now)
    today = tw.date()
    if today.weekday() < 5 and tw.hour >= INCLUDE_TODAY_AFTER_HOUR:
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
