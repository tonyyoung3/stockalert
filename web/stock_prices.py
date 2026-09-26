"""Daily stock OHLCV, independent of institutional holdings."""
import math
import re
from datetime import timedelta

from web.tw_calendar import taiwan_today


def query_prices(conn, stock_id, days=90, today=None):
    if not re.fullmatch(r"[0-9A-Za-z]{2,10}", stock_id):
        return {"error": "invalid_stock_id", "data": []}
    days = max(1, min(int(days), 730))
    today = today or taiwan_today()
    rows = conn.execute(
        "SELECT trade_date, stock_name, open, high, low, close, volume "
        "FROM stock_daily WHERE stock_id=? AND trade_date>=? AND trade_date<=? "
        "ORDER BY trade_date",
        (stock_id, (today - timedelta(days=days)).isoformat(), today.isoformat()),
    ).fetchall()
    data = []
    name = stock_id
    for day, stock_name, o, h, lo, c, volume in rows:
        name = stock_name or name
        prices = (o, h, lo, c)
        if any(v is None or not math.isfinite(v) or v <= 0 for v in prices):
            continue
        if not lo <= min(o, c) <= max(o, c) <= h:
            continue
        data.append({"date": day, "open": o, "high": h, "low": lo,
                     "close": c, "volume": volume})
    summary = None
    if data:
        latest = data[-1]
        prior = conn.execute(
            "SELECT close FROM stock_daily WHERE stock_id=? AND trade_date<? "
            "AND close>0 ORDER BY trade_date DESC LIMIT 1",
            (stock_id, latest["date"]),
        ).fetchone()
        previous = prior[0] if prior and math.isfinite(prior[0]) else None
        change = latest["close"] - previous if previous else None
        summary = {**latest, "previous_close": previous, "change": change,
                   "change_pct": change / previous * 100 if previous else None}
    return {"id": stock_id, "name": name, "days": days, "data": data,
            "summary": summary, "price_mode": "daily_close", "volume_unit": "shares"}
