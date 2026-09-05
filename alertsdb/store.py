import os
import sqlite3
from pathlib import Path
from datetime import date, timedelta

from data.sqlite_util import configure_local
from web.tw_calendar import taiwan_today

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
    configure_local(conn)
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

            CREATE INDEX IF NOT EXISTS idx_alerts_alert_date
            ON alerts (alert_date);

            CREATE TABLE IF NOT EXISTS scanner_alert_profile (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                payload     TEXT    NOT NULL,
                updated_at  TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scanner_alert_runs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date            TEXT    NOT NULL,
                asof                TEXT,
                status              TEXT    NOT NULL,
                hit_count           INTEGER NOT NULL DEFAULT 0,
                skipped_duplicates  INTEGER NOT NULL DEFAULT 0,
                error               TEXT,
                detail              TEXT,
                created_at          TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_scanner_alert_runs_date
            ON scanner_alert_runs (run_date);
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
    cutoff = str(taiwan_today() - timedelta(days=min_age_days))
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

    today = today or taiwan_today()
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


DASHBOARD_HORIZONS = (5, 20, 60)
HORIZON_ASSUMPTIONS = (
    "以訊號K棒收盤價進場，持有至第 N 個交易日收盤出場（T+N 交易日，非日曆日）。"
)
_ALERT_COLS = ("id", "ticker", "pattern_type", "alert_date", "price_at_alert", "created_at")


def _as_dicts(rows, keys: tuple[str, ...] | None = None) -> list[dict]:
    out = []
    for row in rows:
        if hasattr(row, "keys") and not isinstance(row, (tuple, list)):
            out.append(dict(row))
        elif keys is not None:
            out.append(dict(zip(keys, row)))
        else:
            out.append(dict(row))
    return out


def _cell(row, key: str, idx: int):
    if isinstance(row, (tuple, list)):
        return row[idx]
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return row[idx]


def _run_on(conn, fn):
    if conn is not None:
        return fn(conn)
    with get_conn() as owned:
        return fn(owned)


def list_alerts(
    *,
    ticker: str | None = None,
    pattern_type: str | None = None,
    since: str | None = None,
    limit: int = 20,
    conn=None,
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

    def _query(c):
        rows = c.execute(
            f"""
            SELECT id, ticker, pattern_type, alert_date, price_at_alert, created_at
            FROM alerts
            WHERE {' AND '.join(clauses)}
            ORDER BY alert_date DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return _as_dicts(rows, _ALERT_COLS)

    return _run_on(conn, _query)


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


def _horizon_stats(n, wins, avg) -> dict:
    n = int(n or 0)
    wins = int(wins or 0)
    return {
        "n": n,
        "wins": wins,
        "win_rate_pct": round(100.0 * wins / n, 2) if n else None,
        "avg_return_pct": round(float(avg), 2) if avg is not None else None,
    }


def performance_by_horizon(
    horizons: tuple[int, ...] = DASHBOARD_HORIZONS,
    conn=None,
) -> dict:
    """T+5 / T+20 / T+60 summaries, overall and per pattern_type.

    Unlike `performance_summary`, this does not mix horizons into one bucket
    and does not report pending_28d.
    """
    horizons = tuple(int(h) for h in horizons) or DASHBOARD_HORIZONS

    def _empty_horizon(horizon_td: int) -> dict:
        return {
            "horizon_td": horizon_td,
            "n": 0,
            "wins": 0,
            "win_rate_pct": None,
            "avg_return_pct": None,
            "by_pattern": [],
        }

    def _query(c):
        placeholders = ",".join("?" * len(horizons))
        overall_rows = c.execute(
            f"""
            SELECT horizon_td,
                   COUNT(*) AS n,
                   SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) AS wins,
                   AVG(return_pct) AS avg_return_pct
            FROM performance
            WHERE horizon_td IN ({placeholders})
            GROUP BY horizon_td
            """,
            horizons,
        ).fetchall()
        pattern_rows = c.execute(
            f"""
            SELECT p.horizon_td,
                   a.pattern_type,
                   COUNT(*) AS n,
                   SUM(CASE WHEN p.return_pct > 0 THEN 1 ELSE 0 END) AS wins,
                   AVG(p.return_pct) AS avg_return_pct
            FROM performance p
            JOIN alerts a ON a.id = p.alert_id
            WHERE p.horizon_td IN ({placeholders})
            GROUP BY p.horizon_td, a.pattern_type
            ORDER BY p.horizon_td, a.pattern_type
            """,
            horizons,
        ).fetchall()
        return overall_rows, pattern_rows

    overall_rows, pattern_rows = _run_on(conn, _query)
    by_h: dict[int, dict] = {h: _empty_horizon(h) for h in horizons}
    for row in overall_rows:
        h = int(_cell(row, "horizon_td", 0))
        if h not in by_h:
            by_h[h] = _empty_horizon(h)
        stats = _horizon_stats(_cell(row, "n", 1), _cell(row, "wins", 2), _cell(row, "avg_return_pct", 3))
        by_h[h].update(stats)
        by_h[h]["horizon_td"] = h
        by_h[h]["by_pattern"] = []
    patterns: dict[int, list] = {h: [] for h in by_h}
    for row in pattern_rows:
        h = int(_cell(row, "horizon_td", 0))
        stats = _horizon_stats(_cell(row, "n", 2), _cell(row, "wins", 3), _cell(row, "avg_return_pct", 4))
        stats["pattern_type"] = _cell(row, "pattern_type", 1)
        patterns.setdefault(h, []).append(stats)
    for h, items in patterns.items():
        if h in by_h:
            by_h[h]["by_pattern"] = items
        else:
            extra = _empty_horizon(h)
            extra.update(_horizon_stats(
                sum(p["n"] for p in items),
                sum(p["wins"] for p in items),
                None,
            ))
            extra["by_pattern"] = items
            by_h[h] = extra

    ordered = [by_h[h] for h in horizons if h in by_h]
    for h, block in by_h.items():
        if h not in horizons:
            ordered.append(block)
    empty = all(block["n"] == 0 for block in ordered)
    return {
        "empty": empty,
        "assumptions": HORIZON_ASSUMPTIONS,
        "horizons": ordered,
    }
