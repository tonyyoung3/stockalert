"""Rank stocks by close-to-close performance from stored daily bars."""
from __future__ import annotations

import re


_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _value(qs, key: str) -> str:
    return (qs.get(key, [""])[0] or "").strip()


def _clamp_int(raw, default: int, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


def _window(conn, qs) -> tuple[str | None, str | None]:
    latest_row = conn.execute("SELECT MAX(trade_date) FROM stock_daily").fetchone()
    latest = latest_row[0] if latest_row else None
    if not latest:
        return None, None

    start_raw, end_raw = _value(qs, "start"), _value(qs, "end")
    start = start_raw if _YMD.fullmatch(start_raw) else None
    end = end_raw if _YMD.fullmatch(end_raw) else None
    if start or end:
        start, end = start or latest, end or latest
        if start > end:
            start, end = end, start
        return start, end

    days = _clamp_int(_value(qs, "days"), 20, 2, 730)
    row = conn.execute(
        "SELECT MIN(d), MAX(d) FROM ("
        "SELECT DISTINCT trade_date AS d FROM stock_daily "
        "WHERE close IS NOT NULL AND close > 0 "
        "ORDER BY trade_date DESC LIMIT ?) AS recent",
        (days,),
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def ranking(conn, qs) -> dict:
    """Return the best close-to-close performers in the requested date range."""
    limit = _clamp_int(_value(qs, "limit"), 100, 1, 100)
    start, end = _window(conn, qs)
    empty = {
        "start": start,
        "end": end,
        "trading_days": 0,
        "limit": limit,
        "price_basis": "unadjusted_close",
        "data": [],
    }
    if not start or not end:
        return empty

    trading_days = conn.execute(
        "SELECT COUNT(DISTINCT trade_date) FROM stock_daily "
        "WHERE trade_date BETWEEN ? AND ? AND close IS NOT NULL AND close > 0",
        (start, end),
    ).fetchone()[0]
    rows = conn.execute(
        "WITH valid AS ("
        " SELECT trade_date, stock_id, stock_name, close FROM stock_daily"
        " WHERE trade_date BETWEEN ? AND ? AND close IS NOT NULL AND close > 0"
        "), bounds AS ("
        " SELECT stock_id, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date,"
        " COUNT(*) AS observations, MAX(stock_name) AS stock_name"
        " FROM valid GROUP BY stock_id HAVING COUNT(*) >= 2 AND MIN(trade_date) < MAX(trade_date)"
        ")"
        " SELECT b.stock_id, b.stock_name, b.first_date, b.last_date, b.observations,"
        " first_bar.close, last_bar.close"
        " FROM bounds b"
        " JOIN valid first_bar ON first_bar.stock_id=b.stock_id"
        "  AND first_bar.trade_date=b.first_date"
        " JOIN valid last_bar ON last_bar.stock_id=b.stock_id"
        "  AND last_bar.trade_date=b.last_date",
        (start, end),
    ).fetchall()

    data = []
    for stock_id, name, first_date, last_date, observations, first_close, last_close in rows:
        first_close, last_close = float(first_close), float(last_close)
        data.append({
            "stock_id": stock_id,
            "stock_name": name or stock_id,
            "first_date": first_date,
            "last_date": last_date,
            "observations": int(observations),
            "first_close": first_close,
            "last_close": last_close,
            "return_pct": round((last_close / first_close - 1.0) * 100.0, 4),
        })
    data.sort(key=lambda row: (-row["return_pct"], row["stock_id"]))
    empty["trading_days"] = int(trading_days)
    empty["data"] = data[:limit]
    return empty
