"""Configurable-window chip z-scores on stock_chips_daily (#78 / epic #76).

Query-time only (no extra table or VIEW). Per stock_id, last ``window``
trading days ending at ``asof``. Sample stddev (ddof=1). Contract:
docs/chip_zscore.md.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence

DEFAULT_WINDOW = 20
MIN_WINDOW = 2
MAX_WINDOW = 252
MAX_TICKERS = 200
DDOF = 1

STOCK_ID = re.compile(r"^[0-9A-Za-z]{2,10}$")
YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Scanner ranking uses the three nets. Buy/sell z-scores are the same math
# on the natural T86 legs (shares).
NET_FIELDS = ("foreign_net", "trust_net", "dealer_net")
BUY_SELL_FIELDS = (
    "foreign_buy",
    "foreign_sell",
    "trust_buy",
    "trust_sell",
    "dealer_buy",
    "dealer_sell",
)
Z_FIELDS = NET_FIELDS + BUY_SELL_FIELDS

_PRICE_FIELDS = ("close", "volume", "turnover")
_ROW_FIELDS = ("trade_date", "stock_id", "stock_name") + _PRICE_FIELDS + Z_FIELDS


def zscore(value, window_values: Sequence, *, min_periods: int, ddof: int = DDOF):
    """Z-score of ``value`` vs ``window_values`` (NULLs skipped).

    Sample stddev when ``ddof=1``. Returns None when:
    - ``value`` is None
    - non-null count < min_periods or < ddof+1
    - stddev is 0 (constant series) or not finite
    """
    if value is None:
        return None
    xs = [float(x) for x in window_values if x is not None]
    n = len(xs)
    need = max(int(min_periods), ddof + 1)
    if n < need:
        return None
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - ddof)
    if var <= 0 or not math.isfinite(var):
        return None
    std = math.sqrt(var)
    if std == 0 or not math.isfinite(std):
        return None
    z = (float(value) - mean) / std
    if not math.isfinite(z):
        return None
    return z


def parse_tickers(raw_values: Sequence[str] | None) -> list[str]:
    """Split comma/whitespace lists, validate, de-dupe (request order)."""
    out: list[str] = []
    seen: set[str] = set()
    for chunk in raw_values or ():
        for token in re.split(r"[\s,]+", str(chunk).strip()):
            if not token:
                continue
            sid = token.strip()
            if not STOCK_ID.fullmatch(sid) or sid in seen:
                continue
            seen.add(sid)
            out.append(sid)
            if len(out) >= MAX_TICKERS:
                return out
    return out


def _clamp_int(raw, default: int, lo: int, hi: int) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(n, hi))


def _asof(raw) -> str | None:
    v = (raw or "").strip()
    return v if YMD.fullmatch(v) else None


def parse_query(qs: Mapping) -> dict:
    """Parse dashboard-style ``{key: [str]}`` query params."""
    raw_tickers = []
    for key in ("tickers", "ticker"):
        for item in qs.get(key, []) or []:
            raw_tickers.append(item)
    tickers = parse_tickers(raw_tickers)
    window = _clamp_int(
        (qs.get("window", [""])[0] or "").strip() or DEFAULT_WINDOW,
        DEFAULT_WINDOW,
        MIN_WINDOW,
        MAX_WINDOW,
    )
    min_raw = (qs.get("min_periods", [""])[0] or "").strip()
    if min_raw:
        min_periods = _clamp_int(min_raw, window, MIN_WINDOW, window)
    else:
        min_periods = window
    asof = _asof((qs.get("asof", [""])[0] or ""))
    return {
        "tickers": tickers,
        "window": window,
        "min_periods": min_periods,
        "asof": asof,
    }


def _resolve_asof(conn, tickers: Sequence[str], asof: str | None) -> str | None:
    if asof:
        return asof
    if tickers:
        ph = ",".join("?" * len(tickers))
        row = conn.execute(
            f"SELECT MAX(trade_date) FROM stock_chips_daily WHERE stock_id IN ({ph})",
            tuple(tickers),
        ).fetchone()
    else:
        row = conn.execute("SELECT MAX(trade_date) FROM stock_chips_daily").fetchone()
    return row[0] if row else None


def _fetch_windows(conn, tickers: Sequence[str], asof: str, window: int) -> list:
    """Last ``window`` stock_chips_daily rows per ticker with trade_date <= asof."""
    ph = ",".join("?" * len(tickers))
    cols = ", ".join(_ROW_FIELDS)
    sql = (
        f"WITH ranked AS ("
        f" SELECT {cols},"
        f" ROW_NUMBER() OVER (PARTITION BY stock_id ORDER BY trade_date DESC) AS rn"
        f" FROM stock_chips_daily"
        f" WHERE stock_id IN ({ph}) AND trade_date <= ?"
        f") SELECT {cols} FROM ranked WHERE rn <= ? ORDER BY stock_id, trade_date"
    )
    return conn.execute(sql, (*tickers, asof, window)).fetchall()


def _empty_row(stock_id: str, asof: str | None, sample_count: int, insufficient: bool) -> dict:
    body = {
        "stock_id": stock_id,
        "stock_name": None,
        "trade_date": None,
        "asof": asof,
        "close": None,
        "volume": None,
        "turnover": None,
        "sample_count": sample_count,
        "insufficient_sample": insufficient,
    }
    for field in Z_FIELDS:
        body[field] = None
        body[f"{field}_z"] = None
        body[f"{field}_n"] = 0
    return body


def _round_z(z):
    if z is None:
        return None
    return round(z, 6)


def query_chip_zscore(
    conn,
    tickers: Sequence[str],
    *,
    window: int = DEFAULT_WINDOW,
    min_periods: int | None = None,
    asof: str | None = None,
) -> dict:
    """One as-of snapshot per ticker. See docs/chip_zscore.md."""
    window = _clamp_int(window, DEFAULT_WINDOW, MIN_WINDOW, MAX_WINDOW)
    if min_periods is None:
        min_periods = window
    else:
        min_periods = _clamp_int(min_periods, window, MIN_WINDOW, window)
    asof = _resolve_asof(conn, tickers, asof)
    payload = {
        "window": window,
        "min_periods": min_periods,
        "asof": asof,
        "ddof": DDOF,
        "fields": list(Z_FIELDS),
        "tickers": list(tickers),
        "data": [],
    }
    if not tickers:
        payload["error"] = "missing_tickers"
        return payload
    if not asof:
        payload["data"] = [
            _empty_row(sid, None, 0, True) for sid in tickers
        ]
        return payload

    rows = _fetch_windows(conn, tickers, asof, window)
    by_id: dict[str, list] = {sid: [] for sid in tickers}
    col = {name: i for i, name in enumerate(_ROW_FIELDS)}
    for row in rows:
        by_id.setdefault(row[col["stock_id"]], []).append(row)

    data = []
    for sid in tickers:
        series = by_id.get(sid) or []
        sample_count = len(series)
        insufficient = sample_count < window or sample_count < min_periods
        if not series:
            data.append(_empty_row(sid, asof, 0, True))
            continue
        last = series[-1]
        item = {
            "stock_id": sid,
            "stock_name": last[col["stock_name"]],
            "trade_date": last[col["trade_date"]],
            "asof": asof,
            "close": last[col["close"]],
            "volume": last[col["volume"]],
            "turnover": last[col["turnover"]],
            "sample_count": sample_count,
            "insufficient_sample": insufficient,
        }
        for field in Z_FIELDS:
            window_vals = [r[col[field]] for r in series]
            item[field] = last[col[field]]
            item[f"{field}_n"] = sum(v is not None for v in window_vals)
            item[f"{field}_z"] = _round_z(
                zscore(
                    last[col[field]],
                    window_vals,
                    min_periods=min_periods,
                    ddof=DDOF,
                )
            )
        data.append(item)
    payload["data"] = data
    return payload


def api_chip_zscore(conn, qs: Mapping) -> dict:
    """Thin API helper for GET /api/scanner/chip_zscore."""
    parsed = parse_query(qs)
    return query_chip_zscore(
        conn,
        parsed["tickers"],
        window=parsed["window"],
        min_periods=parsed["min_periods"],
        asof=parsed["asof"],
    )
