"""v1 backtest rule-blocks model (AND only) and compile to the flat engine rule.

Schema (names match epic #40 / issues #41–#42; capabilities are 1:1 with
``web.backtest_engine.apply_filters`` + existing entry/exit fields):

.. code-block:: json

    {
      "version": 1,
      "dataset": "2y_hourly" | "15y_daily",
      "mode": "intraday" | "overnight" | "swing",
      "filters": [{"type": "<catalog>", "params": {…}}],
      "entry": {…},
      "exit": {…},
      "cost_pct": 0.03
    }

Filter types (AND): weekdays, trend, prev_day, gap, day_return, ma_cross,
breakout, oi_ratio. No OR / nested groups / scripts.

``POST /api/backtest`` accepts this document **or** the legacy flat rule.
``run_backtest`` compiles blocks server-side. Close-decided filters
(day_return, ma_cross, breakout, trend ``*_today``) are rejected in
intraday mode — same look-ahead guard as the engine.
"""
from __future__ import annotations

from copy import deepcopy

from web.backtest_engine import DATASETS, _uses_close_decided_filters

SCHEMA_VERSION = 1
MODES = ("intraday", "overnight", "swing")

FILTER_TYPES = (
    "weekdays",
    "trend",
    "prev_day",
    "gap",
    "day_return",
    "ma_cross",
    "breakout",
    "oi_ratio",
)

CLOSE_DECIDED_FILTER_TYPES = frozenset({"day_return", "ma_cross", "breakout"})
TREND_VALUES = (
    "none",
    "above_ma20",
    "below_ma20",
    "above_ma60",
    "below_ma60",
    "above_ma20_today",
    "below_ma20_today",
    "above_ma60_today",
    "below_ma60_today",
)
PREV_DAY_VALUES = ("none", "up", "down")
DIR_ANY = ("any", "up", "down")
MA_CROSS_VALUES = ("none", "golden", "death")
BREAKOUT_VALUES = ("none", "n_day_high", "n_day_low")
OI_MODE_VALUES = ("none", "below_pctile", "above_pctile")
DIRECTIONS = ("long", "short")
TRIGGERS = ("touch_from_above", "touch_from_below")
ENTRY_REFS = ("day_open", "first_hour_high", "first_hour_low", "prev_close")
STOP_REFS = ("day_open", "entry_price", "first_hour_high", "first_hour_low")
HOLD_TO_VALUES = ("next_open", "next_close", "next_hour")

CLOSE_DECIDED_INTRADAY_ERROR = (
    "「當日漲跌」「今日均線」「N日新高/新低突破」「均線交叉」這幾種濾網要等"
    "今天收盤才能確定,用在日內模式(進場時機通常早於收盤)等於偷看未來資訊。"
    "這些濾網只適用於隔夜或波段模式。"
)

ALL_WEEKDAYS = [0, 1, 2, 3, 4]


class BlocksError(ValueError):
    """Invalid v1 blocks document (unknown type, duplicate, look-ahead)."""


def default_filters() -> dict:
    """Flat ``filters`` dict the old dashboard form always posted."""
    return {
        "weekdays": list(ALL_WEEKDAYS),
        "trend": "none",
        "prev_day": "none",
        "gap_dir": "any",
        "gap_abs_min_pct": 0,
        "day_ret_dir": "any",
        "day_ret_min_pct": 0,
        "ma_cross": "none",
        "breakout": "none",
        "breakout_window": 20,
        "oi_ratio_mode": "none",
        "oi_ratio_pctile": 25,
        "oi_ratio_window": 60,
    }


def default_entry(mode: str) -> dict:
    if mode == "intraday":
        return {
            "direction": "long",
            "reference": "first_hour_high",
            "offset_pct": 0,
            "trigger": "touch_from_below",
            "earliest_hour": 10,
        }
    return {"direction": "long"}


def default_exit(mode: str) -> dict:
    if mode == "intraday":
        return {
            "exit_hour": 13,
            "stop_enabled": False,
            "stop_reference": "day_open",
            "stop_offset_pct": 0,
        }
    if mode == "overnight":
        return {
            "hold_to": "next_open",
            "hold_to_hour": 10,
            "skip_weekend": True,
        }
    return {
        "stop_pct": 2,
        "max_hold_days": 60,
        "take_profit_on": False,
        "take_profit_pct": 5,
    }


