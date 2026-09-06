#!/usr/bin/env python3
"""Broker-branch (分點買賣超) — path A scheduled ingest + read APIs.

Path A (#61 / #54 / #53 / #108): 熱門前 N powers market Top; same tables
for single-stock reads. Titles say 熱門股, never 全市場.

**Actions write, service reads.** FinMind HTTP runs only from
`python -m market.broker_branch ingest` (GitHub Actions /
``.github/workflows/update_broker_branch.yml``) or a scheduled CLI.
Dashboard / API request handlers never call FinMind. Empty tables stay
honest empty + freshness. ``ingest_configured`` means this process has
FINMIND_TOKEN for *scheduled* ingest — not website live fetch.

Fixture load is TEST/DEV only. See docs/broker_branch.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path

import requests

from data.paths import repo_file
from data.sqlite_util import configure_local
from web import freshness as freshness_mod

log = logging.getLogger(__name__)

DATASET = "TaiwanStockTradingDailyReportSecIdAgg"
FINMIND_SECID_AGG_URL = (
    "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report_secid_agg"
)
DEFAULT_HOT_N = 80
DEFAULT_K = 15
HOT_N_MAX = 500
EXPECTED_AFTER_HOUR = 21  # Asia/Taipei; FinMind SecIdAgg docs, not T86 16:00
HTTP_TIMEOUT = 45
HTTP_RETRIES = 4
HTTP_RETRY_STATUSES = {429, 500, 502, 503, 504}

TITLE_HOT_N = "熱門股分點動向"
TITLE_FULL_MARKET = "全市場分點買賣超"
TITLE_STOCK = "個股分點買賣超"

DATA_MODE_EMPTY = "empty"
DATA_MODE_FIXTURE = "dev_fixture"
DATA_MODE_LIVE = "live"
PATH = "A"
SLICE_DECISION = "hot_n"
SOURCE_LIVE = "live"
SOURCE_FIXTURE = "dev_fixture"

BLOCKER = (
    "FINMIND_TOKEN absent from this process. "
    "Scheduled ingest (GitHub Actions / `python -m market.broker_branch ingest`) "
    "needs the secret. Dashboard/API never call FinMind. "
    "Path A is locked (熱門前 N market Top; same tables for stock reads)."
)
REQUEST_TIME_REFUSAL = (
    "FinMind HTTP is not allowed on dashboard/API request. "
    "Actions write; service reads. Use `python -m market.broker_branch ingest`."
)

# Default True so CLI ingest / unit tests work. dashboard.api() sets False
# for the request ContextVar so Path B / request-time fetch cannot run.
_finmind_http_allowed: ContextVar[bool] = ContextVar(
    "finmind_http_allowed", default=True
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS broker_branch_daily (
    trade_date  TEXT,
    stock_id    TEXT,
    broker_id   TEXT,
    buy_volume  INTEGER,
    sell_volume INTEGER,
    net_volume  INTEGER,
    PRIMARY KEY (trade_date, stock_id, broker_id)
);
CREATE TABLE IF NOT EXISTS brokers (
    broker_id   TEXT PRIMARY KEY,
    broker_name TEXT
);
CREATE TABLE IF NOT EXISTS broker_branch_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_broker_branch_date_net
    ON broker_branch_daily(trade_date, net_volume);
CREATE INDEX IF NOT EXISTS idx_broker_branch_stock_date
    ON broker_branch_daily(stock_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_broker_branch_broker_date
    ON broker_branch_daily(broker_id, trade_date);
"""

DEFAULT_FIXTURE = repo_file("tests", "fixtures", "broker_branch_sample.json")

_DOTENV_LOADED = False


class FinMindError(RuntimeError):
    """FinMind client error. Message must never include the token."""


