#!/usr/bin/env python3
"""通用回測引擎 —— 供 dashboard.py 的「策略回測」頁面呼叫。

設計理念(呼應這個專案一路驗證下來的教訓):
- 2年小時級資料集才支援「日內事件觸發」(區間突破、回檔進場、停損)——因為
  只有這個資料集有小時級的價格路徑可以模擬進出場順序。
- 15年日K資料集**不支援**日內事件觸發,只支援「前收盤價 進場、隔日收盤/開盤
  出場」這種整天賭注(對應日曆效應、趨勢濾網類策略)——因為官方日K的
  「開盤價」99.7%時間等於前一天收盤價(陳舊開盤價陷阱,見
  strategy_summary.md),用它模擬日內事件觸發是幻覺,所以乾脆不開放。
- 所有「篩選條件」一律用「前一交易日收盤時已知」的資訊(prev_close、
  prev_ma20/60、prev_ret),避免用到決策當下還不知道的未來資訊(look-ahead)。
  當日的跳空(gap)例外——因為不管是日內模式(開盤後才檢查)或隔夜模式
  (收盤時才進場),當天的跳空在決策當下都已經發生、已知,不算未來資訊。
- 每次回測都自動附上:t檢定、前後半穩定性、區塊拔靴95%CI、成本敏感度。
"""
from __future__ import annotations
import sqlite3
import numpy as np
import pandas as pd
from scipy import stats


# ==================================================================
# 特徵工程
# ==================================================================

def _merge_oi_ratio(df: pd.DataFrame, conn: sqlite3.Connection, window: int = 60) -> pd.DataFrame:
    """合併「外資/投信 台指期未平倉淨額比」與它在過去 window 天的百分位排名(0~100)。
    只取用「前一交易日」的值(prev_oi_ratio / prev_oi_ratio_pctile)——因為 TAIFEX
    未平倉資料是收盤後才公布的,今天的值今天決策時其實還看不到,一律比照
    prev_close/prev_ma 的作法位移一天,避免未來資訊。TAIFEX 只有約3年資料,
    更早的日期這幾欄會是 NaN(篩選時自然被排除,不需要另外特判)。
    """
    oi = pd.read_sql("SELECT trade_date, investor, oi_net_lots FROM taifex_fut_oi "
                      "WHERE product='臺股期貨'", conn)
    if oi.empty:
        df["oi_ratio"] = np.nan
        df["prev_oi_ratio"] = np.nan
        df["prev_oi_ratio_pctile"] = np.nan
        return df
    oi["trade_date"] = pd.to_datetime(oi["trade_date"])
    piv = oi.pivot_table(index="trade_date", columns="investor", values="oi_net_lots", aggfunc="last")
    foreign = piv.get("外資及陸資"); trust = piv.get("投信")
    if foreign is None or trust is None:
        df["oi_ratio"] = np.nan
        df["prev_oi_ratio"] = np.nan
        df["prev_oi_ratio_pctile"] = np.nan
        return df
    ratio = (foreign / trust.replace(0, np.nan)).rename("oi_ratio").reset_index()
    df = df.merge(ratio, on="trade_date", how="left")
    df["prev_oi_ratio"] = df["oi_ratio"].shift(1)   # 篩選條件一律用這欄(收盤已知),不用 oi_ratio 本身
    return df


def _oi_ratio_pctile(prev_ratio: pd.Series, window: int) -> pd.Series:
    """prev_oi_ratio 在過去 window 天內的百分位排名(0~100),視窗大小由呼叫端(篩選條件)決定。"""
    minp = max(10, window // 3)
    return prev_ratio.rolling(window, min_periods=minp).apply(
        lambda s: (s <= s.iloc[-1]).mean() * 100, raw=False)


def build_hourly_features(conn: sqlite3.Connection) -> pd.DataFrame:
    """2年小時級資料 -> 日層級特徵表(含小時OHLC路徑,供日內事件模擬用)。"""
    h = pd.read_sql("SELECT trade_date, ts, open, high, low, close "
                     "FROM taiex_hourly_ohlc ORDER BY trade_date, ts", conn)
    h["trade_date"] = pd.to_datetime(h["trade_date"])
    h["hour"] = h["ts"].str[11:13].astype(int)

    rows = []
    for d, g in h.groupby("trade_date"):
        g = g.sort_values("hour")
        if len(g) < 5 or set(g["hour"]) != {9, 10, 11, 12, 13}:
            continue
        by_hr = g.set_index("hour")
        row = {"trade_date": d}
        for hr in (9, 10, 11, 12, 13):
            r = by_hr.loc[hr]
            row[f"h{hr}_open"] = r["open"]; row[f"h{hr}_high"] = r["high"]
            row[f"h{hr}_low"] = r["low"];   row[f"h{hr}_close"] = r["close"]
        row["day_open"] = row["h9_open"]
        row["day_close"] = row["h13_close"]
        row["day_high"] = g["high"].max()
        row["day_low"] = g["low"].min()
        row["first_hour_high"] = row["h9_high"]
        row["first_hour_low"] = row["h9_low"]
        row["last_bar_ret"] = (row["h13_close"]/row["h13_open"]-1) if row["h13_open"] else np.nan
        rng = row["h13_high"] - row["h13_low"]
        row["last_bar_pos"] = ((row["h13_close"]-row["h13_low"])/rng) if rng else np.nan
        rows.append(row)
    df = pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)

    daily_close = pd.read_sql("SELECT trade_date, close FROM taiex_daily ORDER BY trade_date", conn)
    daily_close["trade_date"] = pd.to_datetime(daily_close["trade_date"])
    daily_close["ma20"] = daily_close["close"].rolling(20).mean()
    daily_close["ma60"] = daily_close["close"].rolling(60).mean()
    df = df.merge(daily_close[["trade_date", "ma20", "ma60"]], on="trade_date", how="left")

    df["prev_close"] = df["day_close"].shift(1)
    df["prev_ma20"] = df["ma20"].shift(1)
    df["prev_ma60"] = df["ma60"].shift(1)
    df["prev_ret"] = df["day_close"].pct_change().shift(1)   # 前一交易日「自己」的漲跌幅(已知)
    df["prev_date"] = df["trade_date"].shift(1)
    df["gap_days"] = (df["trade_date"] - df["prev_date"]).dt.days
    df["gap"] = df["day_open"] / df["prev_close"] - 1        # 當天自己的跳空(決策時已發生)
    df["day_ret"] = df["day_close"] / df["prev_close"] - 1   # 當天自己的漲跌幅(收盤時已知,可用於「收盤進場」規則)
    df["dow"] = df["trade_date"].dt.dayofweek
    df["stale_open_risk"] = False
    df["has_intraday_path"] = True
    df = _merge_oi_ratio(df, conn)
    return df


