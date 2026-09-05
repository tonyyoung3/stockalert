"""Hot-N 分點主力 metrics from broker_branch_daily (#98 / epic #76).

Query-time only. Reads the path-A 熱門前 N table already ingested by
``market.broker_branch``. No FinMind calls, no N expansion, no BSR.
Contract: docs/broker_main_force.md.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence

from market import broker_branch as broker_branch_mod
from web.chip_zscore import parse_tickers

DEFAULT_K = 5
MIN_K = 1
MAX_K = 50

TITLE = broker_branch_mod.TITLE_HOT_N  # 「熱門股分點動向」— never 全市場
KIND = "broker_main_force"
METRIC_FIELDS = (
    "buy_concentration",
    "sell_concentration",
    "lead_branch_net",
)

_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clamp_k(raw, default: int = DEFAULT_K) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    return max(MIN_K, min(n, MAX_K))


def _asof(raw) -> str | None:
    v = (raw or "").strip()
    return v if _YMD.fullmatch(v) else None


def parse_query(qs: Mapping) -> dict:
    """Parse dashboard-style ``{key: [str]}`` query params."""
    raw_tickers = []
    for key in ("tickers", "ticker"):
        for item in qs.get(key, []) or []:
            raw_tickers.append(item)
    return {
        "tickers": parse_tickers(raw_tickers),
        "k": _clamp_k((qs.get("k", [""])[0] or "").strip() or DEFAULT_K),
        "asof": _asof((qs.get("asof", [""])[0] or "")),
    }


def _round_ratio(value):
    if value is None:
        return None
    return round(float(value), 6)


def buy_concentration(nets: Sequence[int], k: int) -> float | None:
    """Top-K buy-side net+ / all buy-side net+. None when no buy-side."""
    buys = sorted((int(n) for n in nets if n is not None and int(n) > 0), reverse=True)
    total = sum(buys)
    if total <= 0:
        return None
    return sum(buys[: max(MIN_K, int(k))]) / total


def sell_concentration(nets: Sequence[int], k: int) -> float | None:
    """Top-K sell-side |net-| / all sell-side |net-|. None when no sell-side."""
    sells = sorted(
        (abs(int(n)) for n in nets if n is not None and int(n) < 0),
        reverse=True,
    )
    total = sum(sells)
    if total <= 0:
        return None
    return sum(sells[: max(MIN_K, int(k))]) / total


def lead_branch(rows: Sequence[Mapping]) -> dict | None:
    """Branch with max |net|. Tie-break: larger signed net, then broker_id."""
    best = None
    best_key = None
    for row in rows:
        net = row.get("net_volume")
        if net is None:
            continue
        net = int(net)
        broker_id = str(row.get("broker_id") or "")
        key = (abs(net), net, broker_id)
        if best_key is None or key > best_key:
            best_key = key
            best = {
                "lead_broker_id": broker_id or None,
                "lead_broker_name": row.get("broker_name") or broker_id or None,
                "lead_branch_net": net,
            }
    return best


def _resolve_asof(conn, asof: str | None) -> str | None:
    if asof:
        return asof
    return broker_branch_mod.latest_trade_date(conn)


def _stock_names(conn, tickers: Sequence[str]) -> dict[str, str | None]:
    names: dict[str, str | None] = {sid: None for sid in tickers}
    if not tickers:
        return names
    try:
        ph = ",".join("?" * len(tickers))
        for sid, name in conn.execute(
            f"SELECT stock_id, stock_name FROM stocks WHERE stock_id IN ({ph})",
            tuple(tickers),
        ):
            if name:
                names[str(sid)] = str(name)
    except sqlite3.OperationalError:
        pass
    missing = [sid for sid, name in names.items() if not name]
    if missing:
        try:
            ph = ",".join("?" * len(missing))
            for sid, name in conn.execute(
                f"SELECT stock_id, MAX(stock_name) FROM stock_daily "
                f"WHERE stock_id IN ({ph}) GROUP BY stock_id",
                tuple(missing),
            ):
                if name:
                    names[str(sid)] = str(name)
        except sqlite3.OperationalError:
            pass
    return names


def _fetch_day_rows(conn, tickers: Sequence[str], asof: str) -> list:
    if not tickers:
        return []
    ph = ",".join("?" * len(tickers))
    try:
        return conn.execute(
            "SELECT b.stock_id, b.broker_id, "
            "COALESCE(br.broker_name, b.broker_id), b.net_volume "
            "FROM broker_branch_daily b "
            "LEFT JOIN brokers br ON br.broker_id = b.broker_id "
            "WHERE b.trade_date = ? AND b.stock_id IN (" + ph + ")",
            (asof, *tickers),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _universe_count(conn, asof: str | None) -> int:
    if not asof:
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT stock_id) FROM broker_branch_daily "
            "WHERE trade_date = ?",
            (asof,),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0) if row else 0


def _empty_row(stock_id: str, stock_name, asof: str | None) -> dict:
    return {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "trade_date": None,
        "asof": asof,
        "in_hot_n": False,
        "branch_count": 0,
        "buy_side_sum": None,
        "sell_side_sum": None,
        "buy_top_k_sum": None,
        "sell_top_k_sum": None,
        "buy_concentration": None,
        "sell_concentration": None,
        "lead_broker_id": None,
        "lead_broker_name": None,
        "lead_branch_net": None,
    }


def _payload_shell(
    *,
    tickers: Sequence[str],
    k: int,
    asof: str | None,
    coverage: str,
    universe_count: int,
    env: dict[str, str] | None,
) -> dict:
    return {
        "kind": KIND,
        "not": "t86_foreign",
        "title": TITLE,
        "coverage": coverage,
        "coverage_note": (
            "加總範圍是已入庫的熱門前 N 檔，不是全市場。"
            if coverage == "hot_n"
            else "路徑 A 空狀態：熱門前 N 尚無列。標題是熱門股，不是全市場。"
        ),
        "path": broker_branch_mod.PATH,
        "slice_decision": broker_branch_mod.SLICE_DECISION,
        "k": k,
        "asof": asof,
        "hot_n": broker_branch_mod.configured_hot_n(env),
        "universe_count": universe_count,
        "fields": list(METRIC_FIELDS),
        "tickers": list(tickers),
        "data": [],
    }


def query_broker_main_force(
    conn,
    tickers: Sequence[str],
    *,
    k: int = DEFAULT_K,
    asof: str | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """One as-of snapshot per ticker from that day's hot-N ingest."""
    k = _clamp_k(k)
    asof = _resolve_asof(conn, asof)
    coverage = broker_branch_mod.coverage_for_rows(conn)
    # Path A never advertises full-market, even if the helper is later extended.
    if coverage == "full_market":
        coverage = "hot_n"
    universe_count = _universe_count(conn, asof)
    payload = _payload_shell(
        tickers=tickers,
        k=k,
        asof=asof,
        coverage=coverage,
        universe_count=universe_count,
        env=env,
    )
    if not tickers:
        payload["error"] = "missing_tickers"
        return payload
    if not asof:
        names = _stock_names(conn, tickers)
        payload["data"] = [_empty_row(sid, names.get(sid), None) for sid in tickers]
        return payload

    names = _stock_names(conn, tickers)
    raw = _fetch_day_rows(conn, tickers, asof)
    by_id: dict[str, list[dict]] = {sid: [] for sid in tickers}
    for stock_id, broker_id, broker_name, net in raw:
        by_id.setdefault(str(stock_id), []).append(
            {
                "broker_id": str(broker_id),
                "broker_name": broker_name,
                "net_volume": int(net) if net is not None else 0,
            }
        )

    data = []
    for sid in tickers:
        branches = by_id.get(sid) or []
        if not branches:
            data.append(_empty_row(sid, names.get(sid), asof))
            continue
        nets = [b["net_volume"] for b in branches]
        buys = [n for n in nets if n > 0]
        sells = [n for n in nets if n < 0]
        buy_total = sum(buys)
        sell_total = sum(abs(n) for n in sells)
        buy_top = sum(sorted(buys, reverse=True)[:k]) if buys else None
        sell_top = sum(sorted((abs(n) for n in sells), reverse=True)[:k]) if sells else None
        lead = lead_branch(branches) or {}
        data.append(
            {
                "stock_id": sid,
                "stock_name": names.get(sid),
                "trade_date": asof,
                "asof": asof,
                "in_hot_n": True,
                "branch_count": len(branches),
                "buy_side_sum": buy_total if buys else None,
                "sell_side_sum": sell_total if sells else None,
                "buy_top_k_sum": buy_top,
                "sell_top_k_sum": sell_top,
                "buy_concentration": _round_ratio(buy_concentration(nets, k)),
                "sell_concentration": _round_ratio(sell_concentration(nets, k)),
                "lead_broker_id": lead.get("lead_broker_id"),
                "lead_broker_name": lead.get("lead_broker_name"),
                "lead_branch_net": lead.get("lead_branch_net"),
            }
        )
    payload["data"] = data
    return payload


def api_broker_main_force(conn, qs: Mapping, env: dict[str, str] | None = None) -> dict:
    """Thin API helper for GET /api/scanner/broker_main_force."""
    parsed = parse_query(qs)
    return query_broker_main_force(
        conn,
        parsed["tickers"],
        k=parsed["k"],
        asof=parsed["asof"],
        env=env,
    )