def _ensure_dotenv() -> None:
    """Load local .env once so FINMIND_TOKEN works outside GitHub Actions."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass


def _env(env: dict[str, str] | None) -> dict[str, str]:
    if env is None:
        _ensure_dotenv()
        return os.environ
    return env


def token_value(env: dict[str, str] | None = None) -> str:
    """Return the token string, or '' . Callers must not log the return value."""
    return (_env(env).get("FINMIND_TOKEN") or "").strip()


def token_present(env: dict[str, str] | None = None) -> bool:
    return bool(token_value(env))


def finmind_http_allowed() -> bool:
    """False inside dashboard.api(); True for CLI ingest / tests."""
    return bool(_finmind_http_allowed.get())


def forbid_request_time_finmind():
    """Disable FinMind HTTP for this request context. Returns a reset token."""
    return _finmind_http_allowed.set(False)


def reset_request_time_finmind(token) -> None:
    _finmind_http_allowed.reset(token)


def require_finmind_http() -> None:
    if not finmind_http_allowed():
        raise FinMindError(REQUEST_TIME_REFUSAL)


def configured_hot_n(env: dict[str, str] | None = None) -> int:
    raw = (_env(env).get("BROKER_BRANCH_HOT_N") or "").strip()
    if not raw:
        return DEFAULT_HOT_N
    try:
        return max(1, min(int(raw), HOT_N_MAX))
    except ValueError:
        return DEFAULT_HOT_N


def market_title(coverage: str) -> str:
    """UI/API title. 全市場 is allowed only for true full-market coverage."""
    if coverage == "full_market":
        return TITLE_FULL_MARKET
    if coverage == "single_stock":
        return TITLE_STOCK
    return TITLE_HOT_N


def ingest_status(env: dict[str, str] | None = None) -> dict:
    """Token / path metadata. Does not fetch. ingest_configured ≠ website live."""
    present = token_present(env)
    return {
        "kind": "broker_branch",
        "not": "t86_foreign",
        "dataset": DATASET,
        "token_present": present,
        "ingest_configured": present,
        "path": PATH,
        "slice_decision": SLICE_DECISION,
        "blocker": None if present else BLOCKER,
        "hot_n": configured_hot_n(env),
        "hot_n_default": DEFAULT_HOT_N,
        "expected_after_hour": EXPECTED_AFTER_HOUR,
        "writes": "actions_or_cli",
        "reads": "db",
    }


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def _table_ready(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1 FROM broker_branch_daily LIMIT 0")
        return True
    except sqlite3.OperationalError:
        return False


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM broker_branch_meta WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or row[0] is None:
        return None
    return str(row[0])


def _meta_set(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    ensure_schema(conn)
    conn.executemany(
        "INSERT OR REPLACE INTO broker_branch_meta (key, value) VALUES (?, ?)",
        [(str(k), str(v)) for k, v in values.items()],
    )
    conn.commit()


def latest_trade_date(conn: sqlite3.Connection) -> str | None:
    if not _table_ready(conn):
        return None
    row = conn.execute("SELECT MAX(trade_date) FROM broker_branch_daily").fetchone()
    if not row or not row[0]:
        return None
    return str(row[0])


def latest_stock_daily_date(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT MAX(trade_date) FROM stock_daily").fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    return str(row[0])


def stock_daily_date_on_or_before(
    conn: sqlite3.Connection, trade_date: str
) -> str | None:
    try:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM stock_daily WHERE trade_date <= ?",
            (trade_date,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    return str(row[0])


def select_hot_n(
    conn: sqlite3.Connection,
    n: int = DEFAULT_HOT_N,
    trade_date: str | None = None,
) -> list[tuple[str, str | None, int]]:
    """Top-N stock_ids by turnover on a stock_daily trade_date.

    This is the documented 熱門前 N slice — not full-market coverage.
    `trade_date` defaults to the latest row in stock_daily (the 18:00 catch-up).
    """
    n = max(1, int(n))
    day = trade_date or latest_stock_daily_date(conn)
    if not day:
        return []
    try:
        rows = conn.execute(
            "SELECT stock_id, stock_name, COALESCE(turnover, 0) "
            "FROM stock_daily WHERE trade_date = ? "
            "ORDER BY COALESCE(turnover, 0) DESC, stock_id LIMIT ?",
            (day, n),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(str(r[0]), r[1], int(r[2] or 0)) for r in rows]


def row_count(conn: sqlite3.Connection) -> int:
    if not _table_ready(conn):
        return 0
    return int(conn.execute("SELECT COUNT(*) FROM broker_branch_daily").fetchone()[0])


def data_mode(conn: sqlite3.Connection, env: dict[str, str] | None = None) -> str:
    """How the current rows were produced.

    Fixture vs live is stored in broker_branch_meta.source so Cloud Run
    (which has Turso rows but usually no FINMIND_TOKEN) does not label
    production ingest as 示範 fixture.
    """
    if not row_count(conn):
        # Empty is empty. Cloud Run usually has no FINMIND_TOKEN; that does
        # not mean the website is "awaiting token" to live-fetch.
        return DATA_MODE_EMPTY
    source = _meta_get(conn, "source")
    if source == SOURCE_LIVE:
        return DATA_MODE_LIVE
    if source == SOURCE_FIXTURE:
        return DATA_MODE_FIXTURE
    return DATA_MODE_LIVE if token_present(env) else DATA_MODE_FIXTURE


def coverage_for_rows(conn: sqlite3.Connection) -> str:
    if not row_count(conn):
        return "empty"
    # Fixture and hot-N ingest are not full-market. Do not guess 全市場.
    return "hot_n"


def freshness_payload(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    tw = freshness_mod.taiwan_now(now)
    today = tw.date()
    expected = freshness_mod.expected_tw_trade_date(now, after_hour=EXPECTED_AFTER_HOUR)
    last = latest_trade_date(conn)
    status = freshness_mod.table_status("broker_branch_daily", last, today, expected)
    status["expected_trade_date"] = expected.isoformat()
    status["expected_after_hour"] = EXPECTED_AFTER_HOUR
    status["note"] = (
        "分點 freshness 用 21:00 台灣時間，不併入 /api/freshness 的 T86 16:00 關鍵表。"
    )
    return status


def _envelope(
    conn: sqlite3.Connection,
    *,
    coverage: str,
    trade_date: str | None,
    extra: dict,
    env: dict[str, str] | None = None,
) -> dict:
    status = ingest_status(env)
    mode = data_mode(conn, env)
    slice_day = _meta_get(conn, "slice_trade_date") or latest_stock_daily_date(conn)
    if coverage == "hot_n":
        note = "加總範圍是已入庫的熱門前 N 檔，不是全市場。網站只讀資料庫，不即時拉 FinMind。"
    elif coverage == "single_stock":
        note = "該檔讀已入庫熱門前 N 列，不是全市場、也不是 on-demand 拉檔。網站只讀資料庫。"
    else:
        note = (
            "路徑 A 空狀態：熱門前 N 尚無入庫列。網站只讀資料庫，不即時拉 FinMind。"
            "標題是熱門股，不是全市場。"
        )
    body = {
        **status,
        "title": market_title(coverage),
        "coverage": coverage,
        "coverage_note": note,
        "trade_date": trade_date,
        "slice_trade_date": slice_day,
        "data_mode": mode,
        "freshness": freshness_payload(conn),
    }
    if mode == DATA_MODE_FIXTURE:
        body["fixture_warning"] = (
            "Rows are a TEST/DEV fixture (or local upsert). "
            "Not a production FinMind feed. Do not merge as live data."
        )
    body.update(extra)
    return body


def ranking_window(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
    days: int | None = None,
) -> tuple[str | None, str | None, int]:
    """Inclusive (start, end, trading_days) for market Top.

    No date + no rows → (None, None, 0). days unset or 1 → latest (or given) day.
    days>1 → last N distinct trade_dates ending at that day (foreign-ranking habit).
    """
    end = trade_date or latest_trade_date(conn)
    if not end or not _table_ready(conn):
        return None, None, 0
    n = 1 if days is None else max(1, min(int(days), 730))
    if n == 1:
        return end, end, 1
    span = conn.execute(
        "SELECT MIN(d), MAX(d), COUNT(*) FROM ("
        "SELECT DISTINCT trade_date AS d FROM broker_branch_daily "
        "WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?) AS t",
        (end, n),
    ).fetchone()
    if not span or not span[0]:
        return end, end, 0
    return str(span[0]), str(span[1]), int(span[2])


def top_branches(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
    k: int = DEFAULT_K,
    env: dict[str, str] | None = None,
    days: int | None = None,
) -> dict:
    k = max(1, min(int(k), 50))
    start, end, n_days = ranking_window(conn, trade_date, days)
    coverage = coverage_for_rows(conn) if end else "empty"
    buy: list = []
    sell: list = []
    universe = 0
    if end and _table_ready(conn):
        buy = conn.execute(
            "SELECT b.broker_id, COALESCE(MAX(br.broker_name), b.broker_id), "
            "SUM(b.net_volume) AS net "
            "FROM broker_branch_daily b "
            "LEFT JOIN brokers br ON br.broker_id = b.broker_id "
            "WHERE b.trade_date BETWEEN ? AND ? "
            "GROUP BY b.broker_id "
            "ORDER BY net DESC LIMIT ?",
            (start, end, k),
        ).fetchall()
        sell = conn.execute(
            "SELECT b.broker_id, COALESCE(MAX(br.broker_name), b.broker_id), "
            "SUM(b.net_volume) AS net "
            "FROM broker_branch_daily b "
            "LEFT JOIN brokers br ON br.broker_id = b.broker_id "
            "WHERE b.trade_date BETWEEN ? AND ? "
            "GROUP BY b.broker_id "
            "ORDER BY net ASC LIMIT ?",
            (start, end, k),
        ).fetchall()
        universe = int(
            conn.execute(
                "SELECT COUNT(DISTINCT stock_id) FROM broker_branch_daily "
                "WHERE trade_date BETWEEN ? AND ?",
                (start, end),
            ).fetchone()[0]
        )
    return _envelope(
        conn,
        coverage=coverage,
        trade_date=end,
        env=env,
        extra={
            "k": k,
            "days": 1 if days is None else max(1, min(int(days), 730)),
            "start": start,
            "end": end,
            "trading_days": n_days,
            "hot_n": universe or configured_hot_n(env),
            "universe_count": universe,
            "slice_method": (
                "stock_daily latest trade_date, ORDER BY turnover DESC LIMIT N"
            ),
            "buy": [list(r) for r in buy],
            "sell": [list(r) for r in sell],
        },
    )


def broker_stocks(
    conn: sqlite3.Connection,
    broker_id: str,
    trade_date: str | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    broker_id = (broker_id or "").strip()
    day = trade_date or latest_trade_date(conn)
    coverage = coverage_for_rows(conn) if day else "empty"
    rows: list = []
    name = broker_id
    if day and broker_id and _table_ready(conn):
        got = conn.execute(
            "SELECT broker_name FROM brokers WHERE broker_id = ?", (broker_id,)
        ).fetchone()
        if got and got[0]:
            name = got[0]
        try:
            rows = conn.execute(
                "SELECT b.stock_id, COALESCE(s.stock_name, b.stock_id), "
                "b.buy_volume, b.sell_volume, b.net_volume "
                "FROM broker_branch_daily b "
                "LEFT JOIN stocks s ON s.stock_id = b.stock_id "
                "WHERE b.broker_id = ? AND b.trade_date = ? "
                "ORDER BY b.net_volume DESC",
                (broker_id, day),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT stock_id, stock_id, buy_volume, sell_volume, net_volume "
                "FROM broker_branch_daily "
                "WHERE broker_id = ? AND trade_date = ? "
                "ORDER BY net_volume DESC",
                (broker_id, day),
            ).fetchall()
    return _envelope(
        conn,
        coverage=coverage,
        trade_date=day,
        env=env,
        extra={
            "broker_id": broker_id,
            "broker_name": name,
            "data": [list(r) for r in rows],
        },
    )


def stock_branches(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """v1.1 stock-page ranking. Same tables; does not block market-tab work."""
    stock_id = (stock_id or "").strip()
    day = trade_date or latest_trade_date(conn)
    coverage = "single_stock" if day and stock_id else "empty"
    rows: list = []
    name = stock_id
    if day and stock_id and _table_ready(conn):
        try:
            got = conn.execute(
                "SELECT stock_name FROM stocks WHERE stock_id = ?", (stock_id,)
            ).fetchone()
            if got and got[0]:
                name = got[0]
        except sqlite3.OperationalError:
            pass
        rows = conn.execute(
            "SELECT b.broker_id, COALESCE(br.broker_name, b.broker_id), "
            "b.buy_volume, b.sell_volume, b.net_volume "
            "FROM broker_branch_daily b "
            "LEFT JOIN brokers br ON br.broker_id = b.broker_id "
            "WHERE b.stock_id = ? AND b.trade_date = ? "
            "ORDER BY b.net_volume DESC",
            (stock_id, day),
        ).fetchall()
    ranked = [[r[0], r[1], int(r[4] or 0)] for r in rows]
    buy = [r for r in ranked if r[2] > 0]
    sell = sorted((r for r in ranked if r[2] < 0), key=lambda r: r[2])
    return _envelope(
        conn,
        coverage=coverage,
        trade_date=day,
        env=env,
        extra={
            "stock_id": stock_id,
            "stock_name": name,
            "data": [list(r) for r in rows],
            "buy": buy,
            "sell": sell,
        },
    )


def load_fixture(
    conn: sqlite3.Connection,
    path: Path | str | None = None,
    *,
    dev: bool = False,
) -> dict:
    """Upsert the checked-in sample. Refuse unless dev=True.

    Not a production ingest path. Do not push these rows to Turso as live data.
    """
    if not dev:
        raise RuntimeError(
            "Refusing to load broker-branch fixture without dev=True / --dev. "
            "This file is TEST/DEV only and must not be treated as production."
        )
    path = Path(path) if path is not None else DEFAULT_FIXTURE
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("production"):
        raise RuntimeError("fixture must not set production=true")
    ensure_schema(conn)
    brokers = payload.get("brokers") or []
    rows = payload.get("rows") or []
    conn.executemany(
        "INSERT OR REPLACE INTO brokers (broker_id, broker_name) VALUES (?, ?)",
        [(str(b["broker_id"]), str(b.get("broker_name") or b["broker_id"])) for b in brokers],
    )
    upsert = []
    for row in rows:
        buy = int(row["buy_volume"])
        sell = int(row["sell_volume"])
        net = row.get("net_volume")
        if net is None:
            net = buy - sell
        upsert.append(
            (
                str(row["trade_date"]),
                str(row["stock_id"]),
                str(row["broker_id"]),
                buy,
                sell,
                int(net),
            )
        )
    conn.executemany(
        "INSERT OR REPLACE INTO broker_branch_daily "
        "(trade_date, stock_id, broker_id, buy_volume, sell_volume, net_volume) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        upsert,
    )
    day = str(payload.get("trade_date") or (upsert[0][0] if upsert else ""))
    _meta_set(
        conn,
        {
            "source": SOURCE_FIXTURE,
            "slice_trade_date": day,
            "ingest_trade_date": day,
            "hot_n": str(payload.get("hot_n") or 0),
        },
    )
    log.info("loaded TEST/DEV broker-branch fixture %s (%d rows)", path, len(upsert))
    return {
        "path": str(path),
        "brokers": len(brokers),
        "rows": len(upsert),
        "data_mode": DATA_MODE_FIXTURE,
        "ingest_configured": False,
        "production": False,
    }


def _as_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def map_secid_agg_row(raw: dict) -> tuple | None:
    """Map one FinMind SecIdAgg row. Drops buy_price / sell_price."""
    broker_id = str(raw.get("securities_trader_id") or "").strip()
    stock_id = str(raw.get("stock_id") or raw.get("data_id") or "").strip()
    day = str(raw.get("date") or raw.get("trade_date") or "").strip()[:10]
    if not broker_id or not stock_id or not day:
        return None
    buy = _as_int(raw.get("buy_volume"))
    sell = _as_int(raw.get("sell_volume"))
    return (day, stock_id, broker_id, buy, sell, buy - sell)


def finmind_headers(token: str) -> dict[str, str]:
    """Authorization only. Never put the token in the query string."""
    return {"Authorization": f"Bearer {token}"}


def fetch_secid_agg(
    stock_id: str,
    start_date: str,
    end_date: str,
    token: str,
    *,
    session: requests.Session | None = None,
    sleep: callable = time.sleep,
) -> list[dict]:
    """GET SecIdAgg for one stock_id. Token is Bearer-only, never logged.

    CLI / Actions only. Dashboard request context raises REQUEST_TIME_REFUSAL.
    """
    require_finmind_http()
    if not token:
        raise FinMindError("FINMIND_TOKEN missing; refusing to call FinMind")
    params = {
        "data_id": stock_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    sess = session or requests.Session()
    last_err: Exception | None = None
    for attempt in range(HTTP_RETRIES):
        try:
            resp = sess.get(
                FINMIND_SECID_AGG_URL,
                params=params,
                headers=finmind_headers(token),
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_err = exc
            log.warning(
                "FinMind SecIdAgg network error stock_id=%s attempt=%s: %s",
                stock_id,
                attempt + 1,
                type(exc).__name__,
            )
            sleep(2 ** attempt)
            continue
        if resp.status_code in (401, 403):
            raise FinMindError("FinMind auth failed (check FINMIND_TOKEN)")
        if resp.status_code in HTTP_RETRY_STATUSES:
            log.warning(
                "FinMind SecIdAgg HTTP %s stock_id=%s attempt=%s",
                resp.status_code,
                stock_id,
                attempt + 1,
            )
            sleep(2 ** attempt)
            continue
        if resp.status_code >= 400:
            raise FinMindError(
                f"FinMind SecIdAgg HTTP {resp.status_code} stock_id={stock_id}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise FinMindError(
                f"FinMind SecIdAgg invalid JSON stock_id={stock_id}"
            ) from exc
        if not isinstance(payload, dict):
            raise FinMindError(
                f"FinMind SecIdAgg unexpected payload stock_id={stock_id}"
            )
        status = payload.get("status")
        msg = str(payload.get("msg") or "").lower()
        if status not in (None, 200) and msg not in ("", "success"):
            raise FinMindError(
                f"FinMind SecIdAgg status={status} stock_id={stock_id}"
            )
        data = payload.get("data") or []
        if not isinstance(data, list):
            raise FinMindError(
                f"FinMind SecIdAgg data is not a list stock_id={stock_id}"
            )
        return data
    raise FinMindError(
        f"FinMind SecIdAgg failed after retries stock_id={stock_id} "
        f"({type(last_err).__name__ if last_err else 'http'})"
    )


def upsert_live_rows(
    conn: sqlite3.Connection,
    mapped: list[tuple],
    brokers: list[tuple[str, str]],
) -> int:
    """Write day aggregates. Does not accept fixture provenance."""
    if not mapped:
        return 0
    ensure_schema(conn)
    if brokers:
        conn.executemany(
            "INSERT OR REPLACE INTO brokers (broker_id, broker_name) VALUES (?, ?)",
            brokers,
        )
    conn.executemany(
        "INSERT OR REPLACE INTO broker_branch_daily "
        "(trade_date, stock_id, broker_id, buy_volume, sell_volume, net_volume) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        mapped,
    )
    conn.commit()
    return len(mapped)


def _ingest_dates(conn: sqlite3.Connection, end: str, days: int) -> list[str]:
    """Trading days to ingest, newest first. days=1 is just `end`."""
    if days <= 1:
        return [end]
    try:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM stock_daily "
            "WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?",
            (end, days),
        ).fetchall()
    except sqlite3.OperationalError:
        return [end]
    dates = [str(r[0]) for r in rows if r and r[0]]
    return dates or [end]


def resolve_ingest_date(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
    now: datetime | None = None,
) -> str:
    if trade_date:
        return trade_date
    expected = freshness_mod.expected_tw_trade_date(now, after_hour=EXPECTED_AFTER_HOUR)
    latest_sd = latest_stock_daily_date(conn)
    if latest_sd and latest_sd > expected.isoformat():
        return latest_sd
    return expected.isoformat()


def ingest_hot_n(
    conn: sqlite3.Connection,
    *,
    trade_date: str | None = None,
    n: int | None = None,
    days: int = 1,
    env: dict[str, str] | None = None,
    fetcher=None,
    now: datetime | None = None,
) -> dict:
    """Path A scheduled ingest: hot-N from stock_daily.turnover, then SecIdAgg.

    CLI / Actions only. Does not load the TEST/DEV fixture.
    Refuses without FINMIND_TOKEN and refuses on dashboard/API request.
    """
    require_finmind_http()
    token = token_value(env)
    if not token:
        raise FinMindError(
            "FINMIND_TOKEN missing; path A scheduled ingest will not call FinMind"
        )
    n = configured_hot_n(env) if n is None else max(1, min(int(n), HOT_N_MAX))
    days = max(1, min(int(days), 730))
    end = resolve_ingest_date(conn, trade_date, now)
    fetch = fetcher or (
        lambda sid, start, stop: fetch_secid_agg(sid, start, stop, token)
    )
    ensure_schema(conn)

    days_done: list[dict] = []
    failed_stocks: list[str] = []
    total_rows = 0
    last_slice = None
    ingest_days = _ingest_dates(conn, end, days)

    for day in ingest_days:
        slice_day = stock_daily_date_on_or_before(conn, day) or latest_stock_daily_date(
            conn
        )
        picked = select_hot_n(conn, n=n, trade_date=slice_day) if slice_day else []
        if not picked:
            raise FinMindError(
                "no stock_daily turnover slice; cannot pick 熱門前 N "
                f"(ingest_date={day})"
            )
        last_slice = slice_day
        mapped: list[tuple] = []
        brokers: list[tuple[str, str]] = []
        day_fail: list[str] = []
        for stock_id, _name, _turnover in picked:
            try:
                raw_rows = fetch(stock_id, day, day)
            except FinMindError:
                log.exception("FinMind SecIdAgg failed stock_id=%s date=%s", stock_id, day)
                day_fail.append(stock_id)
                continue
            except Exception:
                log.exception("FinMind SecIdAgg failed stock_id=%s date=%s", stock_id, day)
                day_fail.append(stock_id)
                continue
            for raw in raw_rows:
                mapped_row = map_secid_agg_row(raw)
                if mapped_row is None:
                    continue
                mapped.append(mapped_row)
                name = str(raw.get("securities_trader") or mapped_row[2]).strip()
                brokers.append((mapped_row[2], name or mapped_row[2]))
        wrote = upsert_live_rows(conn, mapped, brokers)
        total_rows += wrote
        failed_stocks.extend(day_fail)
        days_done.append(
            {
                "trade_date": day,
                "slice_trade_date": slice_day,
                "hot_n": len(picked),
                "rows": wrote,
                "failed_stocks": day_fail,
                "title": TITLE_HOT_N,
            }
        )
        log.info(
            "熱門股分點 ingest date=%s slice=%s n=%s rows=%s failed=%s",
            day,
            slice_day,
            len(picked),
            wrote,
            len(day_fail),
        )

    if total_rows == 0:
        raise FinMindError(
            "FinMind returned no SecIdAgg rows for 熱門前 N "
            f"(date={end}); data may not be published yet"
        )
    fail_ratio = len(failed_stocks) / max(
        sum(item["hot_n"] for item in days_done), 1
    )
    if fail_ratio > 0.5:
        raise FinMindError(
            f"FinMind failed for {len(failed_stocks)} of hot-N stocks; not marking live"
        )

    _meta_set(
        conn,
        {
            "source": SOURCE_LIVE,
            "slice_trade_date": last_slice or end,
            "ingest_trade_date": end,
            "hot_n": str(n),
        },
    )
    return {
        "title": TITLE_HOT_N,
        "coverage": "hot_n",
        "path": PATH,
        "ingest_configured": True,
        "production": True,
        "data_mode": DATA_MODE_LIVE,
        "trade_date": end,
        "slice_trade_date": last_slice,
        "hot_n": n,
        "rows": total_rows,
        "failed_stocks": failed_stocks,
        "days": days_done,
        "dataset": DATASET,
    }


def push_turso_if_configured(
    *,
    days: int = 14,
    today: date | None = None,
    skip: bool = False,
) -> dict:
    """Reuse cloud_db.push_market_files (trade_date window + full brokers/meta)."""
    from data import cloud_db

    if skip:
        log.info("Turso push skipped (--skip-turso)")
        return {"skipped": True}
    if not cloud_db.configured():
        log.info(
            "Turso not configured; broker_branch_daily stayed in local sqlite. "
            "Follow-up: set TURSO_DATABASE_URL + TURSO_AUTH_TOKEN to push "
            "broker_branch_daily / brokers / broker_branch_meta on the existing N-day window."
        )
        return {"skipped": True, "reason": "not_configured"}
    pushed = cloud_db.push_market_files(days=days, today=today)
    log.info("Turso push complete for 熱門股分點 tables (existing cloud_db window)")
    return pushed


def _connect_local() -> sqlite3.Connection:
    from market.collector import DB_PATH, init_db

    conn = sqlite3.connect(DB_PATH)
    configure_local(conn)
    init_db(conn)
    return conn


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    _ensure_dotenv()
    parser = argparse.ArgumentParser(
        description=(
            "Broker-branch helpers: status, TEST/DEV fixture, or path A scheduled "
            "ingest（熱門股分點動向，不是全市場）。FinMind writes are CLI/Actions only."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("status", "load-fixture", "ingest"),
        default="status",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="required for load-fixture; marks the write as TEST/DEV",
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--date", default="", help="ingest trade date YYYY-MM-DD")
    parser.add_argument("--n", type=int, default=None, help="hot-N (default BROKER_BRANCH_HOT_N or 80)")
    parser.add_argument("--days", type=int, default=1, help="calendar days to ingest ending at --date")
    parser.add_argument(
        "--skip-turso",
        action="store_true",
        help="do not push local sqlite to Turso even if secrets are set",
    )
    parser.add_argument(
        "--turso-days",
        type=int,
        default=14,
        help="N-day window for cloud_db.push_market_files (default 14)",
    )
    args = parser.parse_args(argv)

    if args.command == "status":
        status = ingest_status()
        # Never dump the token; ingest_status only has booleans.
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    if args.command == "load-fixture":
        if not args.dev:
            log.error("load-fixture refused: pass --dev (TEST/DEV only, not production)")
            return 2
        conn = _connect_local()
        try:
            result = load_fixture(conn, args.fixture, dev=True)
        finally:
            conn.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "ingest":
        if not token_present():
            log.error(
                "ingest refused: FINMIND_TOKEN missing. "
                "Path A scheduled ingest will not call FinMind. "
                "Set the GitHub Actions secret or local .env (not required on Cloud Run)."
            )
            return 2
        conn = _connect_local()
        try:
            result = ingest_hot_n(
                conn,
                trade_date=args.date or None,
                n=args.n,
                days=args.days,
            )
        except FinMindError as exc:
            log.error("scheduled ingest failed: %s", exc)
            conn.close()
            return 1
        except Exception:
            log.exception("scheduled ingest failed")
            conn.close()
            return 1
        conn.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        try:
            push_turso_if_configured(
                days=args.turso_days,
                skip=args.skip_turso,
            )
        except Exception:
            log.exception("Turso push failed")
            return 1
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
