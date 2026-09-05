"""個股日K pattern-replay — 持有 N 日／可選停損停利。

資料只走 ``stock_daily`` 日 OHLCV，**不**复用 TAIEX 小時路徑，也沒有個股
日內／小時回測。進場對齊 ``signals.patterns`` 的 live ``check_*``（環境門檻
不變）：每個訊號日只用「截至當日」的 trailing window，不偷看未來。

出場語意（日K 高低觸價）:
- 進場：訊號日收盤（pattern 收盤才確定）。
- 持有 N 個交易日：第 N 根後續日K收盤出場。
- 可選停損／停利：做多時當日**低點** ≤ 停損價、**高點** ≥ 停利價即觸價。
- 同一日停損與停利都可能觸及時，保守假設先停損（與指數波段引擎相同）。
- 訊號落在資料尾端、出場前沒有足夠日K → 未解決，不計入統計。
"""
from __future__ import annotations

import re
import sqlite3

import numpy as np
import pandas as pd

from signals.patterns import (
    MIN_PATTERN_BARS,
    PATTERN_LABELS,
    REPLAY_PATTERNS,
    pattern_on_trailing_window,
)
from web.backtest_engine import _excursions, _swing_extra_stats, compute_stats

_STOCK_ID = re.compile(r"^[0-9A-Za-z]{2,10}$")

ASSUMPTIONS = (
    "進場：訊號日收盤（pattern 收盤才確定）。"
    "出場：持有 N 個交易日收盤；可選停損／停利以日K高低觸價"
    "（做多：當日低點≤停損價、高點≥停利價）。"
    "同一日兩者都觸及時，保守假設先停損。"
    "僅 stock_daily 日K，無個股小時／日內路徑。"
)

HOLD_DAYS_MIN = 1
HOLD_DAYS_MAX = 120
HOLD_DAYS_DEFAULT = 5
COST_PCT_DEFAULT = 0.03
_PCT_MAX = 50.0


def _opt_pct(raw, field: str):
    """Optional percent. None / blank / 0 → disabled. Positive → enabled."""
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 必須是數字")
    if value <= 0:
        return None
    if value > _PCT_MAX:
        raise ValueError(f"{field} 不可超過 {_PCT_MAX:g}%")
    return value


def parse_stock_backtest_request(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("請求必須是 JSON 物件")
    stock_id = str(payload.get("stock_id") or "").strip()
    if not _STOCK_ID.fullmatch(stock_id):
        raise ValueError("請提供有效的股票代號")
    pattern = str(payload.get("pattern") or "").strip()
    if pattern not in REPLAY_PATTERNS:
        raise ValueError("只支援上影線反轉與 Inside Day")
    try:
        hold_days = int(payload.get("hold_days", HOLD_DAYS_DEFAULT) or HOLD_DAYS_DEFAULT)
    except (TypeError, ValueError):
        raise ValueError("持有天數必須是整數")
    if hold_days < HOLD_DAYS_MIN or hold_days > HOLD_DAYS_MAX:
        raise ValueError(f"持有天數須介於 {HOLD_DAYS_MIN}–{HOLD_DAYS_MAX}")
    stop_pct = _opt_pct(payload.get("stop_pct"), "停損 %")
    take_profit_pct = _opt_pct(payload.get("take_profit_pct"), "停利 %")
    try:
        cost_pct = float(payload.get("cost_pct", COST_PCT_DEFAULT) or 0)
    except (TypeError, ValueError):
        raise ValueError("成本 % 必須是數字")
    if cost_pct < 0 or cost_pct > 3:
        raise ValueError("成本 % 須介於 0–3")
    return {
        "stock_id": stock_id,
        "pattern": pattern,
        "hold_days": hold_days,
        "stop_pct": stop_pct,
        "take_profit_pct": take_profit_pct,
        "cost_pct": cost_pct,
    }


def stock_daily_rows_to_yahoo(rows) -> tuple[pd.DataFrame, str | None]:
    """Map ``stock_daily`` rows → Yahoo-style Open/High/Low/Close/Volume."""
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]), None
    dates, opens, highs, lows, closes, volumes, names = [], [], [], [], [], [], []
    for row in rows:
        trade_date, open_, high, low, close, volume = row[:6]
        name = row[6] if len(row) > 6 else None
        if trade_date is None or any(v is None or (isinstance(v, float) and np.isnan(v))
                                     for v in (open_, high, low, close)):
            continue
        dates.append(trade_date)
        opens.append(float(open_))
        highs.append(float(high))
        lows.append(float(low))
        closes.append(float(close))
        volumes.append(float(volume) if volume is not None else 0.0)
        if name:
            names.append(str(name))
    if not dates:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]), None
    df = pd.DataFrame(
        {
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": volumes,
        },
        index=pd.to_datetime(dates),
    )
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df, (names[-1] if names else None)