def build_daily_features(conn: sqlite3.Connection) -> pd.DataFrame:
    """15年日K -> 特徵表。不含小時級路徑,不支援日內事件觸發。"""
    df = pd.read_sql("SELECT trade_date, open, high, low, close FROM taiex_daily ORDER BY trade_date", conn)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["day_open"] = df["open"]; df["day_high"] = df["high"]
    df["day_low"] = df["low"];   df["day_close"] = df["close"]
    df["first_hour_high"] = np.nan; df["first_hour_low"] = np.nan  # 無小時級資料

    df["prev_close"] = df["close"].shift(1)
    df["prev_ma20"] = df["ma20"].shift(1)
    df["prev_ma60"] = df["ma60"].shift(1)
    df["prev_ret"] = df["close"].pct_change().shift(1)
    df["prev_date"] = df["trade_date"].shift(1)
    df["gap_days"] = (df["trade_date"] - df["prev_date"]).dt.days
    df["gap"] = df["open"] / df["prev_close"] - 1   # 陳舊開盤價,只能參考,不可交易(見下方警告)
    df["day_ret"] = df["close"] / df["prev_close"] - 1   # 當天自己的漲跌幅(收盤時已知,不受開盤價陳舊問題影響)
    df["dow"] = df["trade_date"].dt.dayofweek
    df["month"] = df["trade_date"].dt.month
    df["stale_open_risk"] = True
    df["has_intraday_path"] = False
    df = _merge_oi_ratio(df, conn)
    return df


DATASETS = {"2y_hourly": build_hourly_features, "15y_daily": build_daily_features}


# ==================================================================
# 篩選條件
# ==================================================================

def apply_filters(df: pd.DataFrame, filters: dict) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    wd = filters.get("weekdays")
    if wd:
        mask &= df["dow"].isin(wd)

    trend = filters.get("trend", "none")
    if trend == "above_ma20":
        mask &= df["prev_close"] > df["prev_ma20"]
    elif trend == "below_ma20":
        mask &= df["prev_close"] < df["prev_ma20"]
    elif trend == "above_ma60":
        mask &= df["prev_close"] > df["prev_ma60"]
    elif trend == "below_ma60":
        mask &= df["prev_close"] < df["prev_ma60"]
    elif trend == "above_ma20_today":
        # 用「今天收盤」自己跟今天的MA比較,適用於收盤才決策的規則(例如波段進場)
        mask &= df["day_close"] > df["ma20"]
    elif trend == "below_ma20_today":
        mask &= df["day_close"] < df["ma20"]
    elif trend == "above_ma60_today":
        mask &= df["day_close"] > df["ma60"]
    elif trend == "below_ma60_today":
        mask &= df["day_close"] < df["ma60"]

    pd_filter = filters.get("prev_day", "none")
    if pd_filter == "up":
        mask &= df["prev_ret"] > 0
    elif pd_filter == "down":
        mask &= df["prev_ret"] < 0

    gmin = float(filters.get("gap_abs_min_pct", 0) or 0) / 100
    if gmin > 0:
        mask &= df["gap"].abs() >= gmin
    gdir = filters.get("gap_dir", "any")
    if gdir == "up":
        mask &= df["gap"] > 0
    elif gdir == "down":
        mask &= df["gap"] < 0

    # 當日漲跌幅(收盤時已知,例如「當日漲幅2%」這種收盤才決策的濾網)
    drmin = float(filters.get("day_ret_min_pct", 0) or 0) / 100
    if drmin > 0:
        mask &= df["day_ret"].abs() >= drmin
    drdir = filters.get("day_ret_dir", "any")
    if drdir == "up":
        mask &= df["day_ret"] > 0
    elif drdir == "down":
        mask &= df["day_ret"] < 0

    # 外資/投信台指期未平倉比:用「前一交易日」的值(收盤後才公布,今天決策時看不到今天的)
    # 在過去 N 天內的百分位排名 —— 例如「低於過去60天的25%分位」代表比值相對過去更負
    oi_mode = filters.get("oi_ratio_mode", "none")   # none | below_pctile | above_pctile
    if oi_mode in ("below_pctile", "above_pctile") and "prev_oi_ratio" in df.columns:
        oi_window = int(filters.get("oi_ratio_window", 60) or 60)
        oi_thresh = float(filters.get("oi_ratio_pctile", 25) or 25)
        pctile = _oi_ratio_pctile(df["prev_oi_ratio"], oi_window)
        mask &= (pctile <= oi_thresh) if oi_mode == "below_pctile" else (pctile >= oi_thresh)

    # N日新高/新低突破(唐奇安通道式):用不含今天的前 N 天高/低點當基準,今天收盤
    # 突破/跌破才算數。這是「收盤才確定」的濾網,只能用在隔夜/波段模式(見下方 guard)。
    brk = filters.get("breakout", "none")   # none | n_day_high | n_day_low
    if brk in ("n_day_high", "n_day_low"):
        brk_window = int(filters.get("breakout_window", 20) or 20)
        if brk == "n_day_high":
            roll_high = df["day_high"].rolling(brk_window).max().shift(1)
            mask &= df["day_close"] > roll_high
        else:
            roll_low = df["day_low"].rolling(brk_window).min().shift(1)
            mask &= df["day_close"] < roll_low

    # 均線黃金/死亡交叉(MA20 vs MA60,以「今天」交叉當天為準):同樣是收盤才確定的濾網。
    ma_cross = filters.get("ma_cross", "none")   # none | golden | death
    if ma_cross == "golden":
        mask &= (df["prev_ma20"] <= df["prev_ma60"]) & (df["ma20"] > df["ma60"])
    elif ma_cross == "death":
        mask &= (df["prev_ma20"] >= df["prev_ma60"]) & (df["ma20"] < df["ma60"])

    # 只有真的用到 prev_close/gap/趨勢(前收版)相關篩選時才需要它非空;沒用到就不該
    # 平白排除資料集第一天(它本來就沒有 prev_close,但不代表不能拿來做純日內規則)
    # 「今日」版趨勢濾網跟 day_ret 濾網用的是今天自己的欄位,不需要 prev_close
    trend_needs_prev = trend not in ("none", "above_ma20_today", "below_ma20_today",
                                      "above_ma60_today", "below_ma60_today")
    needs_prev = trend_needs_prev or (pd_filter != "none") or gdir != "any" or gmin > 0 \
        or drdir != "any" or drmin > 0 or ma_cross != "none"
    if needs_prev:
        mask &= df["prev_close"].notna()
    return mask


