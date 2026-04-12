import sqlite3
from pathlib import Path
from datetime import date, timedelta

DB_PATH = Path(__file__).parent / "screener.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
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
        """)


def save_alert(ticker: str, pattern_type: str, alert_date: str, price_at_alert: float) -> int:
    """Insert a new alert row and return its id."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (ticker, pattern_type, alert_date, price_at_alert) VALUES (?, ?, ?, ?)",
            (ticker, pattern_type, alert_date, price_at_alert),
        )
        return cur.lastrowid


def save_performance(alert_id: int, check_date: str, price_at_check: float, return_pct: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO performance (alert_id, check_date, price_at_check, return_pct) VALUES (?, ?, ?, ?)",
            (alert_id, check_date, price_at_check, return_pct),
        )


def get_pending_alerts(days_ago_min: int = 28, days_ago_max: int = 35):
    """Return alerts whose alert_date falls in the look-back window and
    have no performance record yet."""
    today = date.today()
    date_min = str(today - timedelta(days=days_ago_max))
    date_max = str(today - timedelta(days=days_ago_min))
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.*
            FROM alerts a
            LEFT JOIN performance p ON p.alert_id = a.id
            WHERE a.alert_date BETWEEN ? AND ?
              AND p.id IS NULL
            ORDER BY a.alert_date
            """,
            (date_min, date_max),
        ).fetchall()
    return rows
