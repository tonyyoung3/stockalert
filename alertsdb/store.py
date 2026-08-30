import os
import sqlite3
from pathlib import Path
from datetime import date, timedelta

# Repo root, not alertsdb/. Default file stays screener.db next to screener.py.
_ROOT = Path(__file__).resolve().parent.parent
_db_path: Path | None = None


def get_db_path() -> Path:
    if _db_path is not None:
        return _db_path
    env = os.environ.get("SCREENER_DB")
    if env:
        return Path(env)
    return _ROOT / "screener.db"


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
    from data.prices import calendar_buffer_days

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


def _as_dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]


def list_alerts(
    *,
    ticker: str | None = None,
    pattern_type: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Newest alerts first. `since` is an inclusive YYYY-MM-DD on alert_date."""
    clauses = ["1=1"]
    params: list = []
    if ticker:
        clauses.append("ticker = ?")
        params.append(ticker)
    if pattern_type:
        clauses.append("pattern_type = ?")
        params.append(pattern_type)
    if since:
        clauses.append("alert_date >= ?")
        params.append(since)
    params.append(max(1, limit))
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id, ticker, pattern_type, alert_date, price_at_alert, created_at
            FROM alerts
            WHERE {' AND '.join(clauses)}
            ORDER BY alert_date DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return _as_dicts(rows)


def list_alert_history(ticker: str, limit: int = 20) -> list[dict]:
    """Alerts for one ticker, joined with performance when it exists."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                a.id,
                a.ticker,
                a.pattern_type,
                a.alert_date,
                a.price_at_alert,
                p.check_date,
                p.price_at_check,
                p.return_pct
            FROM alerts a
            LEFT JOIN performance p ON p.alert_id = a.id
            WHERE a.ticker = ?
            ORDER BY a.alert_date DESC, a.id DESC
            LIMIT ?
            """,
            (ticker, max(1, limit)),
        ).fetchall()
    return _as_dicts(rows)


def count_alerts() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()
    return int(row["n"])


def performance_summary(pattern_type: str | None = None) -> dict:
    """Aggregate 28-day returns. `checked` is 0 when the table is still empty."""
    where = "1=1"
    params: list = []
    if pattern_type:
        where = "a.pattern_type = ?"
        params.append(pattern_type)

    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS checked,
                AVG(p.return_pct) AS avg_return_pct,
                SUM(CASE WHEN p.return_pct > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN p.return_pct < 0 THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN p.return_pct = 0 THEN 1 ELSE 0 END) AS flats,
                MIN(p.return_pct) AS min_return_pct,
                MAX(p.return_pct) AS max_return_pct
            FROM performance p
            JOIN alerts a ON a.id = p.alert_id
            WHERE {where}
            """,
            params,
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]

    checked = int(row["checked"] or 0)
    wins = int(row["wins"] or 0)
    return {
        "total_alerts": int(total),
        "checked": checked,
        "pending_28d": len(get_pending_alerts()),
        "wins": wins,
        "losses": int(row["losses"] or 0),
        "flats": int(row["flats"] or 0),
        "win_rate_pct": round(100.0 * wins / checked, 2) if checked else None,
        "avg_return_pct": round(float(row["avg_return_pct"]), 2) if row["avg_return_pct"] is not None else None,
        "min_return_pct": round(float(row["min_return_pct"]), 2) if row["min_return_pct"] is not None else None,
        "max_return_pct": round(float(row["max_return_pct"]), 2) if row["max_return_pct"] is not None else None,
        "pattern_type": pattern_type,
    }