def _uses_close_decided_filters(filters: dict) -> bool:
    """這些濾網要等『今天收盤』才能確定,只適合收盤才進場的模式(隔夜/波段)。
    如果被套用在日內模式(進場時機通常早於收盤),等於用了決策當下還不存在的
    未來資訊 —— 跟這個引擎一路堅持的 look-ahead 防線矛盾,所以直接擋掉並說明原因。
    """
    if filters.get("day_ret_dir", "any") != "any" or float(filters.get("day_ret_min_pct", 0) or 0) > 0:
        return True
    if str(filters.get("trend", "none")).endswith("_today"):
        return True
    if filters.get("breakout", "none") != "none":
        return True
    if filters.get("ma_cross", "none") != "none":
        return True
    return False


# ==================================================================
# 規則執行:日內事件模擬(需要 has_intraday_path)
# ==================================================================

_REF_COLS = {
    "day_open": "day_open",
    "first_hour_high": "first_hour_high",
    "first_hour_low": "first_hour_low",
    "prev_close": "prev_close",
}


_BAR_END = {9: "10:00", 10: "11:00", 11: "12:00", 12: "13:00", 13: "13:30"}


def _hour_label(trade_date, hr):
    """把「第 hr 根小時K」轉成可讀的時間標示。

    注意精度上限:資料只到小時K,所以「觸價進場」只知道發生在那一根K棒之內,
    無法知道確切分秒 —— 標成區間(例如 10:00-11:00)而不是假裝知道精確時點。
    最後一根(13:00)實際只到 13:30 收盤,所以區間結尾是 13:30 而非 14:00。
    """
    if hr is None or pd.isna(trade_date):
        return None
    d = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    return f"{d} {hr:02d}:00-{_BAR_END.get(int(hr), '')}"


def _excursions(direction, entry_price, path_hi, path_lo):
    """MFE(最大有利偏移)/ MAE(最大不利偏移),以進場價為基準的百分比。

    MFE 用途:看策略「最好的時候賺多少」,若 MFE 遠大於實際獲利,代表出場太晚/沒停利。
    MAE 用途:看策略「最慘被套多深」,是決定停損該放多寬最直接的依據——
    停損若設得比多數獲利單的 MAE 還窄,等於會把本來會賺的單洗掉。
    """
    if entry_price in (None, 0) or pd.isna(entry_price):
        return None, None
    if not np.isfinite(path_hi) or not np.isfinite(path_lo):
        return None, None
    up = path_hi / entry_price - 1
    dn = path_lo / entry_price - 1
    if direction == "long":
        return float(max(up, 0.0)), float(min(dn, 0.0))
    return float(max(-dn, 0.0)), float(min(-up, 0.0))


def _level(row, ref, offset_pct):
    base = row[_REF_COLS[ref]]
    if pd.isna(base):
        return np.nan
    return base * (1 + offset_pct / 100)


