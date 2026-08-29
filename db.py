import os
import sqlite3
from pathlib import Path
from datetime import date, timedelta

_db_path: Path | None = None


def get_db_path() -> Path:
    if _db_path is not None:
        return _db_path
    env = os.environ.get("SCREENER_DB")
    if env:
        return Path(env)
    return Path(__file__).parent / "screener.db"


def set_db_path(path: Path | str | None) -> None:
    """Override the SQLite file path (used by tests). Pass None to reset."""
    global _db_path
    _db_path = Path(path) if path is not None else None


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT    NOT NULL,
                pattern_type    TEXT    NOT NULL,
                alert_date      TEXT    NOT NULL,
                price_at_alert  REAL    NOT NULL,
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS performance (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id        INTEGER NOT NULL REFERENCES alerts(id),
                check_date      TEXT    NOT NULL,
                price_at_check  REAL    NOT NULL,
                return_pct      REAL    NOT NULL,
                checked_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_ticker_pattern_date
            ON alerts (ticker, pattern_type, alert_date);
        """)


def save_alert(ticker: str, pattern_type: str, alert_date: str, price_at_alert: float) -> int | None:
    """Insert a new alert row and return its id, or None if it already exists."""
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO alerts (ticker, pattern_type, alert_date, price_at_alert) VALUES (?, ?, ?, ?)",
                (ticker, pattern_type, alert_date, price_at_alert),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def has_alert(ticker: str, pattern_type: str, alert_date: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE ticker = ? AND pattern_type = ? AND alert_date = ?",
            (ticker, pattern_type, alert_date),
        ).fetchone()
    return row is not None


def save_performance(alert_id: int, check_date: str, price_at_check: float, return_pct: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO performance (alert_id, check_date, price_at_check, return_pct) VALUES (?, ?, ?, ?)",
            (alert_id, check_date, price_at_check, return_pct),
        )


def get_pending_alerts(min_age_days: int = 28):
    """Return alerts at least min_age_days old that have no performance record yet.

    There is no upper bound: missing a daily run no longer drops alerts forever.
    """
    cutoff = str(date.today() - timedelta(days=min_age_days))
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.*
            FROM alerts a
            LEFT JOIN performance p ON p.alert_id = a.id
            WHERE a.alert_date <= ?
              AND p.id IS NULL
            ORDER BY a.alert_date
            """,
            (cutoff,),
        ).fetchall()
    return rows
