#!/usr/bin/env python3
"""Broker-branch (分點買賣超) contract helpers — not a live FinMind ingest.

Owner has not decided token or slice (epic #53 / issue #54).
See docs/broker_branch.md.

This module:
- documents schema / ranking SQL used by empty tables in collector.init_db
- reports the token blocker honestly
- loads a TEST/DEV fixture only when explicitly asked
It does not call FinMind.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

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
EXPECTED_AFTER_HOUR = 21  # Asia/Taipei; FinMind SecIdAgg docs, not T86 16:00

TITLE_HOT_N = "熱門股分點動向"
TITLE_FULL_MARKET = "全市場分點買賣超"
TITLE_STOCK = "個股分點買賣超"

DATA_MODE_EMPTY = "empty_awaiting_owner_decision"
DATA_MODE_FIXTURE = "dev_fixture"

BLOCKER = (
    "FINMIND_TOKEN absent from GitHub secrets and local env. "
    "Owner has not decided token + 大盤 vs 個股. "
    "No live ingest in this PR."
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
CREATE INDEX IF NOT EXISTS idx_broker_branch_date_net
    ON broker_branch_daily(trade_date, net_volume);
CREATE INDEX IF NOT EXISTS idx_broker_branch_stock_date
    ON broker_branch_daily(stock_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_broker_branch_broker_date
    ON broker_branch_daily(broker_id, trade_date);
"""

DEFAULT_FIXTURE = repo_file("tests", "fixtures", "broker_branch_sample.json")


def token_present(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool((env.get("FINMIND_TOKEN") or "").strip())


def market_title(coverage: str) -> str:
    """UI/API title. 全市場 is allowed only for true full-market coverage."""
    if coverage == "full_market":
        return TITLE_FULL_MARKET
    if coverage == "single_stock":
        return TITLE_STOCK
    return TITLE_HOT_N


def ingest_status(env: dict[str, str] | None = None) -> dict:
    present = token_present(env)
    return {
        "kind": "broker_branch",
        "not": "t86_foreign",
        "dataset": DATASET,
        "token_present": present,
        "live_ingest": False,
        "slice_decision": "pending_owner",
        "blocker": None if present else BLOCKER,
        "hot_n_default": DEFAULT_HOT_N,
        "expected_after_hour": EXPECTED_AFTER_HOUR,
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


def data_mode(conn: sqlite3.Connection) -> str:
    return DATA_MODE_FIXTURE if row_count(conn) else DATA_MODE_EMPTY


def coverage_for_rows(conn: sqlite3.Connection) -> str:
    if not row_count(conn):
        return "empty"
    # Fixture and any future hot-N ingest are not full-market. Do not guess 全市場.
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
    mode = data_mode(conn)
    body = {
        **status,
        "title": market_title(coverage),
        "coverage": coverage,
        "coverage_note": (
            "加總範圍是已入庫的熱門前 N 檔，不是全市場。"
            if coverage == "hot_n"
            else "尚無正式 ingest。空狀態或本機 fixture，不是全市場。"
        ),
        "trade_date": trade_date,
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


def top_branches(
    conn: sqlite3.Connection,
    trade_date: str | None = None,
    k: int = DEFAULT_K,
    env: dict[str, str] | None = None,
) -> dict:
    k = max(1, min(int(k), 50))
    day = trade_date or latest_trade_date(conn)
    coverage = coverage_for_rows(conn) if day else "empty"
    buy: list = []
    sell: list = []
    universe = 0
    if day and _table_ready(conn):
        buy = conn.execute(
            "SELECT b.broker_id, COALESCE(MAX(br.broker_name), b.broker_id), "
            "SUM(b.net_volume) AS net "
            "FROM broker_branch_daily b "
            "LEFT JOIN brokers br ON br.broker_id = b.broker_id "
            "WHERE b.trade_date = ? "
            "GROUP BY b.broker_id "
            "ORDER BY net DESC LIMIT ?",
            (day, k),
        ).fetchall()
        sell = conn.execute(
            "SELECT b.broker_id, COALESCE(MAX(br.broker_name), b.broker_id), "
            "SUM(b.net_volume) AS net "
            "FROM broker_branch_daily b "
            "LEFT JOIN brokers br ON br.broker_id = b.broker_id "
            "WHERE b.trade_date = ? "
            "GROUP BY b.broker_id "
            "ORDER BY net ASC LIMIT ?",
            (day, k),
        ).fetchall()
        universe = int(
            conn.execute(
                "SELECT COUNT(DISTINCT stock_id) FROM broker_branch_daily "
                "WHERE trade_date = ?",
                (day,),
            ).fetchone()[0]
        )
    return _envelope(
        conn,
        coverage=coverage,
        trade_date=day,
        env=env,
        extra={
            "k": k,
            "hot_n": universe or None,
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
    return _envelope(
        conn,
        coverage=coverage,
        trade_date=day,
        env=env,
        extra={
            "stock_id": stock_id,
            "stock_name": name,
            "data": [list(r) for r in rows],
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
    conn.commit()
    log.info("loaded TEST/DEV broker-branch fixture %s (%d rows)", path, len(upsert))
    return {
        "path": str(path),
        "brokers": len(brokers),
        "rows": len(upsert),
        "data_mode": DATA_MODE_FIXTURE,
        "live_ingest": False,
        "production": False,
    }


def _connect_local() -> sqlite3.Connection:
    from market.collector import DB_PATH, init_db

    conn = sqlite3.connect(DB_PATH)
    configure_local(conn)
    init_db(conn)
    return conn


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="Broker-branch contract helpers (no live FinMind ingest)",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("status", "load-fixture"),
        default="status",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="required for load-fixture; marks the write as TEST/DEV",
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args(argv)

    if args.command == "status":
        status = ingest_status()
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

    return 1


if __name__ == "__main__":
    sys.exit(main())