def is_blocks_payload(payload) -> bool:
    """True when ``filters`` is a list (v1 blocks), not the flat engine dict."""
    if not isinstance(payload, dict):
        return False
    filters = payload.get("filters")
    if isinstance(filters, list):
        return True
    if payload.get("version") == SCHEMA_VERSION and not isinstance(filters, dict):
        return True
    return False


def filter_block_is_close_decided(block: dict) -> bool:
    if not isinstance(block, dict):
        return False
    kind = block.get("type")
    if kind in CLOSE_DECIDED_FILTER_TYPES:
        return True
    if kind == "trend":
        return str((block.get("params") or {}).get("value") or "").endswith("_today")
    return False


def _expect(cond, message):
    if not cond:
        raise BlocksError(message)


def _as_float(value, default=0):
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value, default=0):
    try:
        if value is None or value == "":
            return int(default)
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _params(block) -> dict:
    params = block.get("params") if isinstance(block, dict) else None
    return params if isinstance(params, dict) else {}


def _apply_filter_block(filters: dict, block: dict) -> None:
    kind = block.get("type")
    params = _params(block)
    if kind == "weekdays":
        days = params.get("days", ALL_WEEKDAYS)
        _expect(isinstance(days, (list, tuple)), "weekdays.params.days 必須是陣列")
        out = []
        for d in days:
            n = _as_int(d, -1)
            if 0 <= n <= 4:
                out.append(n)
        filters["weekdays"] = sorted(set(out))
        return
    if kind == "trend":
        value = params.get("value", "none")
        _expect(value in TREND_VALUES, f"未知趨勢濾網 {value}")
        filters["trend"] = value
        return
    if kind == "prev_day":
        value = params.get("value", "none")
        _expect(value in PREV_DAY_VALUES, f"未知前一日濾網 {value}")
        filters["prev_day"] = value
        return
    if kind == "gap":
        direction = params.get("dir", "any")
        _expect(direction in DIR_ANY, f"未知跳空方向 {direction}")
        filters["gap_dir"] = direction
        filters["gap_abs_min_pct"] = _as_float(params.get("abs_min_pct"), 0)
        return
    if kind == "day_return":
        direction = params.get("dir", "any")
        _expect(direction in DIR_ANY, f"未知當日漲跌方向 {direction}")
        filters["day_ret_dir"] = direction
        filters["day_ret_min_pct"] = _as_float(params.get("min_pct"), 0)
        return
    if kind == "ma_cross":
        value = params.get("value", "none")
        _expect(value in MA_CROSS_VALUES, f"未知均線交叉 {value}")
        filters["ma_cross"] = value
        return
    if kind == "breakout":
        value = params.get("kind", "none")
        _expect(value in BREAKOUT_VALUES, f"未知突破種類 {value}")
        filters["breakout"] = value
        filters["breakout_window"] = max(2, _as_int(params.get("window"), 20))
        return
    if kind == "oi_ratio":
        mode = params.get("mode", "none")
        _expect(mode in OI_MODE_VALUES, f"未知 OI 比模式 {mode}")
        filters["oi_ratio_mode"] = mode
        filters["oi_ratio_pctile"] = _as_float(params.get("pctile"), 25)
        filters["oi_ratio_window"] = max(10, _as_int(params.get("window"), 60))
        return
    raise BlocksError(f"未知濾網積木 type={kind}")


def _compile_intraday(entry: dict, exit_block: dict, rule: dict) -> None:
    direction = entry.get("direction", "long")
    reference = entry.get("reference", "first_hour_high")
    trigger = entry.get("trigger", "touch_from_below")
    _expect(direction in DIRECTIONS, f"未知方向 {direction}")
    _expect(reference in ENTRY_REFS, f"未知進場參考價 {reference}")
    _expect(trigger in TRIGGERS, f"未知觸發 {trigger}")
    rule["entry"] = {
        "reference": reference,
        "offset_pct": _as_float(entry.get("offset_pct"), 0),
        "trigger": trigger,
        "direction": direction,
        "earliest_hour": _as_int(entry.get("earliest_hour"), 10),
    }
    stop_ref = exit_block.get("stop_reference", "day_open")
    _expect(stop_ref in STOP_REFS, f"未知停損參考價 {stop_ref}")
    rule["exit_hour"] = _as_int(exit_block.get("exit_hour"), 13)
    rule["stop"] = {
        "enabled": bool(exit_block.get("stop_enabled", False)),
        "reference": stop_ref,
        "offset_pct": _as_float(exit_block.get("stop_offset_pct"), 0),
    }


