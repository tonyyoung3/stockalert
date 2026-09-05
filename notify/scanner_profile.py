"""One saved scanner-alert profile. Structured fields only — no DSL.

Persistence:
- repo file ``data/scanner_alert_profile.json`` (schedule default)
- single-row ``scanner_alert_profile`` in screener.db / Turso (dashboard save)

Load order for the job: explicit path → DB row → repo file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from data.paths import repo_file
from web.chip_zscore import (
    DEFAULT_WINDOW,
    MAX_TICKERS,
    MAX_WINDOW,
    MIN_WINDOW,
    Z_FIELDS,
    parse_tickers,
)

PROFILE_ID = 1
DEFAULT_PROFILE_PATH = repo_file("data", "scanner_alert_profile.json")
DEFAULT_FIELD = "foreign_net_z"
DEFAULT_MIN = 1.5
PATTERN_PREFIX = "scanner_"

# Scatter axes that the workbench already exposes (#79). No expressions.
ALLOWED_FIELDS = tuple(f"{name}_z" for name in Z_FIELDS) + Z_FIELDS + (
    "close",
    "volume",
    "turnover",
)
ALLOWED_AXES = ALLOWED_FIELDS

# Reject these so a "query language" cannot sneak in via extra keys.
FORBIDDEN_KEYS = frozenset({
    "dsl",
    "expr",
    "expression",
    "sql",
    "query",
    "where",
    "code",
    "script",
    "formula",
    "eval",
})

KNOWN_KEYS = frozenset({
    "tickers",
    "window",
    "min_periods",
    "field",
    "min",
    "max",
    "x",
    "y",
    "enabled",
    "asof",
})


def default_profile() -> dict:
    return {
        "tickers": [],
        "window": DEFAULT_WINDOW,
        "min_periods": DEFAULT_WINDOW,
        "field": DEFAULT_FIELD,
        "min": DEFAULT_MIN,
        "max": None,
        "x": DEFAULT_FIELD,
        "y": "close",
        "enabled": True,
        "asof": None,
    }


def pattern_type(field: str) -> str:
    return f"{PATTERN_PREFIX}{field}"


def _clamp_window(raw, default: int = DEFAULT_WINDOW) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    return max(MIN_WINDOW, min(n, MAX_WINDOW))


def _opt_float(raw):
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _opt_axis(raw, fallback: str | None) -> str | None:
    key = str(raw or "").strip()
    if key in ALLOWED_AXES:
        return key
    return fallback


def parse_profile(raw: Any) -> dict:
    """Validate a mapping. Extra DSL-like keys raise ValueError."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("profile must be an object")
    extra = {str(k).lower() for k in raw} & FORBIDDEN_KEYS
    if extra:
        raise ValueError(f"unsupported_condition:{sorted(extra)[0]}")

    out = default_profile()
    tickers = parse_tickers(raw.get("tickers") or [])
    if isinstance(raw.get("tickers"), str):
        tickers = parse_tickers([raw["tickers"]])
    out["tickers"] = tickers[:MAX_TICKERS]

    out["window"] = _clamp_window(raw.get("window"), DEFAULT_WINDOW)
    mp_raw = raw.get("min_periods")
    if mp_raw in (None, ""):
        out["min_periods"] = out["window"]
    else:
        out["min_periods"] = _clamp_window(mp_raw, out["window"])
        out["min_periods"] = min(out["min_periods"], out["window"])

    field = str(raw.get("field") or DEFAULT_FIELD).strip()
    if field not in ALLOWED_FIELDS:
        raise ValueError(f"unknown_field:{field}")
    out["field"] = field

    out["min"] = _opt_float(raw.get("min"))
    out["max"] = _opt_float(raw.get("max"))
    if out["min"] is None and out["max"] is None:
        out["min"] = DEFAULT_MIN

    out["x"] = _opt_axis(raw.get("x"), field if field in ALLOWED_AXES else DEFAULT_FIELD)
    out["y"] = _opt_axis(raw.get("y"), "close")
    enabled = raw.get("enabled", True)
    if isinstance(enabled, str):
        out["enabled"] = enabled.strip().lower() not in ("0", "false", "no", "off")
    else:
        out["enabled"] = bool(enabled)

    asof = raw.get("asof")
    asof_s = str(asof).strip() if asof else ""
    out["asof"] = asof_s if asof_s and len(asof_s) == 10 else None
    return out