def run_intraday(df: pd.DataFrame, mask: pd.Series, rule: dict) -> pd.DataFrame:
    """逐小時模擬進場/停損/出場,回傳每筆交易的明細(含 ret_gross, ret_net)。"""
    entry = rule["entry"]
    stop = rule.get("stop", {})
    exit_hour = int(rule.get("exit_hour", 13))
    cost = float(rule.get("cost_pct", 0.03)) / 100
    earliest_hour = int(entry.get("earliest_hour", 10))
    direction = entry["direction"]           # long | short
    # touch_from_above: 這根K的高點觸及/超過 level(價格向上碰到它,對應「向上突破」)
    # touch_from_below: 這根K的低點觸及/跌破 level(價格向下碰到它,對應「向下跌破」)
    # 命名是以「K棒觸碰 level 的方向」為準,不是「price相對level原本在哪一側」,
    # 之前 dashboard.py 下拉選單的文字標籤標反了(已於本次修正,程式邏輯本身沒問題,
    # 之前的驗證/strategy_summary.md 數字用的都是這裡的邏輯,是對的)。
    trigger = entry["trigger"]               # touch_from_above | touch_from_below
    ref = entry["reference"]; offset = float(entry.get("offset_pct", 0))
    stop_on = bool(stop.get("enabled", False))
    stop_ref = stop.get("reference", "day_open"); stop_offset = float(stop.get("offset_pct", 0))

    hours = [h for h in (9, 10, 11, 12, 13) if h >= earliest_hour and h <= exit_hour]

    trades = []
    for _, row in df[mask].iterrows():
        level = _level(row, ref, offset)
        if pd.isna(level):
            continue
        stop_level = None
        if stop_on:
            if stop_ref == "entry_price":
                stop_level = level
            else:
                stop_level = _level(row, stop_ref, stop_offset)
            if stop_level is None or pd.isna(stop_level):
                continue
            # 停損必須設在進場價「不利」的那一側,否則是無意義的設定(例如做多
            # 但停損價設在進場價之上)—— 這種天直接跳過,不構成合法交易設定
            if direction == "long" and stop_level >= level:
                continue
            if direction == "short" and stop_level <= level:
                continue

        entered = False; entry_price = None; stopped = False; exit_price = None
        entry_hr = None; exit_hr = None
        path_hi = -np.inf; path_lo = np.inf   # 持倉期間走過的最高/最低,供 MAE/MFE 用
        for hr in hours:
            hi, lo = row[f"h{hr}_high"], row[f"h{hr}_low"]
            if pd.isna(hi) or pd.isna(lo):
                continue
            if not entered:
                touched = (hi >= level) if trigger == "touch_from_above" else (lo <= level)
                if touched:
                    entered = True; entry_price = level; entry_hr = hr
                    # 進場當根K只知道整根的高低,不知道進場「之後」才走到哪,
                    # 這裡把整根納入計算 —— MAE/MFE 會略為高估,屬保守方向
                    path_hi = max(path_hi, hi); path_lo = min(path_lo, lo)
                    # 同一根K內若停損也被觸及,保守假設當根K就停損(避免順序不明造成高估)
                    if stop_on and stop_level is not None:
                        long_stop_hit = (direction == "long" and lo <= stop_level)
                        short_stop_hit = (direction == "short" and hi >= stop_level)
                        if long_stop_hit or short_stop_hit:
                            stopped = True; exit_price = stop_level; exit_hr = hr
                            break
                continue
            # 已進場,先看這根有沒有停損;有的話路徑只採計到停損價為止
            if stop_on and stop_level is not None:
                long_stop_hit = (direction == "long" and lo <= stop_level)
                short_stop_hit = (direction == "short" and hi >= stop_level)
                if long_stop_hit or short_stop_hit:
                    stopped = True; exit_price = stop_level; exit_hr = hr
                    path_hi = max(path_hi, exit_price); path_lo = min(path_lo, exit_price)
                    break
            path_hi = max(path_hi, hi); path_lo = min(path_lo, lo)
        if not entered:
            continue
        if not stopped:
            exit_price = row[f"h{exit_hour}_close"]
            exit_hr = exit_hour
        if pd.isna(exit_price):
            continue
        raw_ret = (exit_price/entry_price - 1) if direction == "long" else -(exit_price/entry_price - 1)
        mfe, mae = _excursions(direction, entry_price, path_hi, path_lo)
        trades.append({"trade_date": row["trade_date"], "dow": row["dow"],
                        "entry_time": _hour_label(row["trade_date"], entry_hr),
                        "exit_time": _hour_label(row["trade_date"], exit_hr),
                        "entry_price": entry_price, "exit_price": exit_price,
                        "stopped": stopped,
                        "exit_reason": "stop" if stopped else "exit_hour",
                        "mfe": mfe, "mae": mae,
                        "ret_gross": raw_ret, "ret_net": raw_ret - cost})
    return pd.DataFrame(trades)


# ==================================================================
# 規則執行:波段模式(收盤進場、固定%停損、可多日持有;兩個資料集都支援,
# 因為只用到收盤/最高/最低價,不碰陳舊開盤價問題)
# ==================================================================

