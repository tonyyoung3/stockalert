"""Taiwan cash-market trading calendar.

Weekends plus a maintained static table of TWSE closed weekdays.
Does not scrape announcements. Typhoon / unscheduled closures are not included.

Source
------
TWSE「市場開休市日期」
https://www.twse.com.tw/zh/trading/holiday.html
https://www.twse.com.tw/holidaySchedule/holidaySchedule?response=json
https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule

Year coverage
-------------
* 2026: copied from the live TWSE holidaySchedule JSON (民國 115 年).
* 2025: TWSE 民國 114 年表, including the mid-year revision that added
  教師節 / 光復節 / 行憲紀念日 (same dates as the TAIFEX 114 年修正版).
* 2027+: not published as of 2026-09; those years fall back to weekdays only
  until this table is updated.

Settlement-only days (市場無交易，僅辦理結算交割) are closed: no trade data.
「開始交易日」/「最後交易日」are trading days and are not listed here.
"""
from __future__ import annotations

from datetime import date

HOLIDAY_YEARS = (2025, 2026)
SOURCE_URL = "https://www.twse.com.tw/zh/trading/holiday.html"
SOURCE_NOTE = (
    "TWSE 市場開休市日期（靜態表 2025–2026；"
    "2026 取自 holidaySchedule JSON，2025 為 114 年修正版。"
    "2027 尚未公告，先當平日）"
)

# Weekday cash-market closures only. Saturdays/Sundays are omitted;
# is_tw_trading_day already rejects weekday() >= 5.
_CLOSED_WEEKDAYS: dict[date, str] = {
    # --- 2025 (民國 114 年, 修正版) ---
    date(2025, 1, 1): "中華民國開國紀念日",
    date(2025, 1, 23): "春節前結算交割（無交易）",
    date(2025, 1, 24): "春節前結算交割（無交易）",
    date(2025, 1, 27): "農曆除夕前一日（調整放假）",
    date(2025, 1, 28): "農曆除夕",
    date(2025, 1, 29): "農曆春節",
    date(2025, 1, 30): "農曆春節",
    date(2025, 1, 31): "農曆春節",
    date(2025, 2, 28): "和平紀念日",
    date(2025, 4, 3): "兒童節及民族掃墓節（補假）",
    date(2025, 4, 4): "兒童節及民族掃墓節",
    date(2025, 5, 1): "勞動節",
    date(2025, 5, 30): "端午節（補假）",
    date(2025, 9, 29): "孔子誕辰紀念日／教師節（補假）",
    date(2025, 10, 6): "中秋節",
    date(2025, 10, 10): "國慶日",
    date(2025, 10, 24): "臺灣光復紀念日（補假）",
    date(2025, 12, 25): "行憲紀念日",
    # --- 2026 (民國 115 年, TWSE holidaySchedule JSON) ---
    date(2026, 1, 1): "中華民國開國紀念日",
    date(2026, 2, 12): "春節前結算交割（無交易）",
    date(2026, 2, 13): "春節前結算交割（無交易）",
    date(2026, 2, 16): "農曆除夕及春節",
    date(2026, 2, 17): "農曆除夕及春節",
    date(2026, 2, 18): "農曆除夕及春節",
    date(2026, 2, 19): "農曆除夕及春節",
    date(2026, 2, 20): "農曆除夕及春節",
    date(2026, 2, 27): "和平紀念日（補假）",
    date(2026, 4, 3): "兒童節（補假）",
    date(2026, 4, 6): "民族掃墓節（補假）",
    date(2026, 5, 1): "勞動節",
    date(2026, 6, 19): "端午節",
    date(2026, 9, 25): "中秋節",
    date(2026, 9, 28): "孔子誕辰紀念日／教師節",
    date(2026, 10, 9): "國慶日（補假）",
    date(2026, 10, 26): "臺灣光復紀念日（補假）",
    date(2026, 12, 25): "行憲紀念日",
}

TWSE_CLOSED_WEEKDAYS = frozenset(_CLOSED_WEEKDAYS)


def is_tw_holiday(d: date) -> bool:
    """True if `d` is a listed TWSE weekday closure (not merely a weekend)."""
    return d in TWSE_CLOSED_WEEKDAYS


def holiday_name(d: date) -> str | None:
    return _CLOSED_WEEKDAYS.get(d)


def is_tw_trading_day(d: date) -> bool:
    """Weekday and not a TWSE cash-market holiday."""
    return d.weekday() < 5 and d not in TWSE_CLOSED_WEEKDAYS