def profile_from_file(path: Path | str | None = None) -> dict | None:
    path = Path(path) if path is not None else DEFAULT_PROFILE_PATH
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return parse_profile(data)


def write_profile_file(profile: dict, path: Path | str | None = None) -> Path:
    path = Path(path) if path is not None else DEFAULT_PROFILE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    body = parse_profile(profile)
    path.write_text(
        json.dumps(body, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def ensure_schema(conn) -> None:
    conn.executescript(
        """
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
        """
    )
    try:
        conn.commit()
    except Exception:
        pass


def profile_from_conn(conn) -> dict | None:
    if conn is None:
        return None
    ensure_schema(conn)
    try:
        row = conn.execute(
            "SELECT payload FROM scanner_alert_profile WHERE id = ?",
            (PROFILE_ID,),
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    payload = row[0] if not hasattr(row, "keys") else row["payload"]
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    return parse_profile(payload)


def save_profile_conn(conn, profile: dict) -> dict:
    body = parse_profile(profile)
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO scanner_alert_profile (id, payload, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            payload = excluded.payload,
            updated_at = CURRENT_TIMESTAMP
        """,
        (PROFILE_ID, json.dumps(body, ensure_ascii=False)),
    )
    try:
        conn.commit()
    except Exception:
        pass
    return body


def last_run(conn) -> dict | None:
    if conn is None:
        return None
    ensure_schema(conn)
    try:
        row = conn.execute(
            """
            SELECT id, run_date, asof, status, hit_count, skipped_duplicates,
                   error, detail, created_at
            FROM scanner_alert_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    keys = (
        "id",
        "run_date",
        "asof",
        "status",
        "hit_count",
        "skipped_duplicates",
        "error",
        "detail",
        "created_at",
    )
    if hasattr(row, "keys") and not isinstance(row, (tuple, list)):
        return {k: row[k] for k in keys}
    return dict(zip(keys, row))


def record_run(
    conn,
    *,
    run_date: str,
    status: str,
    asof: str | None = None,
    hit_count: int = 0,
    skipped_duplicates: int = 0,
    error: str | None = None,
    detail: str | None = None,
) -> None:
    if conn is None:
        return
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO scanner_alert_runs
            (run_date, asof, status, hit_count, skipped_duplicates, error, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_date, asof, status, hit_count, skipped_duplicates, error, detail),
    )
    try:
        conn.commit()
    except Exception:
        pass


def has_run_for(conn, asof: str, statuses: tuple[str, ...] = ("ok", "empty")) -> bool:
    if conn is None or not asof:
        return False
    ensure_schema(conn)
    ph = ",".join("?" * len(statuses))
    row = conn.execute(
        f"""
        SELECT 1 FROM scanner_alert_runs
        WHERE asof = ? AND status IN ({ph})
        LIMIT 1
        """,
        (asof, *statuses),
    ).fetchone()
    return row is not None


def row_hits(row: dict, profile: dict) -> bool:
    """True when a chip_zscore row meets the saved min/max on ``field``."""
    if not row or row.get("insufficient_sample"):
        return False
    val = row.get(profile["field"])
    if val is None:
        return False
    try:
        num = float(val)
    except (TypeError, ValueError):
        return False
    lo, hi = profile.get("min"), profile.get("max")
    if lo is not None and num < lo:
        return False
    if hi is not None and num > hi:
        return False
    return True


def describe_condition(profile: dict) -> str:
    field = profile.get("field") or DEFAULT_FIELD
    parts = [field]
    if profile.get("min") is not None:
        parts.append(f">= {profile['min']}")
    if profile.get("max") is not None:
        parts.append(f"<= {profile['max']}")
    return " ".join(parts)