def run_swing(df: pd.DataFrame, mask: pd.Series, rule: dict) -> pd.DataFrame:
    """收盤進場後逐日往後追蹤,直到停損(或選配的停利)觸發、或達最長持有天數
    才出場。停損固定用「進場價的百分比」設定 —— 多日部位最自然的停損定義。

    如果訊號出現在資料尾端、還沒等到停損/停利/最長持有天數就沒資料了,
    這筆交易視為「未解決」而排除在統計之外(不知道真正結果前不亂猜)。
    """
    direction = rule.get("direction", "long")
    stop_pct = float(rule.get("stop_pct", 2.0)) / 100
    tp_on = bool(rule.get("take_profit_on", False))
    tp_pct = float(rule.get("take_profit_pct", 0) or 0) / 100
    max_hold = max(1, int(rule.get("max_hold_days", 60)))
    cost = float(rule.get("cost_pct", 0.03)) / 100

    n = len(df)
    highs = df["day_high"].values; lows = df["day_low"].values; closes = df["day_close"].values
    dates = df["trade_date"].values; dows = df["dow"].values

    trades = []
    unresolved = 0
    for i in df.index[mask]:
        entry_price = closes[i]
        if pd.isna(entry_price):
            continue
        if direction == "long":
            stop_level = entry_price * (1 - stop_pct)
            tp_level = entry_price * (1 + tp_pct) if tp_on else None
        else:
            stop_level = entry_price * (1 + stop_pct)
            tp_level = entry_price * (1 - tp_pct) if tp_on else None

        last_j = min(i + max_hold, n - 1)
        ran_out = (i + max_hold) >= n   # 最長持有天數還沒到,資料就先用完了
        exit_price = None; exit_reason = None; exit_j = None
        path_hi = -np.inf; path_lo = np.inf

        for j in range(i + 1, last_j + 1):
            hi, lo = highs[j], lows[j]
            if pd.isna(hi) or pd.isna(lo):
                continue
            stop_hit = (lo <= stop_level) if direction == "long" else (hi >= stop_level)
            tp_hit = (tp_on and ((hi >= tp_level) if direction == "long" else (lo <= tp_level)))
            if stop_hit:
                # 同一天停損停利條件都可能滿足時,保守假設停損先發生(避免高估)
                exit_price = stop_level; exit_reason = "stop"; exit_j = j
                # 出場當天只採計到出場價為止 —— 出場後的走勢與這筆部位無關,
                # 若把整根K的極值算進 MAE 會誇大「被套多深」
                path_hi = max(path_hi, exit_price); path_lo = min(path_lo, exit_price)
                break
            if tp_hit:
                exit_price = tp_level; exit_reason = "take_profit"; exit_j = j
                path_hi = max(path_hi, exit_price); path_lo = min(path_lo, exit_price)
                break
            # 這天沒出場,整根K的高低都是實際經歷過的未實現損益
            path_hi = max(path_hi, hi); path_lo = min(path_lo, lo)
            if j == last_j:
                if not ran_out:
                    exit_price = closes[j]; exit_reason = "max_hold"; exit_j = j
                # ran_out 且都沒觸發 -> exit_price 維持 None,視為未解決

        if exit_price is None:
            unresolved += 1
            continue

        raw_ret = (exit_price/entry_price - 1) if direction == "long" else -(exit_price/entry_price - 1)
        # 停損/停利是盤中觸價,只知道發生在那一天之內(日K精度),標「盤中」以免誤導;
        # 到期出場則是明確用當天收盤價
        exit_when = "收盤" if exit_reason == "max_hold" else "盤中觸價"
        mfe, mae = _excursions(direction, entry_price, path_hi, path_lo)
        trades.append({"trade_date": dates[i], "dow": int(dows[i]),
                        "entry_time": pd.Timestamp(dates[i]).strftime("%Y-%m-%d") + " 收盤",
                        "exit_time": pd.Timestamp(dates[exit_j]).strftime("%Y-%m-%d") + " " + exit_when,
                        "entry_price": float(entry_price), "exit_price": float(exit_price),
                        "exit_date": dates[exit_j], "hold_days": int(exit_j - i),
                        "stopped": exit_reason == "stop", "exit_reason": exit_reason,
                        "mfe": mfe, "mae": mae,
                        "ret_gross": raw_ret, "ret_net": raw_ret - cost})
    out = pd.DataFrame(trades)
    out.attrs["unresolved"] = unresolved
    return out


def _swing_extra_stats(trades: pd.DataFrame) -> dict:
    if trades.empty or "hold_days" not in trades.columns:
        return {}
    total = len(trades)
    reason_counts = trades["exit_reason"].value_counts().to_dict()
    reason_pct = {k: round(v/total*100, 1) for k, v in reason_counts.items()}

    # 重疊部位檢查:如果訊號常常在前一筆倉位還沒出場時就又出現,代表交易之間
    # 共用同一段價格路徑、不是獨立樣本 —— 跟本專案一路強調的自相關/有效樣本數
    # 問題是同一類陷阱,「交易數」看起來多不代表統計檢定力真的有那麼多。
    s = trades.sort_values("trade_date").reset_index(drop=True)
    overlap_count = 0
    running_max_exit = None
    for _, row in s.iterrows():
        if running_max_exit is not None and row["trade_date"] < running_max_exit:
            overlap_count += 1
        if running_max_exit is None or row["exit_date"] > running_max_exit:
            running_max_exit = row["exit_date"]
    overlap_pct = round(overlap_count/total*100, 1)

    return {
        "avg_hold_days": round(float(trades["hold_days"].mean()), 1),
        "exit_reason_pct": reason_pct,
        "overlap_pct": overlap_pct,
    }


# ==================================================================
# 規則執行:隔夜模式(兩個資料集都支援)
# ==================================================================