def _compile_overnight(entry: dict, exit_block: dict, rule: dict) -> None:
    direction = entry.get("direction", "long")
    hold_to = exit_block.get("hold_to", "next_open")
    _expect(direction in DIRECTIONS, f"未知方向 {direction}")
    _expect(hold_to in HOLD_TO_VALUES, f"未知隔夜出場 {hold_to}")
    rule["direction"] = direction
    rule["hold_to"] = hold_to
    rule["hold_to_hour"] = _as_int(exit_block.get("hold_to_hour"), 10)
    rule["skip_weekend"] = bool(exit_block.get("skip_weekend", True))


def _compile_swing(entry: dict, exit_block: dict, rule: dict) -> None:
    direction = entry.get("direction", "long")
    _expect(direction in DIRECTIONS, f"未知方向 {direction}")
    rule["direction"] = direction
    rule["stop_pct"] = _as_float(exit_block.get("stop_pct"), 2)
    rule["max_hold_days"] = max(1, _as_int(exit_block.get("max_hold_days"), 60))
    rule["take_profit_on"] = bool(exit_block.get("take_profit_on", False))
    rule["take_profit_pct"] = _as_float(exit_block.get("take_profit_pct"), 5)


def blocks_to_rule(blocks: dict) -> dict:
    """Compile a v1 blocks document to the flat dict ``run_backtest`` already consumes."""
    _expect(isinstance(blocks, dict), "積木文件必須是物件")
    version = blocks.get("version", SCHEMA_VERSION)
    _expect(version == SCHEMA_VERSION, f"不支援的積木 schema version={version}")
    dataset = blocks.get("dataset", "2y_hourly")
    _expect(dataset in DATASETS, f"未知資料集 {dataset}")
    mode = blocks.get("mode", "intraday")
    _expect(mode in MODES, f"未知模式 {mode}")

    raw_filters = blocks.get("filters") or []
    _expect(isinstance(raw_filters, list), "filters 必須是積木陣列")
    seen = set()
    filters = default_filters()
    for block in raw_filters:
        _expect(isinstance(block, dict) and block.get("type"), "每個濾網積木需要 type")
        kind = block["type"]
        _expect(kind in FILTER_TYPES, f"未知濾網積木 type={kind}")
        _expect(kind not in seen, f"v1 每個濾網種類只能一塊（AND），重複了 {kind}")
        seen.add(kind)
        _apply_filter_block(filters, block)

    entry = blocks.get("entry") if isinstance(blocks.get("entry"), dict) else {}
    exit_block = blocks.get("exit") if isinstance(blocks.get("exit"), dict) else {}
    entry = {**default_entry(mode), **entry}
    exit_block = {**default_exit(mode), **exit_block}

    rule = {
        "dataset": dataset,
        "mode": mode,
        "filters": filters,
        "cost_pct": _as_float(blocks.get("cost_pct"), 0.03),
    }
    if mode == "intraday":
        _compile_intraday(entry, exit_block, rule)
    elif mode == "overnight":
        _compile_overnight(entry, exit_block, rule)
    else:
        _compile_swing(entry, exit_block, rule)

    if mode == "intraday" and _uses_close_decided_filters(filters):
        raise BlocksError(CLOSE_DECIDED_INTRADAY_ERROR)
    return rule


def _weekday_block(days) -> dict | None:
    if not days:
        return None
    days = [int(d) for d in days if d is not None]
    if sorted(days) == ALL_WEEKDAYS:
        return None
    return {"type": "weekdays", "params": {"days": days}}