def load_stock_daily_yahoo(conn: sqlite3.Connection, stock_id: str) -> tuple[pd.DataFrame, str | None]:
    """Load one ticker from ``stock_daily``. No TAIEX / hourly tables."""
    try:
        rows = conn.execute(
            "SELECT trade_date, open, high, low, close, volume, stock_name "
            "FROM stock_daily WHERE stock_id=? ORDER BY trade_date",
            (stock_id,),
        ).fetchall()
    except Exception:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]), None
    df, name = stock_daily_rows_to_yahoo(rows)
    if name is None:
        try:
            got = conn.execute(
                "SELECT stock_name FROM stocks WHERE stock_id=?", (stock_id,)
            ).fetchone()
        except Exception:
            got = None
        if got and got[0]:
            name = str(got[0])
    return df, name


def run_stock_pattern_replay(
    df: pd.DataFrame,
    pattern: str,
    hold_days: int = HOLD_DAYS_DEFAULT,
    stop_pct: float | None = None,
    take_profit_pct: float | None = None,
    cost_pct: float = COST_PCT_DEFAULT,
) -> dict:
    """Replay ``pattern`` on a Yahoo-style daily OHLCV frame (no network)."""
    if pattern not in REPLAY_PATTERNS:
        return {"n": 0, "error": "只支援上影線反轉與 Inside Day"}
    label = PATTERN_LABELS[pattern]
    if df is None or df.empty or len(df) < MIN_PATTERN_BARS:
        return {
            "n": 0,
            "error": f"日K不足 {MIN_PATTERN_BARS} 根，無法計算月線與 pattern。",
        }

    n = len(df)
    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    closes = df["Close"].to_numpy(dtype=float)
    dates = pd.to_datetime(df.index)
    dows = dates.dayofweek.to_numpy()
    cost = float(cost_pct) / 100
    stop_on = stop_pct is not None
    tp_on = take_profit_pct is not None
    stop_frac = (float(stop_pct) / 100) if stop_on else 0.0
    tp_frac = (float(take_profit_pct) / 100) if tp_on else 0.0

    trades = []
    unresolved = 0
    signal_days = 0
    for i in range(n):
        if not pattern_on_trailing_window(df, i, pattern):
            continue
        signal_days += 1
        entry_price = closes[i]
        if not np.isfinite(entry_price) or entry_price == 0:
            continue
        stop_level = entry_price * (1 - stop_frac) if stop_on else None
        tp_level = entry_price * (1 + tp_frac) if tp_on else None
        last_j = i + hold_days
        ran_out = last_j >= n
        last_check = min(last_j, n - 1)
        exit_price = None
        exit_reason = None
        exit_j = None
        path_hi = -np.inf
        path_lo = np.inf

        for j in range(i + 1, last_check + 1):
            hi, lo = highs[j], lows[j]
            if not np.isfinite(hi) or not np.isfinite(lo):
                continue
            stop_hit = stop_on and lo <= stop_level
            tp_hit = tp_on and hi >= tp_level
            if stop_hit:
                # Same-day stop+TP: stop wins (avoid overstating the trade).
                exit_price = stop_level
                exit_reason = "stop"
                exit_j = j
                path_hi = max(path_hi, exit_price)
                path_lo = min(path_lo, exit_price)
                break
            if tp_hit:
                exit_price = tp_level
                exit_reason = "take_profit"
                exit_j = j
                path_hi = max(path_hi, exit_price)
                path_lo = min(path_lo, exit_price)
                break
            path_hi = max(path_hi, hi)
            path_lo = min(path_lo, lo)
            if j == last_j:
                exit_price = closes[j]
                exit_reason = "max_hold"
                exit_j = j

        if exit_price is None:
            if ran_out:
                unresolved += 1
            continue

        raw_ret = exit_price / entry_price - 1
        exit_when = "收盤" if exit_reason == "max_hold" else "盤中觸價"
        mfe, mae = _excursions("long", entry_price, path_hi, path_lo)
        trades.append(
            {
                "trade_date": dates[i],
                "dow": int(dows[i]),
                "entry_time": dates[i].strftime("%Y-%m-%d") + " 收盤",
                "exit_time": dates[exit_j].strftime("%Y-%m-%d") + " " + exit_when,
                "entry_price": float(entry_price),
                "exit_price": float(exit_price),
                "exit_date": dates[exit_j],
                "hold_days": int(exit_j - i),
                "stopped": exit_reason == "stop",
                "exit_reason": exit_reason,
                "mfe": mfe,
                "mae": mae,
                "ret_gross": raw_ret,
                "ret_net": raw_ret - cost,
            }
        )

    out = pd.DataFrame(trades)
    out.attrs["unresolved"] = unresolved
    out.attrs["cost_pct"] = cost_pct
    if out.empty:
        if unresolved:
            return {
                "n": 0,
                "unresolved_trades": unresolved,
                "days_passed_filter": signal_days,
                "total_days_in_dataset": n,
                "error": f"有 {unresolved} 個訊號出現在資料尾端，出場前日K就用完了（結果未知）。",
            }
        return {
            "n": 0,
            "no_trigger": True,
            "days_passed_filter": 0,
            "total_days_in_dataset": n,
            "error": f"這段日K沒有觸發「{label}」。",
        }

    result = compute_stats(out)
    if result.get("n"):
        result.update(_swing_extra_stats(out))
        result["avg_return_pct"] = result.get("ev_pct")
        result["price_series"] = _price_series(df, out)
    result["unresolved_trades"] = unresolved
    result["days_passed_filter"] = signal_days
    result["total_days_in_dataset"] = n
    return result