def run_overnight(df: pd.DataFrame, mask: pd.Series, rule: dict) -> pd.DataFrame:
    hold_to = rule.get("hold_to", "next_open")     # next_open | next_close | next_hour
    hold_hour = int(rule.get("hold_to_hour", 10))
    skip_weekend = bool(rule.get("skip_weekend", False))
    direction = rule.get("direction", "long")
    cost = float(rule.get("cost_pct", 0.03)) / 100
    has_path = bool(df["has_intraday_path"].iloc[0]) if len(df) else False

    d2 = df.copy()
    d2["next_date"] = d2["trade_date"].shift(-1)
    d2["next_gap_days"] = (d2["next_date"] - d2["trade_date"]).dt.days
    # 是否跨週末:用 ISO 年週判斷(不是單純看天數差,才不會被國定假日誤判)
    iso_this = d2["trade_date"].dt.isocalendar()
    iso_next = d2["next_date"].dt.isocalendar()
    d2["crosses_weekend"] = (iso_this["year"].values != iso_next["year"].values) | \
                             (iso_this["week"].values != iso_next["week"].values)
    if hold_to == "next_close":
        d2["exit_price"] = d2["day_close"].shift(-1)
    elif hold_to == "next_hour" and has_path:
        col = f"h{hold_hour}_close"
        d2["exit_price"] = d2[col].shift(-1) if col in d2.columns else np.nan
    else:  # next_open
        d2["exit_price"] = d2["day_open"].shift(-1)

    sel = mask & d2["exit_price"].notna()
    if skip_weekend:
        sel &= ~d2["crosses_weekend"]

    exit_label = {"next_close": "收盤", "next_open": "開盤"}.get(
        hold_to, f"{hold_hour:02d}:00-{_BAR_END.get(hold_hour, '')}")

    # MAE/MFE 需要持倉期間的價格路徑。隔夜倉的路徑資訊天生殘缺:
    # - hold_to=next_open:整段持倉都在休市,盤中根本沒有價格,MAE/MFE 無意義 -> None
    # - 有小時K時:可用隔日開盤到出場時點之間的小時K高低,但仍缺夜盤那段
    # 寧可標 None 也不要用不完整的路徑算出看似精確的數字。
    path_cols = []
    if has_path:
        if hold_to == "next_close":
            path_cols = [9, 10, 11, 12, 13]
        elif hold_to == "next_hour":
            path_cols = [h for h in (9, 10, 11, 12, 13) if h <= hold_hour]
    for hcol in path_cols:
        d2[f"nx_h{hcol}_high"] = d2[f"h{hcol}_high"].shift(-1)
        d2[f"nx_h{hcol}_low"] = d2[f"h{hcol}_low"].shift(-1)

    trades = []
    for _, row in d2[sel].iterrows():
        entry_price = row["day_close"]
        exit_price = row["exit_price"]
        raw_ret = (exit_price/entry_price - 1) if direction == "long" else -(exit_price/entry_price - 1)
        nd = row["next_date"]
        if path_cols:
            his = [row[f"nx_h{h}_high"] for h in path_cols]
            los = [row[f"nx_h{h}_low"] for h in path_cols]
            his = [x for x in his if pd.notna(x)]; los = [x for x in los if pd.notna(x)]
            mfe, mae = _excursions(direction, entry_price,
                                    max(his) if his else np.nan,
                                    min(los) if los else np.nan)
        else:
            mfe = mae = None
        trades.append({"trade_date": row["trade_date"], "dow": row["dow"],
                        "entry_time": pd.Timestamp(row["trade_date"]).strftime("%Y-%m-%d") + " 收盤",
                        "exit_time": (pd.Timestamp(nd).strftime("%Y-%m-%d") + " " + exit_label)
                                      if pd.notna(nd) else None,
                        "entry_price": entry_price, "exit_price": exit_price,
                        "stopped": False, "exit_reason": "hold_to",
                        "mfe": mfe, "mae": mae,
                        "ret_gross": raw_ret, "ret_net": raw_ret - cost})
    return pd.DataFrame(trades)


# ==================================================================
# 統計檢驗套件
# ==================================================================

def _max_drawdown(equity: np.ndarray) -> dict:
    """權益曲線的最大回撤(peak-to-trough)。注意這是「每筆交易複利」的曲線,
    不是真實資金曲線(沒有部位大小、保證金、閒置資金的概念),只反映策略本身
    連續虧損的嚴重程度,不能直接當「我的帳戶會賠多少」看。
    """
    if len(equity) == 0:
        return {}
    peak = np.maximum.accumulate(equity)
    dd = equity / peak - 1
    trough_i = int(np.argmin(dd))
    peak_i = int(np.argmax(equity[:trough_i + 1])) if trough_i > 0 else 0
    # 回撤修復:回到前高需要幾筆交易(還沒修復就是 None)
    recover_i = None
    after = np.where(equity[trough_i:] >= equity[peak_i])[0]
    if len(after):
        recover_i = int(trough_i + after[0])
    return {
        "mdd_pct": round(float(dd.min()) * 100, 3),
        "peak_idx": peak_i, "trough_idx": trough_i,
        "recover_idx": recover_i,
        "trades_to_recover": (recover_i - trough_i) if recover_i is not None else None,
        "longest_dd_trades": int(_longest_underwater(dd)),
    }


def _longest_underwater(dd: np.ndarray) -> int:
    """權益曲線待在水面下(未創新高)最久的一段有幾筆交易。"""
    longest = cur = 0
    for x in dd:
        if x < 0:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return longest


