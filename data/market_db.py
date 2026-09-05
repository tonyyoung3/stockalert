"""Read-only market data for the website: local sqlite or Turso.

Collectors still write twse_data.db. Cloud Run has no disk, so when
TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are set the dashboard queries Turso.
Backtests snapshot the few needed tables into a temp sqlite so pandas keeps working.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from data.paths import repo_file
from data.sqlite_util import configure_local

BACKTEST_TABLES = ("taiex_hourly_ohlc", "taiex_daily", "taifex_fut_oi")

_override: Path | None = None
_snapshot_path: Path | None = None


def set_db_path(path: Path | str | None) -> None:
    """Tests and local overrides. Pass None to reset."""
    global _override, _snapshot_path
    _override = Path(path) if path is not None else None
    _snapshot_path = None


def local_path() -> Path:
    if _override is not None:
        return _override
    env = os.environ.get("TWSE_DB")
    if env:
        return Path(env)
    return repo_file("twse_data.db")


def using_turso(env: dict[str, str] | None = None) -> bool:
    if _override is not None:
        return False
    from data import cloud_db
    return cloud_db.configured(env)


def available(env: dict[str, str] | None = None) -> bool:
    if using_turso(env):
        return True
    return local_path().exists()


def _readonly_file(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    configure_local(conn, wal=False)
    return conn


def connect(env: dict[str, str] | None = None):
    if using_turso(env):
        from data import cloud_db
        return cloud_db.connect_remote(env)
    return _readonly_file(local_path())


def fetchall(sql: str, params: tuple = (), env: dict[str, str] | None = None) -> list:
    conn = connect(env)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _copy_table(remote, dest: sqlite3.Connection, table: str) -> int:
    from data.cloud_db import _ident

    table = _ident(table)
    try:
        schema = remote.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    except Exception:
        schema = None
    if not schema or not schema[0]:
        return 0
    dest.execute(schema[0] if "IF NOT EXISTS" in schema[0].upper()
                 else schema[0].replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
    cols = [row[1] for row in remote.execute(f"PRAGMA table_info({table})")]
    if not cols:
        return 0
    col_sql = ", ".join(_ident(c) for c in cols)
    rows = remote.execute(f"SELECT {col_sql} FROM {table}").fetchall()
    if rows:
        placeholders = ", ".join("?" for _ in cols)
        dest.executemany(
            f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({placeholders})",
            rows,
        )
    return len(rows)


def connect_for_backtest(env: dict[str, str] | None = None) -> sqlite3.Connection:
    """sqlite3 connection pandas.read_sql can use."""
    global _snapshot_path
    if not using_turso(env):
        return _readonly_file(local_path())

    if _snapshot_path is not None and _snapshot_path.exists():
        return _readonly_file(_snapshot_path)

    from data import cloud_db
    remote = cloud_db.connect_remote(env)
    try:
        fd, name = tempfile.mkstemp(prefix="stockalert-bt-", suffix=".db")
        os.close(fd)
        dest = sqlite3.connect(name)
        try:
            for table in BACKTEST_TABLES:
                _copy_table(remote, dest, table)
            dest.commit()
        finally:
            dest.close()
        _snapshot_path = Path(name)
    finally:
        remote.close()
    return _readonly_file(_snapshot_path)


def listen_host_port(env: dict[str, str] | None = None) -> tuple[str, int]:
    """Cloud Run / any PaaS sets PORT and expects 0.0.0.0."""
    env = env if env is not None else os.environ
    raw = (env.get("PORT") or "").strip()
    port = int(raw) if raw else 8765
    public = bool(env.get("K_SERVICE") or raw)
    host = (env.get("HOST") or "").strip() or ("0.0.0.0" if public else "127.0.0.1")
    return host, port


def must_listen(env: dict[str, str] | None = None) -> bool:
    """Cloud Run sets PORT / K_SERVICE and requires the process to bind immediately."""
    env = env if env is not None else os.environ
    return bool((env.get("PORT") or "").strip() or env.get("K_SERVICE"))


def should_open_browser(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else os.environ
    if env.get("K_SERVICE") or env.get("DASHBOARD_NO_BROWSER"):
        return False
    return not bool((env.get("PORT") or "").strip())