def _price_series(df: pd.DataFrame, trades: pd.DataFrame) -> list:
    if trades.empty:
        return []
    lo = pd.Timestamp(trades["trade_date"].min())
    hi = pd.Timestamp(pd.to_datetime(trades["exit_date"]).max())
    idx = pd.to_datetime(df.index)
    mask = (idx >= lo) & (idx <= hi + pd.Timedelta(days=5))
    sub = df.loc[mask, "Close"].dropna()
    return [
        {"date": pd.Timestamp(d).strftime("%Y-%m-%d"), "close": round(float(c), 2)}
        for d, c in zip(sub.index, sub.values)
    ]


def _meta(payload: dict, stock_name: str | None) -> dict:
    pattern = payload["pattern"]
    return {
        "universe": "stock",
        "dataset": "stock_daily",
        "mode": "pattern_hold",
        "direction": "long",
        "stock_id": payload["stock_id"],
        "stock_name": stock_name or payload["stock_id"],
        "pattern": pattern,
        "pattern_label": PATTERN_LABELS[pattern],
        "hold_days": payload["hold_days"],
        "stop_pct": payload["stop_pct"],
        "take_profit_pct": payload["take_profit_pct"],
        "cost_pct": payload["cost_pct"],
        "assumptions": ASSUMPTIONS,
        "stale_open_warning": False,
        "has_intraday_path": False,
    }


def run_stock_backtest(conn: sqlite3.Connection, payload: dict) -> dict:
    """Validate request, load ``stock_daily``, replay. Never hits the network."""
    try:
        req = parse_stock_backtest_request(payload)
    except ValueError as exc:
        return {"error": str(exc), "universe": "stock"}
    df, name = load_stock_daily_yahoo(conn, req["stock_id"])
    if df.empty:
        return {
            "error": "查無此股的日K（stock_daily）。請確認代號，或先更新市場資料。",
            **_meta(req, name),
        }
    result = run_stock_pattern_replay(
        df,
        req["pattern"],
        hold_days=req["hold_days"],
        stop_pct=req["stop_pct"],
        take_profit_pct=req["take_profit_pct"],
        cost_pct=req["cost_pct"],
    )
    result.update(_meta(req, name))
    return result