def _streaks(wins: np.ndarray) -> dict:
    """最大連續獲利/虧損次數(連續虧損次數是心理面與資金控管的關鍵)。"""
    max_w = max_l = cur_w = cur_l = 0
    for w in wins:
        if w:
            cur_w += 1; cur_l = 0
        else:
            cur_l += 1; cur_w = 0
        max_w = max(max_w, cur_w); max_l = max(max_l, cur_l)
    return {"max_consec_wins": int(max_w), "max_consec_losses": int(max_l)}


def _monthly_table(s: pd.DataFrame) -> list:
    """逐月報酬表(該月所有交易複利)。月份沒有交易就不列。"""
    t = s.copy()
    t["ym"] = pd.to_datetime(t["trade_date"]).dt.to_period("M")
    out = []
    for ym, g in t.groupby("ym"):
        ret = float((1 + g["ret_net"]).prod() - 1)
        out.append({"month": str(ym), "n": len(g),
                     "ret_pct": round(ret * 100, 3),
                     "win_rate": round(float((g["ret_net"] > 0).mean()) * 100, 1)})
    return out


def _excursion_stats(trades: pd.DataFrame) -> dict:
    """MAE/MFE 匯總,並拆成贏家與輸家 —— 這組數字是調停損/停利最直接的依據。"""
    if "mae" not in trades.columns:
        return {}
    t = trades.dropna(subset=["mae", "mfe"])
    if t.empty:
        return {}
    win = t[t["ret_net"] > 0]; loss = t[t["ret_net"] <= 0]
    def pack(g):
        if g.empty:
            return None
        return {"n": len(g),
                 "mae_avg_pct": round(float(g["mae"].mean()) * 100, 3),
                 "mae_p90_pct": round(float(g["mae"].quantile(0.10)) * 100, 3),  # 最差的10%
                 "mfe_avg_pct": round(float(g["mfe"].mean()) * 100, 3)}
    return {"excursion": {"all": pack(t), "winners": pack(win), "losers": pack(loss),
                           "coverage": f"{len(t)}/{len(trades)}"}}


def _block_bootstrap_ci(returns: np.ndarray, block=20, n_boot=3000, seed=0):
    n = len(returns)
    if n < block * 3:
        return None
    rng = np.random.default_rng(seed)
    nb = n // block
    means = []
    for _ in range(n_boot):
        starts = rng.integers(0, n - block, nb)
        idx = np.concatenate([np.arange(s, s+block) for s in starts])
        means.append(returns[idx].mean())
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {"lo": float(lo), "hi": float(hi), "block": block, "n_blocks": nb}


