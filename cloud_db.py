#!/usr/bin/env python3
"""Push local SQLite files to a Turso / libSQL database.

Collectors keep writing twse_data.db / us_data.db. The screener keeps writing
screener.db. When TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are set, scheduled
jobs copy recent rows to one remote database (市場表 + 訊號表可以共存).

Incremental copy uses the first present of trade_date / alert_date / check_date.
Tables without those columns (e.g. stocks) are copied in full.

用法:
  python cloud_db.py status
  python cloud_db.py push --days 14
  python cloud_db.py push-alerts
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CHUNK = 400
DEFAULT_FILES = (ROOT / "twse_data.db", ROOT / "us_data.db")
ALERT_FILES = (ROOT / "screener.db",)
SINCE_COLUMNS = ("trade_date", "alert_date", "check_date")


def _ident(name: str) -> str:
    if not IDENT.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def configured(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else os.environ
    url = (env.get("TURSO_DATABASE_URL") or "").strip()
    token = (env.get("TURSO_AUTH_TOKEN") or "").strip()
    return bool(url and token)


def connect_remote(env: dict[str, str] | None = None):
    env = env if env is not None else os.environ
    url = (env.get("TURSO_DATABASE_URL") or "").strip()
    token = (env.get("TURSO_AUTH_TOKEN") or "").strip()
    if not url or not token:
        raise RuntimeError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are required")
    import libsql
    return libsql.connect(database=url, auth_token=token)


def _add_if_not_exists(sql: str) -> str:
    upper = sql.upper()
    if "IF NOT EXISTS" in upper:
        return sql
    for prefix in ("CREATE UNIQUE INDEX", "CREATE INDEX", "CREATE TABLE"):
        if upper.startswith(prefix):
            return prefix + " IF NOT EXISTS" + sql[len(prefix):]
    return sql


def ensure_schema(local: sqlite3.Connection, remote) -> None:
    rows = local.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "AND type IN ('table', 'index') "
        "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name"
    ).fetchall()
    for _type, name, sql in rows:
        _ident(name)
        remote.execute(_add_if_not_exists(sql))
    remote.commit()


def _columns(local: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in local.execute(f"PRAGMA table_info({_ident(table)})")]


def _since_column(local: sqlite3.Connection, table: str) -> str | None:
    cols = set(_columns(local, table))
    for name in SINCE_COLUMNS:
        if name in cols:
            return name
    return None


def copy_table(local: sqlite3.Connection, remote, table: str, since: str | None) -> int:
    table = _ident(table)
    cols = _columns(local, table)
    if not cols:
        return 0
    col_sql = ",".join(cols)
    placeholders = ",".join("?" * len(cols))
    sql = f"SELECT {col_sql} FROM {table}"
    params: tuple = ()
    date_col = _since_column(local, table) if since else None
    if since and date_col:
        sql += f" WHERE {_ident(date_col)} >= ?"
        params = (since,)
    rows = local.execute(sql, params).fetchall()
    insert = f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({placeholders})"
    for i in range(0, len(rows), CHUNK):
        remote.executemany(insert, rows[i:i + CHUNK])
        remote.commit()
    return len(rows)


def push_file(path: Path, remote, since: str | None = None) -> dict[str, int]:
    if not path.exists():
        log.info("skip missing %s", path.name)
        return {}
    local = sqlite3.connect(path)
    try:
        ensure_schema(local, remote)
        tables = [
            name for (name,) in local.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        counts = {}
        for table in tables:
            n = copy_table(local, remote, table, since)
            counts[table] = n
            log.info("%s %s: %s rows", path.name, table, n)
        return counts
    finally:
        local.close()


def _push_paths(
    files: tuple[Path, ...],
    days: int | None,
    today: date | None,
    remote=None,
) -> dict[str, dict[str, int]]:
    if not configured() and remote is None:
        log.info("Turso not configured; leaving data in local sqlite")
        return {}
    remote = remote or connect_remote()
    since = None
    if days is not None:
        since = ((today or date.today()) - timedelta(days=days)).isoformat()
    out = {}
    for path in files:
        out[path.name] = push_file(path, remote, since)
    return out


def push_market_files(
    files: tuple[Path, ...] | None = None,
    days: int | None = 14,
    today: date | None = None,
    remote=None,
) -> dict[str, dict[str, int]]:
    return _push_paths(files or DEFAULT_FILES, days, today, remote)


def push_alert_files(
    files: tuple[Path, ...] | None = None,
    days: int | None = None,
    today: date | None = None,
    remote=None,
) -> dict[str, dict[str, int]]:
    """Copy screener.db. Default days=None is a full copy: the file is small
    and performance.alert_id must already exist on the remote."""
    return _push_paths(files or ALERT_FILES, days, today, remote)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Sync local sqlite files to Turso")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("status", "push", "push-alerts"),
        default="status",
    )
    parser.add_argument("--days", type=int, default=None)
    args = parser.parse_args(argv)
    if args.command == "status":
        print("turso", "configured" if configured() else "not configured")
        return 0
    if not configured():
        if args.command == "push-alerts":
            log.info("Turso not configured; leaving screener.db local")
            return 0
        log.error("set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN")
        return 1
    if args.command == "push-alerts":
        push_alert_files(days=args.days)
        return 0
    push_market_files(days=args.days if args.days is not None else 14)
    return 0


if __name__ == "__main__":
    sys.exit(main())
