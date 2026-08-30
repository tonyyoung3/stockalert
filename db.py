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
                checked_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
                horizon_td      INTEGER NOT NULL DEFAULT 20
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_ticker_pattern_date
            ON alerts (ticker, pattern_type, alert_date);
        """)
        _ensure_performance_horizons(conn)


def _ensure_performance_horizons(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(performance)")}
    if "horizon_td" not in cols:
        conn.execute("ALTER TABLE performance ADD COLUMN horizon_td INTEGER NOT NULL DEFAULT 20")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_performance_alert_horizon "
        "ON performance (alert_id, horizon_td)"
    )


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


def save_performance(
    alert_id: int,
    check_date: str,
    price_at_check: float,
    return_pct: float,
    horizon_td: int = 20,
) -> bool:
    """Insert one horizon row. Returns False if that (alert, horizon) already exists."""
    with get_conn() as conn:
        try:
            conn.execute(
                """
                INSERT INTO performance
                    (alert_id, check_date, price_at_check, return_pct, horizon_td)
                VALUES (?, ?, ?, ?, ?)
                """,
                (alert_id, check_date, price_at_check, return_pct, horizon_td),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_pending_alerts(min_age_days: int = 28):
    """Alerts old enough that have no performance row at all (any horizon)."""
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


def get_pending_horizon_jobs(
    horizons: tuple[int, ...] = (5, 20, 60),
    today: date | None = None,
):
    """Alerts missing a row for a given trading-day horizon and old enough to try."""
    from prices import calendar_buffer_days

    today = today or date.today()
    jobs = []
    with get_conn() as conn:
        for horizon_td in horizons:
            cutoff = str(today - timedelta(days=calendar_buffer_days(horizon_td)))
            rows = conn.execute(
                """
                SELECT a.*
                FROM alerts a
                LEFT JOIN performance p
                  ON p.alert_id = a.id AND p.horizon_td = ?
                WHERE a.alert_date <= ?
                  AND p.id IS NULL
                ORDER BY a.alert_date
                """,
                (horizon_td, cutoff),
            ).fetchall()
            for row in rows:
                jobs.append((row, horizon_td))
    return jobs
