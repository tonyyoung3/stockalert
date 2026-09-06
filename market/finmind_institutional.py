#!/usr/bin/env python3
"""FinMind institutional investors (投信／自營商) for T86 gap fill.

TWSE T86 is the daily source when GitHub Actions can reach it. Actions IPs
often get empty/HTML instead of JSON (`Expecting value: line 1 column 1`),
so historical trust_daily / dealer_daily gaps use FinMind:

  GET https://api.finmindtrade.com/api/v4/data
  dataset=TaiwanStockInstitutionalInvestorsBuySell
    (or TaiwanStockInstitutionalInvestorsBuySellWide)

Prefer **all-stocks by start_date** (one request per trade_date, no data_id).
That needs Backer/Sponsor. Free tier requires data_id (per-stock); pacing
that path for ~1,400 listed names is documented in PER_STOCK_PACING, not
used for a 400-day full-market fill.

Mapping (same 自營商 convention as T86 合計含避險):
  Investment_Trust → trust_daily
  Dealer + Dealer_self + Dealer_Hedging → dealer_daily
  Foreign_Investor is ignored here (foreign_daily already exists).

Reads FINMIND_TOKEN from env. Never log the token or put it in query strings.
Pace: FINMIND_SLEEP seconds (~3600/hr), under Sponsor ~6000/hr.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date
from typing import Iterable

import requests

from market.broker_branch import (
    FinMindError,
    finmind_headers,
    require_finmind_http,
    token_value,
)
from market.collector import T86Tables

log = logging.getLogger(__name__)

FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
DATASET_LONG = "TaiwanStockInstitutionalInvestorsBuySell"
DATASET_WIDE = "TaiwanStockInstitutionalInvestorsBuySellWide"
# Sponsor docs ~6000/hr. 1.0s keeps us ~3600/hr with retry headroom.
FINMIND_SLEEP = 1.0
HTTP_TIMEOUT = 60
HTTP_RETRIES = 4
HTTP_RETRY_STATUSES = {429, 500, 502, 503, 504}

TRUST_NAME = "Investment_Trust"
DEALER_NAMES = ("Dealer", "Dealer_self", "Dealer_Hedging")
# Long-format `name` values we keep. Others (Foreign_Investor, …) are ignored.
_KEEP_LONG = frozenset((TRUST_NAME, *DEALER_NAMES))

PER_STOCK_PACING = (
    "All-stocks-by-start_date needs Backer/Sponsor (no data_id). "
    "Free tier must pass data_id per stock: one request per (stock_id, date range). "
    f"Listed universe is ~1,400 names; at FINMIND_SLEEP={FINMIND_SLEEP}s "
    "(Sponsor ~6000/hr, this client ~3600/hr) a full-market day is ~25 min, "
    "and a 400-day gap fill is not practical per-stock. "
    "Use all-stocks (one request per trade_date) for institutional_gaps."
)


def _as_int(value) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def _name_for(stock_id: str, names: dict[str, str] | None) -> str:
    if names and stock_id in names:
        return names[stock_id]
    return ""


def map_long_rows(
    rows: Iterable[dict],
    day: date,
    names: dict[str, str] | None = None,
    listed_ids: set[str] | None = None,
) -> T86Tables:
    """Long FinMind rows → trust_daily / dealer_daily (foreign left empty).

    Each input row is one (stock_id, name) leg with buy/sell.
    Dealer total = Dealer + Dealer_self + Dealer_Hedging (T86 合計含避險).
    """
    trade_date = day.isoformat()
    trust_acc: dict[str, list[int]] = {}
    dealer_acc: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    seen: set[str] = set()

    for raw in rows or ():
        sid = str(raw.get("stock_id") or raw.get("data_id") or "").strip()
        if not sid:
            continue
        if listed_ids is not None and sid not in listed_ids:
            continue
        row_day = str(raw.get("date") or raw.get("trade_date") or "").strip()[:10]
        if row_day and row_day != trade_date:
            continue
        inst = str(raw.get("name") or "").strip()
        if inst not in _KEEP_LONG:
            continue
        buy = _as_int(raw.get("buy"))
        sell = _as_int(raw.get("sell"))
        seen.add(sid)
        if inst == TRUST_NAME:
            trust_acc[sid] = [buy, sell]
        else:
            dealer_acc[sid][0] += buy
            dealer_acc[sid][1] += sell

    trust = [
        (trade_date, sid, _name_for(sid, names), acc[0], acc[1], acc[0] - acc[1])
        for sid, acc in sorted(trust_acc.items())
    ]
    dealer = [
        (trade_date, sid, _name_for(sid, names), acc[0], acc[1], acc[0] - acc[1])
        for sid, acc in sorted(dealer_acc.items())
        if sid in seen
    ]
    return T86Tables([], trust, dealer)


def map_wide_rows(
    rows: Iterable[dict],
    day: date,
    names: dict[str, str] | None = None,
    listed_ids: set[str] | None = None,
) -> T86Tables:
    """Wide FinMind rows → trust_daily / dealer_daily (foreign left empty)."""
    trade_date = day.isoformat()
    trust, dealer = [], []
    for raw in rows or ():
        sid = str(raw.get("stock_id") or raw.get("data_id") or "").strip()
        if not sid:
            continue
        if listed_ids is not None and sid not in listed_ids:
            continue
        row_day = str(raw.get("date") or raw.get("trade_date") or "").strip()[:10]
        if row_day and row_day != trade_date:
            continue
        t_buy = _as_int(raw.get("Investment_Trust_buy"))
        t_sell = _as_int(raw.get("Investment_Trust_sell"))
        d_buy = (
            _as_int(raw.get("Dealer_buy"))
            + _as_int(raw.get("Dealer_self_buy"))
            + _as_int(raw.get("Dealer_Hedging_buy"))
        )
        d_sell = (
            _as_int(raw.get("Dealer_sell"))
            + _as_int(raw.get("Dealer_self_sell"))
            + _as_int(raw.get("Dealer_Hedging_sell"))
        )
        name = _name_for(sid, names)
        trust.append((trade_date, sid, name, t_buy, t_sell, t_buy - t_sell))
        dealer.append((trade_date, sid, name, d_buy, d_sell, d_buy - d_sell))
    return T86Tables([], trust, dealer)


def map_finmind_payload(
    payload: dict | list,
    day: date,
    names: dict[str, str] | None = None,
    listed_ids: set[str] | None = None,
) -> T86Tables:
    """Accept a FinMind v4 body or a bare row list (long or wide)."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("data") or []
    else:
        return T86Tables([], [], [])
    if not isinstance(rows, list) or not rows:
        return T86Tables([], [], [])
    sample = rows[0] if isinstance(rows[0], dict) else {}
    if "Investment_Trust_buy" in sample or "Dealer_self_buy" in sample:
        return map_wide_rows(rows, day, names=names, listed_ids=listed_ids)
    return map_long_rows(rows, day, names=names, listed_ids=listed_ids)


