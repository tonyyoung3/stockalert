from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd

import db
from screener import classify_pattern

ALLOWED_PATTERNS = frozenset({"upper_shadow_reversal", "inside_day"})
MAX_LIMIT = 50


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., dict[str, Any]]


def normalize_ticker(raw: str) -> tuple[str, str]:
    """Return (db_ticker, yfinance_symbol)."""
    text = (raw or "").strip().upper()
    if not text:
        raise ValueError("ticker is required")
    if text.endswith(".TW") or text.endswith(".TWO"):
        return text.split(".", 1)[0], text
    if text.isdigit():
        return text, f"{text}.TW"
    return text, text


def flatten_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance sometimes returns MultiIndex columns even for one ticker."""
    if df is None or df.empty:
        return df
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [col[0] if isinstance(col, tuple) else col for col in out.columns]
    return out


def _clamp_limit(value: Any, default: int = 20) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, MAX_LIMIT))


def _optional_pattern(value: Any) -> str | None:
    if value in (None, ""):
        return None
    pattern = str(value).strip()
    if pattern not in ALLOWED_PATTERNS:
        raise ValueError(f"pattern_type must be one of {sorted(ALLOWED_PATTERNS)}")
    return pattern


def _since_from_days(days: Any) -> str:
    try:
        n = int(days)
    except (TypeError, ValueError):
        n = 14
    n = max(1, min(n, 365))
    return str(date.today() - timedelta(days=n))


def _default_fetch_ohlcv(symbol: str) -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download(
        symbol,
        period="2mo",
        interval="1d",
        progress=False,
        auto_adjust=True,
        group_by="ticker",
    )
    if raw.empty:
        return raw
    if isinstance(raw.columns, pd.MultiIndex) and symbol in raw.columns.get_level_values(0):
        return flatten_ohlcv(raw[symbol])
    return flatten_ohlcv(raw)


def list_recent_alerts(
    days: int = 14,
    pattern_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    since = _since_from_days(days)
    rows = db.list_alerts(
        pattern_type=_optional_pattern(pattern_type),
        since=since,
        limit=_clamp_limit(limit),
    )
    return {"since": since, "count": len(rows), "alerts": rows}


def lookup_alert_history(ticker: str, limit: int = 20) -> dict[str, Any]:
    db_ticker, symbol = normalize_ticker(ticker)
    rows = db.list_alert_history(db_ticker, limit=_clamp_limit(limit))
    return {"ticker": db_ticker, "symbol": symbol, "count": len(rows), "alerts": rows}


def summarize_performance(pattern_type: str | None = None) -> dict[str, Any]:
    return db.performance_summary(pattern_type=_optional_pattern(pattern_type))


def list_pending_checks(limit: int = 20) -> dict[str, Any]:
    pending = db.get_pending_alerts()
    cap = _clamp_limit(limit)
    rows = [
        {
            "id": row["id"],
            "ticker": row["ticker"],
            "pattern_type": row["pattern_type"],
            "alert_date": row["alert_date"],
            "price_at_alert": row["price_at_alert"],
        }
        for row in pending[:cap]
    ]
    return {"pending": len(pending), "showing": len(rows), "alerts": rows}


def check_ticker_pattern(
    ticker: str,
    fetch_ohlcv: Callable[[str], pd.DataFrame] | None = None,
) -> dict[str, Any]:
    db_ticker, symbol = normalize_ticker(ticker)
    fetch = fetch_ohlcv or _default_fetch_ohlcv
    df = flatten_ohlcv(fetch(symbol))
    if df is None or df.empty or len(df) < 22:
        return {
            "ticker": db_ticker,
            "symbol": symbol,
            "pattern": None,
            "error": "insufficient_price_data",
            "bars": 0 if df is None or df.empty else int(len(df)),
        }

    last = df.iloc[-1]
    last_close = float(last["Close"])
    last_date = str(pd.Timestamp(df.index[-1]).date())
    return {
        "ticker": db_ticker,
        "symbol": symbol,
        "pattern": classify_pattern(df),
        "last_date": last_date,
        "last_close": last_close,
        "bars": int(len(df)),
    }


def default_tools(
    fetch_ohlcv: Callable[[str], pd.DataFrame] | None = None,
) -> list[Tool]:
    def _check(ticker: str) -> dict[str, Any]:
        return check_ticker_pattern(ticker, fetch_ohlcv=fetch_ohlcv)

    return [
        Tool(
            name="list_recent_alerts",
            description="List recent screener alerts from SQLite, newest first.",
            parameters={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Look back this many calendar days. Default 14."},
                    "pattern_type": {
                        "type": "string",
                        "enum": sorted(ALLOWED_PATTERNS),
                        "description": "Optional pattern filter.",
                    },
                    "limit": {"type": "integer", "description": "Max rows, cap 50."},
                },
            },
            handler=list_recent_alerts,
        ),
        Tool(
            name="lookup_alert_history",
            description="Alerts and 28-day returns for one ticker.",
            parameters={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "2330, 2330.TW, or a US symbol."},
                    "limit": {"type": "integer"},
                },
                "required": ["ticker"],
            },
            handler=lookup_alert_history,
        ),
        Tool(
            name="summarize_performance",
            description="Win rate and average 28-day return across checked alerts.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern_type": {
                        "type": "string",
                        "enum": sorted(ALLOWED_PATTERNS),
                    },
                },
            },
            handler=summarize_performance,
        ),
        Tool(
            name="list_pending_checks",
            description="Alerts at least 28 days old that still have no performance row.",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
            handler=list_pending_checks,
        ),
        Tool(
            name="check_ticker_pattern",
            description="Download recent bars and classify today's pattern for one ticker.",
            parameters={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                },
                "required": ["ticker"],
            },
            handler=_check,
        ),
    ]


def openai_tool_schemas(tools: list[Tool]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def execute_tool(name: str, arguments: dict[str, Any] | None, tools: list[Tool]) -> dict[str, Any]:
    registry = {tool.name: tool for tool in tools}
    tool = registry.get(name)
    if tool is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return tool.handler(**(arguments or {}))
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — surface tool failures to the model
        return {"error": f"{name} failed: {exc}"}


def dump_tool_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)
