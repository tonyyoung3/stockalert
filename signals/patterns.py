"""Upper-shadow reversal / Inside Day classifiers.

Thresholds stay env-overridable so Slack and the harness keep the same live
rule. Do not auto-cut a new rule_version from here.
"""

from __future__ import annotations

import os
from datetime import date

import pandas as pd

shadow_ratio = float(os.environ.get("SHADOW_RATIO", "1.5"))  # 上影線至少是實體的 N 倍
upper_shadow_min_pct = float(os.environ.get("UPPER_SHADOW_MIN_PCT", "0.02"))  # 前一天上影線至少佔收盤價的 N%
min_daily_gain = float(os.environ.get("MIN_DAILY_GAIN", "0.01"))  # 當天漲幅至少 N%


def _check_reversal_pattern(df, day1_idx, day2_idx):
    """檢查指定兩天的上影線反轉模式 (輔助函數)"""
    open1, close1, high1 = df['Open'].iloc[day1_idx], df['Close'].iloc[day1_idx], df['High'].iloc[day1_idx]
    body1 = abs(close1 - open1)
    upper_shadow1 = high1 - max(open1, close1)

    close2 = df['Close'].iloc[day2_idx]

    # 上影線長度需同時滿足：
    # 1) 長於實體的 shadow_ratio 倍
    # 2) 佔前一日收盤價至少 upper_shadow_min_pct
    upper_shadow_pct = (upper_shadow1 / close1) if close1 != 0 else 0
    first_day_shadow = (body1 > 0 and upper_shadow1 / body1 > shadow_ratio) and (upper_shadow_pct >= upper_shadow_min_pct)

    # 第二天收復上影線
    second_day_recover = close2 >= high1

    # 第二天漲幅要 min_daily_gain 以上，且成交量高於均量
    daily_gain     = (close2 - df['Close'].iloc[day1_idx]) / df['Close'].iloc[day1_idx] >= min_daily_gain
    vol2           = df['Volume'].iloc[day2_idx]
    volume_confirm = vol2 > df['Volume'].mean()

    return first_day_shadow and second_day_recover and daily_gain and volume_confirm


def check_upper_shadow_reversal(df):
    if len(df) < 22:  # 需要至少22天資料來計算月線和檢查模式
        return False

    # 檢查昨天和前天
    pattern_match = _check_reversal_pattern(df, -2, -1)

    # 今天收盤價在月線上（20日均線）
    ma20 = df['Close'].rolling(window=20).mean().iloc[-1]
    above_ma20 = df['Close'].iloc[-1] > ma20

    return pattern_match and above_ma20


def check_inside_day(df):
    if len(df) < 22:  # 需要足夠資料來檢查模式和計算月線
        return False

    # 前天符合上影線反轉條件 (檢查 D-3 和 D-2)
    day_before_match = _check_reversal_pattern(df, -3, -2)

    # 昨天符合上影線反轉條件 (檢查 D-2 和 D-1)
    yesterday_match = _check_reversal_pattern(df, -2, -1)

    # 今天收盤價是三天最高
    today_close = df['Close'].iloc[-1]
    three_day_high = df['Close'].iloc[-3:].max()
    is_three_day_high = today_close == three_day_high

    # 今天收盤價在月線上（20日均線）
    ma20 = df['Close'].rolling(window=20).mean().iloc[-1]
    above_ma20 = today_close > ma20

    return day_before_match and yesterday_match and is_three_day_high and above_ma20


def last_bar_date(df) -> date:
    """Calendar date of the last candle, in Taiwan time if the index is tz-aware."""
    ts = pd.Timestamp(df.index[-1])
    if ts.tz is not None:
        ts = ts.tz_convert("Asia/Taipei")
    return ts.date()


def classify_pattern(df) -> str | None:
    """Return the matched pattern name.

    Inside Day is a stricter form of upper-shadow reversal (it also requires
    the prior pair to match), so it must be checked first. The old
    `if upper_shadow / elif inside_day` order made Inside Day unreachable.
    """
    if check_inside_day(df):
        return "inside_day"
    if check_upper_shadow_reversal(df):
        return "upper_shadow_reversal"
    return None