def rule_to_blocks(rule: dict) -> dict:
    """Inverse of ``blocks_to_rule``: only emit filter blocks that are active."""
    _expect(isinstance(rule, dict), "規則必須是物件")
    if is_blocks_payload(rule):
        return deepcopy(rule)
    mode = rule.get("mode", "intraday")
    _expect(mode in MODES, f"未知模式 {mode}")
    dataset = rule.get("dataset", "2y_hourly")
    filters = rule.get("filters") if isinstance(rule.get("filters"), dict) else {}
    blocks = {
        "version": SCHEMA_VERSION,
        "dataset": dataset,
        "mode": mode,
        "filters": [],
        "entry": default_entry(mode),
        "exit": default_exit(mode),
        "cost_pct": _as_float(rule.get("cost_pct"), 0.03),
    }

    wd = _weekday_block(filters.get("weekdays"))
    if wd:
        blocks["filters"].append(wd)
    trend = filters.get("trend", "none")
    if trend not in (None, "", "none"):
        blocks["filters"].append({"type": "trend", "params": {"value": trend}})
    prev_day = filters.get("prev_day", "none")
    if prev_day not in (None, "", "none"):
        blocks["filters"].append({"type": "prev_day", "params": {"value": prev_day}})
    gap_dir = filters.get("gap_dir", "any")
    gap_min = _as_float(filters.get("gap_abs_min_pct"), 0)
    if gap_dir != "any" or gap_min > 0:
        blocks["filters"].append({
            "type": "gap",
            "params": {"dir": gap_dir, "abs_min_pct": gap_min},
        })
    day_dir = filters.get("day_ret_dir", "any")
    day_min = _as_float(filters.get("day_ret_min_pct"), 0)
    if day_dir != "any" or day_min > 0:
        blocks["filters"].append({
            "type": "day_return",
            "params": {"dir": day_dir, "min_pct": day_min},
        })
    ma_cross = filters.get("ma_cross", "none")
    if ma_cross not in (None, "", "none"):
        blocks["filters"].append({"type": "ma_cross", "params": {"value": ma_cross}})
    breakout = filters.get("breakout", "none")
    if breakout not in (None, "", "none"):
        blocks["filters"].append({
            "type": "breakout",
            "params": {
                "kind": breakout,
                "window": _as_int(filters.get("breakout_window"), 20),
            },
        })
    oi_mode = filters.get("oi_ratio_mode", "none")
    if oi_mode not in (None, "", "none"):
        blocks["filters"].append({
            "type": "oi_ratio",
            "params": {
                "mode": oi_mode,
                "pctile": _as_float(filters.get("oi_ratio_pctile"), 25),
                "window": _as_int(filters.get("oi_ratio_window"), 60),
            },
        })

    if mode == "intraday":
        entry = rule.get("entry") if isinstance(rule.get("entry"), dict) else {}
        stop = rule.get("stop") if isinstance(rule.get("stop"), dict) else {}
        blocks["entry"] = {
            "direction": entry.get("direction", "long"),
            "reference": entry.get("reference", "first_hour_high"),
            "offset_pct": _as_float(entry.get("offset_pct"), 0),
            "trigger": entry.get("trigger", "touch_from_below"),
            "earliest_hour": _as_int(entry.get("earliest_hour"), 10),
        }
        blocks["exit"] = {
            "exit_hour": _as_int(rule.get("exit_hour"), 13),
            "stop_enabled": bool(stop.get("enabled", False)),
            "stop_reference": stop.get("reference", "day_open"),
            "stop_offset_pct": _as_float(stop.get("offset_pct"), 0),
        }
    elif mode == "overnight":
        blocks["entry"] = {"direction": rule.get("direction", "long")}
        blocks["exit"] = {
            "hold_to": rule.get("hold_to", "next_open"),
            "hold_to_hour": _as_int(rule.get("hold_to_hour"), 10),
            "skip_weekend": bool(rule.get("skip_weekend", True)),
        }
    else:
        blocks["entry"] = {"direction": rule.get("direction", "long")}
        blocks["exit"] = {
            "stop_pct": _as_float(rule.get("stop_pct"), 2),
            "max_hold_days": _as_int(rule.get("max_hold_days"), 60),
            "take_profit_on": bool(rule.get("take_profit_on", False)),
            "take_profit_pct": _as_float(rule.get("take_profit_pct"), 5),
        }
    return blocks


def coerce_rule(payload: dict) -> dict:
    """Accept v1 blocks or a legacy flat rule; always return the flat engine rule."""
    if is_blocks_payload(payload):
        return blocks_to_rule(payload)
    if not isinstance(payload, dict):
        raise BlocksError("規則必須是物件")
    return payload


def _dow_label(days) -> str:
    names = ["一", "二", "三", "四", "五"]
    if not days or sorted(days) == ALL_WEEKDAYS:
        return "週一～週五"
    return "週" + "、".join(names[d] for d in days if 0 <= d <= 4)


def _trend_label(value: str) -> str:
    return {
        "above_ma20": "前收 > MA20",
        "below_ma20": "前收 < MA20",
        "above_ma60": "前收 > MA60",
        "below_ma60": "前收 < MA60",
        "above_ma20_today": "今收 > MA20",
        "below_ma20_today": "今收 < MA20",
        "above_ma60_today": "今收 > MA60",
        "below_ma60_today": "今收 < MA60",
    }.get(value, value)