def compute_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"n": 0, "error": "沒有任何交易被觸發,請調整規則或篩選條件"}

    r = trades["ret_net"].values
    n = len(r)
    mean = float(np.mean(r)); std = float(np.std(r, ddof=1)) if n > 1 else 0.0
    win_rate = float((r > 0).mean())
    t, p = stats.ttest_1samp(r, 0) if n > 1 else (np.nan, np.nan)
    sharpe = (mean/std*np.sqrt(252)) if std > 0 else np.nan

    s = trades.sort_values("trade_date").reset_index(drop=True)
    half = n // 2
    front = float(s["ret_net"].iloc[:half].mean()) if half > 0 else np.nan
    back = float(s["ret_net"].iloc[half:].mean()) if (n-half) > 0 else np.nan

    boot = _block_bootstrap_ci(r, block=min(20, max(5, n//10 or 1)))

    # 成本敏感度:成本從0到現有成本的3倍,看EV怎麼變
    cur_cost = float(trades.attrs.get("cost_pct", 0.03))
    gross = trades["ret_gross"].values
    cost_curve = []
    for mult in [0, 0.5, 1, 1.5, 2, 3]:
        c = cur_cost/100 * mult
        cost_curve.append({"cost_pct": round(cur_cost*mult, 4), "ev_pct": round(float((gross-c).mean())*100, 4)})

    dow_stats = []
    for dw, g in trades.groupby("dow"):
        dow_stats.append({"dow": int(dw), "n": len(g), "ev_pct": round(float(g["ret_net"].mean())*100, 4),
                           "win_rate": round(float((g["ret_net"] > 0).mean())*100, 1)})

    equity = (1 + s["ret_net"]).cumprod()
    eq = equity.values

    # 獲利因子 = 總獲利 / 總虧損(絕對值)。>1 才賺錢;XQ 等平台的標配指標。
    gains = r[r > 0].sum(); losses = -r[r < 0].sum()
    profit_factor = float(gains/losses) if losses > 0 else None
    avg_win = float(r[r > 0].mean()) if (r > 0).any() else None
    avg_loss = float(r[r < 0].mean()) if (r < 0).any() else None

    dd = _max_drawdown(eq)
    streak = _streaks(r > 0)

    result = {
        "n": n, "win_rate": round(win_rate*100, 2), "ev_pct": round(mean*100, 4),
        "t_stat": None if np.isnan(t) else round(float(t), 3),
        "p_value": None if np.isnan(p) else round(float(p), 4),
        "sharpe": None if np.isnan(sharpe) else round(float(sharpe), 3),
        "std_pct": round(std*100, 4),
        "worst_pct": round(float(r.min())*100, 3), "best_pct": round(float(r.max())*100, 3),
        "front_half_ev_pct": None if np.isnan(front) else round(front*100, 4),
        "back_half_ev_pct": None if np.isnan(back) else round(back*100, 4),
        "block_bootstrap_ci": boot and {"lo_pct": round(boot["lo"]*100, 4), "hi_pct": round(boot["hi"]*100, 4),
                                          "n_blocks": boot["n_blocks"], "block_days": boot["block"]},
        "cost_sensitivity": cost_curve,
        "by_weekday": sorted(dow_stats, key=lambda x: x["dow"]),
        "equity_curve": [{"date": d.strftime("%Y-%m-%d"), "equity": round(float(e), 4)}
                          for d, e in zip(s["trade_date"], equity)],
        "stopped_rate": round(float(trades["stopped"].mean())*100, 1) if "stopped" in trades else None,
        "trades": _trade_rows(s),
        # ---- 報表層 ----
        "profit_factor": None if profit_factor is None else round(profit_factor, 3),
        "avg_win_pct": None if avg_win is None else round(avg_win*100, 4),
        "avg_loss_pct": None if avg_loss is None else round(avg_loss*100, 4),
        "payoff_ratio": (round(abs(avg_win/avg_loss), 3)
                          if (avg_win is not None and avg_loss not in (None, 0)) else None),
        "max_drawdown": dd,
        "monthly": _monthly_table(s),
        **streak,
    }
    result.update(_excursion_stats(s))
    return result


def _trade_rows(s: pd.DataFrame) -> list:
    """每筆交易的進出場明細(已依日期排序),給前端表格用。"""
    rows = []
    for _, t in s.iterrows():
        rows.append({
            "date": pd.Timestamp(t["trade_date"]).strftime("%Y-%m-%d"),
            "dow": int(t["dow"]),
            "entry_time": t.get("entry_time"),
            "exit_time": t.get("exit_time"),
            "entry_price": round(float(t["entry_price"]), 2),
            "exit_price": round(float(t["exit_price"]), 2),
            "hold_days": int(t["hold_days"]) if "hold_days" in s.columns and pd.notna(t.get("hold_days")) else None,
            "exit_reason": t.get("exit_reason"),
            "mfe_pct": (round(float(t["mfe"])*100, 3)
                         if ("mfe" in s.columns and pd.notna(t.get("mfe"))) else None),
            "mae_pct": (round(float(t["mae"])*100, 3)
                         if ("mae" in s.columns and pd.notna(t.get("mae"))) else None),
            "ret_net_pct": round(float(t["ret_net"])*100, 4),
        })
    return rows


# ==================================================================
# 對外主函式
# ==================================================================

def run_backtest(conn: sqlite3.Connection, rule: dict) -> dict:
    # Tiny compat: accept v1 blocks JSON (filters as a list) or the legacy flat rule.
    from web.strategy_blocks import BlocksError, coerce_rule
    try:
        rule = coerce_rule(rule)
    except BlocksError as exc:
        return {"error": str(exc)}
    ds = rule.get("dataset", "2y_hourly")
    if ds not in DATASETS:
        return {"error": f"未知資料集 {ds}"}
    df = DATASETS[ds](conn)
    mask = apply_filters(df, rule.get("filters", {}))

    mode = rule.get("mode", "intraday")
    if mode == "intraday":
        if not df["has_intraday_path"].iloc[0]:
            return {"error": "15年日K資料集沒有小時級路徑,不支援日內事件觸發模式。"
                              "請改選「隔夜模式」或「波段模式」,或切換資料集為2年小時K。"}
        if _uses_close_decided_filters(rule.get("filters", {})):
            return {"error": "「當日漲跌」「今日均線」「N日新高/新低突破」「均線交叉」這幾種濾網要等"
                              "今天收盤才能確定,用在日內模式(進場時機通常早於收盤)等於偷看未來資訊。"
                              "這些濾網只適用於隔夜或波段模式。"}
        trades = run_intraday(df, mask, rule)
    elif mode == "swing":
        trades = run_swing(df, mask, rule)
    else:
        trades = run_overnight(df, mask, rule)

    unresolved = int(trades.attrs.get("unresolved", 0))
    trades.attrs["cost_pct"] = rule.get("cost_pct", 0.03)
    result = compute_stats(trades)
    if mode == "swing" and result.get("n"):
        result.update(_swing_extra_stats(trades))
        result["unresolved_trades"] = unresolved
    result["dataset"] = ds
    result["mode"] = mode
    result["direction"] = (rule.get("entry", {}).get("direction") if mode == "intraday"
                            else rule.get("direction", "long"))
    result["total_days_in_dataset"] = int(len(df))
    result["days_passed_filter"] = int(mask.sum())
    # 波段模式完全不用開盤價(進場用收盤、停損停利用高低點),不受陳舊開盤價問題影響
    result["stale_open_warning"] = bool(df["stale_open_risk"].iloc[0]) if (len(df) and mode != "swing") else False
    if result.get("n"):
        result["price_series"] = _price_series(df, trades)
    return result


def _price_series(df: pd.DataFrame, trades: pd.DataFrame) -> list:
    """交易期間的指數收盤序列,供前端把進出場位置標在走勢圖上。
    只取第一筆到最後一筆交易涵蓋的範圍,避免把整段沒交易的期間也畫進去。
    """
    if trades.empty:
        return []
    lo = pd.Timestamp(trades["trade_date"].min())
    hi_col = "exit_date" if "exit_date" in trades.columns else "trade_date"
    hi = pd.Timestamp(pd.to_datetime(trades[hi_col]).max())
    m = (df["trade_date"] >= lo) & (df["trade_date"] <= hi + pd.Timedelta(days=5))
    sub = df.loc[m, ["trade_date", "day_close"]].dropna()
    return [{"date": d.strftime("%Y-%m-%d"), "close": round(float(c), 2)}
             for d, c in zip(sub["trade_date"], sub["day_close"])]