def _payload_status_ok(payload: dict) -> bool:
    status = payload.get("status")
    msg = str(payload.get("msg") or "").lower()
    if status in (None, 200):
        return True
    if msg in ("", "success"):
        return True
    return False


def _looks_like_need_data_id(payload: dict, status_code: int) -> bool:
    msg = str(payload.get("msg") or "").lower()
    if status_code in (400, 401, 403):
        if "data_id" in msg or "data id" in msg or "permission" in msg:
            return True
        if "token" in msg and "level" in msg:
            return True
    if "data_id" in msg and ("require" in msg or "need" in msg or "必須" in msg):
        return True
    return False


def fetch_institutional_day(
    day: date,
    token: str,
    *,
    dataset: str = DATASET_LONG,
    stock_id: str | None = None,
    session: requests.Session | None = None,
    sleep: callable = time.sleep,
) -> list[dict]:
    """One FinMind v4/data call. All-stocks when stock_id is omitted.

    Token is Bearer-only. Never log it. Caller must pace between dates.
    CLI / Actions only — dashboard request context refuses.
    """
    require_finmind_http()
    if not token:
        raise FinMindError("FINMIND_TOKEN missing; refusing to call FinMind")
    params = {
        "dataset": dataset,
        "start_date": day.isoformat(),
        "end_date": day.isoformat(),
    }
    if stock_id:
        params["data_id"] = stock_id
    sess = session or requests.Session()
    last_err: Exception | None = None
    label = f"dataset={dataset} date={day.isoformat()}"
    if stock_id:
        label += f" stock_id={stock_id}"
    for attempt in range(HTTP_RETRIES):
        try:
            resp = sess.get(
                FINMIND_DATA_URL,
                params=params,
                headers=finmind_headers(token),
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_err = exc
            log.warning(
                "FinMind institutional network error %s attempt=%s: %s",
                label,
                attempt + 1,
                type(exc).__name__,
            )
            sleep(2 ** attempt)
            continue
        if resp.status_code in (401, 403):
            raise FinMindError("FinMind auth failed (check FINMIND_TOKEN)")
        if resp.status_code in HTTP_RETRY_STATUSES:
            log.warning(
                "FinMind institutional HTTP %s %s attempt=%s",
                resp.status_code,
                label,
                attempt + 1,
            )
            sleep(2 ** attempt)
            continue
        try:
            payload = resp.json()
        except ValueError as exc:
            raise FinMindError(
                f"FinMind institutional invalid JSON {label}"
            ) from exc
        if not isinstance(payload, dict):
            raise FinMindError(f"FinMind institutional unexpected payload {label}")
        if resp.status_code >= 400 or not _payload_status_ok(payload):
            if _looks_like_need_data_id(payload, resp.status_code):
                raise FinMindError(
                    f"FinMind all-stocks denied {label}. {PER_STOCK_PACING}"
                )
            if resp.status_code >= 400:
                raise FinMindError(
                    f"FinMind institutional HTTP {resp.status_code} {label}"
                )
            raise FinMindError(
                f"FinMind institutional status={payload.get('status')} {label}"
            )
        data = payload.get("data") or []
        if not isinstance(data, list):
            raise FinMindError(f"FinMind institutional data is not a list {label}")
        return data
    raise FinMindError(
        f"FinMind institutional failed after retries {label} "
        f"({type(last_err).__name__ if last_err else 'http'})"
    )


def fetch_mapped_day(
    day: date,
    token: str,
    *,
    names: dict[str, str] | None = None,
    listed_ids: set[str] | None = None,
    session: requests.Session | None = None,
    sleep: callable = time.sleep,
    fetcher=None,
) -> T86Tables:
    """All-stocks long dataset first; Wide if long is empty. One day."""
    fetch = fetcher or (
        lambda d, ds: fetch_institutional_day(
            d, token, dataset=ds, session=session, sleep=sleep,
        )
    )
    rows = fetch(day, DATASET_LONG)
    tables = map_finmind_payload(
        rows, day, names=names, listed_ids=listed_ids,
    )
    if tables.trust or tables.dealer:
        return tables
    rows = fetch(day, DATASET_WIDE)
    return map_finmind_payload(
        rows, day, names=names, listed_ids=listed_ids,
    )


def stock_names_from_conn(conn) -> dict[str, str]:
    """stock_id → name from stocks, then any T86 / daily table that has names."""
    out: dict[str, str] = {}
    for sql in (
        "SELECT stock_id, stock_name FROM stocks",
        "SELECT stock_id, stock_name FROM foreign_daily",
        "SELECT stock_id, stock_name FROM stock_daily",
    ):
        try:
            for sid, name in conn.execute(sql):
                sid = str(sid or "").strip()
                name = str(name or "").strip()
                if sid and name and sid not in out:
                    out[sid] = name
        except Exception:
            continue
    return out


def listed_ids_from_conn(conn) -> set[str]:
    """Listed universe from local foreign_daily (T86 has no OTC)."""
    try:
        return {
            str(r[0]).strip()
            for r in conn.execute("SELECT DISTINCT stock_id FROM foreign_daily")
            if r and r[0]
        }
    except Exception:
        return set()