def summarize_blocks(blocks: dict) -> list[str]:
    """Human-readable chips for the pre-run summary (Traditional Chinese)."""
    if not is_blocks_payload(blocks):
        blocks = rule_to_blocks(blocks)
    mode = blocks.get("mode", "intraday")
    dataset = blocks.get("dataset", "2y_hourly")
    chips = [
        "2年小時K" if dataset == "2y_hourly" else "15年日K",
        {"intraday": "日內", "overnight": "隔夜", "swing": "波段"}.get(mode, mode),
    ]
    for block in blocks.get("filters") or []:
        kind = block.get("type")
        params = _params(block)
        if kind == "weekdays":
            chips.append("若 " + _dow_label(params.get("days") or ALL_WEEKDAYS))
        elif kind == "trend":
            chips.append("若 " + _trend_label(params.get("value", "")))
        elif kind == "prev_day":
            chips.append("若 前一日" + ("上漲" if params.get("value") == "up" else "下跌"))
        elif kind == "gap":
            dmap = {"up": "跳空漲", "down": "跳空跌", "any": "跳空"}
            text = "若 " + dmap.get(params.get("dir"), "跳空")
            amin = _as_float(params.get("abs_min_pct"), 0)
            if amin > 0:
                text += f" |跳空|≥{amin:g}%"
            chips.append(text)
        elif kind == "day_return":
            dmap = {"up": "當日上漲", "down": "當日下跌", "any": "當日漲跌"}
            text = "若 " + dmap.get(params.get("dir"), "當日漲跌")
            amin = _as_float(params.get("min_pct"), 0)
            if amin > 0:
                text += f" |漲跌|≥{amin:g}%"
            chips.append(text)
        elif kind == "ma_cross":
            chips.append("若 黃金交叉" if params.get("value") == "golden" else "若 死亡交叉")
        elif kind == "breakout":
            window = _as_int(params.get("window"), 20)
            if params.get("kind") == "n_day_low":
                chips.append(f"若 破{window}日新低")
            else:
                chips.append(f"若 創{window}日新高")
        elif kind == "oi_ratio":
            mode_oi = params.get("mode")
            pctile = _as_float(params.get("pctile"), 25)
            window = _as_int(params.get("window"), 60)
            cmp_ = "低於" if mode_oi == "below_pctile" else "高於"
            chips.append(f"若 外資/投信 OI 比{cmp_}{window}日{pctile:g}%分位")
    entry = blocks.get("entry") or {}
    exit_block = blocks.get("exit") or {}
    direction = "做多" if entry.get("direction", "long") == "long" else "做空"
    if mode == "intraday":
        ref = {
            "first_hour_high": "第一小時高點",
            "first_hour_low": "第一小時低點",
            "day_open": "開盤價",
            "prev_close": "前收",
        }.get(entry.get("reference"), entry.get("reference", ""))
        trig = "向下觸及" if entry.get("trigger") == "touch_from_below" else "向上觸及"
        chips.append(f"則進場 {direction} {ref} {trig}")
        eh = _as_int(exit_block.get("exit_hour"), 13)
        chips.append("則出場 13:30 收盤" if eh == 13 else f"則出場 {eh}:00 收盤")
        if exit_block.get("stop_enabled"):
            chips.append("停損開")
    elif mode == "overnight":
        hold = {
            "next_open": "隔日開盤",
            "next_close": "隔日收盤",
            "next_hour": f"隔日{exit_block.get('hold_to_hour', 10)}:00",
        }.get(exit_block.get("hold_to"), "隔日出場")
        chips.append(f"則進場 {direction} 收盤")
        chips.append("則出場 " + hold)
        if exit_block.get("skip_weekend", True):
            chips.append("跳過週末")
    else:
        chips.append(f"則進場 {direction} 收盤")
        chips.append(f"停損 {exit_block.get('stop_pct', 2):g}%")
        if exit_block.get("take_profit_on"):
            chips.append(f"停利 {exit_block.get('take_profit_pct', 5):g}%")
        chips.append(f"最長持有 {exit_block.get('max_hold_days', 60)} 日")
    chips.append(f"成本 { _as_float(blocks.get('cost_pct'), 0.03):g}%")
    return chips
