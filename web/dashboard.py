#!/usr/bin/env python3
"""台股資料儀表板。

  python -m web.dashboard        # 本機 http://localhost:8765,讀 twse_data.db
  PORT=8080 python -m web.dashboard   # Cloud Run / 任何 PaaS

設了 TURSO_DATABASE_URL 跟 TURSO_AUTH_TOKEN 就讀雲端,否則讀本機 sqlite。
告警／績效：本機讀 screener.db 的 alerts、performance；Turso 則與市場表同一顆遠端 DB。

HTML/JS 在 web/static/；GET / 與 GET /static/* 帶 ETag / Cache-Control。
Python 只當薄殼：auth、API、靜態檔。

DASHBOARD_USER 與 DASHBOARD_PASSWORD 都有值時啟用 HTTP Basic Auth
（HTML、/static/* 與 /api/*；GET /health 永遠開放）。缺任一變數則為匿名：本機預設
允許（回測仍限流）；Cloud Run（K_SERVICE）需設帳密或
DASHBOARD_ALLOW_ANONYMOUS=1，否則業務 API 拒絕。
"""
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import sqlite3
import threading
import time
import webbrowser
from contextvars import ContextVar
from datetime import timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

from alertsdb.store import (
    DASHBOARD_HORIZONS,
    HORIZON_ASSUMPTIONS,
    get_conn,
    get_db_path,
    list_alerts,
    performance_by_horizon,
)
from data import market_db
from market import broker_branch as broker_branch_mod
from web import broker_main_force as broker_main_force_mod
from web import chip_zscore as chip_zscore_mod
from web import freshness as freshness_mod

_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# One sqlite/Turso connection per HTTP API request (Cloud Run round-trips are expensive).
# ContextVar is copied per thread: ThreadingHTTPServer workers must not share this
# cursor/connection. api() always opens, binds, then resets+closes in the same thread.
_request_conn: ContextVar = ContextVar("dashboard_db_conn", default=None)
_log = logging.getLogger("web.dashboard")


def q(sql, params=()):
    conn = _request_conn.get()
    if conn is not None:
        return conn.execute(sql, params).fetchall()
    return market_db.fetchall(sql, params)


def _ymd(qs, key):
    v = (qs.get(key, [""])[0] or "").strip()
    return v if _YMD.fullmatch(v) else None


_STOCK_ID = re.compile(r"^[0-9A-Za-z]{2,10}$")
AUTH_REALM = "stockalert"
UNAUTHORIZED_JSON = {"error": "unauthorized"}
ANONYMOUS_DISABLED_JSON = {"error": "anonymous_disabled"}
RATE_LIMITED_JSON = {"error": "rate_limited"}
INVALID_JSON = {"error": "invalid_json"}
INTERNAL_ERROR_JSON = {"error": "internal_error"}
BACKTEST_FAILED_JSON = {"error": "回測執行失敗"}
BACKTEST_RATE_LIMIT = 10
BACKTEST_ANON_RATE_LIMIT = 3
BACKTEST_RATE_WINDOW_SEC = 60
_TRUTHY = {"1", "true", "yes", "on"}


def dashboard_credentials(env=None):
    """Return (user, password) when both env vars are non-empty; else None."""
    env = os.environ if env is None else env
    user = (env.get("DASHBOARD_USER") or "").strip()
    password = (env.get("DASHBOARD_PASSWORD") or "").strip()
    if user and password:
        return user, password
    return None


def auth_enabled(env=None) -> bool:
    return dashboard_credentials(env) is not None


def env_flag(name: str, env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get(name) or "").strip().lower() in _TRUTHY


def on_cloud_run(env=None) -> bool:
    """Cloud Run always sets K_SERVICE. PORT-only local sims stay anonymous-allowed."""
    env = os.environ if env is None else env
    return bool((env.get("K_SERVICE") or "").strip())


def allow_anonymous(env=None) -> bool:
    """Whether unauthenticated HTML / business APIs are served.

    Local-dev (no K_SERVICE): yes, with backtest rate limits.
    Cloud Run: no, unless DASHBOARD_ALLOW_ANONYMOUS=1 (or Basic auth is set;
    callers still go through auth when credentials exist).
    DASHBOARD_FAIL_CLOSED=1 denies anonymous even locally.
    """
    env = os.environ if env is None else env
    if env_flag("DASHBOARD_ALLOW_ANONYMOUS", env):
        return True
    if env_flag("DASHBOARD_FAIL_CLOSED", env):
        return False
    if on_cloud_run(env):
        return False
    return True


def _env_int(env, name: str, default: int, lo: int, hi: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(int(raw), hi))
    except (TypeError, ValueError):
        return default


def backtest_max_hits(env=None) -> int:
    """RPM cap for POST /api/backtest*. Anonymous is stricter."""
    env = os.environ if env is None else env
    if auth_enabled(env):
        return _env_int(env, "DASHBOARD_BACKTEST_RPM", BACKTEST_RATE_LIMIT, 1, 600)
    return _env_int(env, "DASHBOARD_BACKTEST_ANON_RPM", BACKTEST_ANON_RATE_LIMIT, 1, 600)


def path_requires_auth(path: str) -> bool:
    if path == "/health":
        return False
    if path in ("/", "/index.html") or path.startswith("/static/"):
        return True
    return path.startswith("/api/")


def parse_basic_authorization(header: str | None):
    if not header:
        return None
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "basic" or not rest:
        return None
    try:
        decoded = base64.b64decode(rest.strip(), validate=True).decode("utf-8")
    except Exception:
        return None
    if ":" not in decoded:
        return None
    user, password = decoded.split(":", 1)
    return user, password


def _const_eq(left: str, right: str) -> bool:
    a, b = left.encode("utf-8"), right.encode("utf-8")
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


def valid_basic_header(header: str | None, user: str, password: str) -> bool:
    parsed = parse_basic_authorization(header)
    if parsed is None:
        return False
    got_user, got_pass = parsed
    return _const_eq(got_user, user) and _const_eq(got_pass, password)


class _IpRateLimiter:
    def __init__(self, max_hits, window_sec):
        self.max_hits = max_hits
        self.window_sec = window_sec
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key, now=None, max_hits=None) -> bool:
        now = time.monotonic() if now is None else now
        cap = self.max_hits if max_hits is None else max_hits
        with self._lock:
            recent = [t for t in self._hits.get(key, ()) if now - t < self.window_sec]
            if len(recent) >= cap:
                self._hits[key] = recent
                return False
            recent.append(now)
            self._hits[key] = recent
            return True

    def reset(self):
        with self._lock:
            self._hits.clear()


_backtest_limiter = _IpRateLimiter(BACKTEST_RATE_LIMIT, BACKTEST_RATE_WINDOW_SEC)


def client_ip(handler) -> str:
    """Rate-limit identity: trust the *rightmost* X-Forwarded-For hop.

    Cloud Run sits behind one trusted proxy that appends the connecting
    client as the last XFF segment. Leading segments are client-controlled
    and must not mint extra limiter buckets.
    """
    xff = handler.headers.get("X-Forwarded-For") if handler.headers else None
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    if handler.client_address:
        return handler.client_address[0]
    return "unknown"


def _synthetic_handler_hold():
    """Production no-op. Tests patch this to hold a worker while probing /health."""
    return None


def execute_index_backtest(conn, payload):
    from web import backtest_engine
    return backtest_engine.run_backtest(conn, payload)


def execute_stock_backtest(conn, payload):
    from web.stock_backtest import run_stock_backtest
    return run_stock_backtest(conn, payload)


def parse_stock_query(query: str | None) -> str | None:
    """Extract a ticker from `?stock=` / `stock=` query text.

    Accepts a raw query string, a leading `?`, or a path/URL that contains
    `?stock=`. First whitespace-delimited token must be 2–10 alphanumerics
    (same rule as the dashboard JS `parseStockQuery`).
    """
    if not query:
        return None
    raw = query.strip()
    if "?" in raw:
        raw = raw.split("?", 1)[1]
    if "#" in raw:
        raw = raw.split("#", 1)[0]
    qs = parse_qs(raw, keep_blank_values=True)
    val = (qs.get("stock", [""])[0] or "").strip()
    if not val:
        return None
    token = val.split()[0]
    return token if _STOCK_ID.fullmatch(token) else None


def _foreign_window(qs):
    """Inclusive (start, end) for foreign ranking. Empty DB → (None, None).

    start/end (YYYY-MM-DD) win if either is set; otherwise `days` is the last N
    trading days in foreign_daily. With neither param, latest day only.
    """
    latest_row = q("SELECT MAX(trade_date) FROM foreign_daily")
    latest = latest_row[0][0] if latest_row else None
    if not latest:
        return None, None
    start, end = _ymd(qs, "start"), _ymd(qs, "end")
    if start or end:
        start = start or latest
        end = end or latest
        if start > end:
            start, end = end, start
        return start, end
    raw = (qs.get("days", [""])[0] or "").strip()
    if raw.isdigit():
        n = max(1, min(int(raw), 730))
        span = q(
            "SELECT MIN(d), MAX(d) FROM ("
            "SELECT DISTINCT trade_date AS d FROM foreign_daily "
            "ORDER BY trade_date DESC LIMIT ?) AS t",
            (n,),
        )[0]
        return span[0], span[1]
    return latest, latest


def _clamp_int(raw, default, lo, hi):
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(n, hi))


def _open_alerts_conn():
    """Turso shares market + alerts on the request connection; local uses screener.db.

    Returns (conn, owns). owns=True means the caller must close conn.
    """
    if market_db.using_turso():
        shared = _request_conn.get()
        if shared is not None:
            return shared, False
        return market_db.connect(), True
    path = get_db_path()
    if not path.exists():
        return None, False
    return get_conn(), True


def _empty_alerts(days, since=None):
    return {"data": [], "empty": True, "days": days, "since": since}


def _empty_performance():
    return {
        "empty": True,
        "assumptions": HORIZON_ASSUMPTIONS,
        "horizons": [
            {
                "horizon_td": h,
                "n": 0,
                "wins": 0,
                "win_rate_pct": None,
                "avg_return_pct": None,
                "by_pattern": [],
            }
            for h in DASHBOARD_HORIZONS
        ],
    }


def _stock_names(tickers: list[str]) -> dict[str, str]:
    uniq = list(dict.fromkeys(t for t in tickers if t))
    if not uniq:
        return {}
    placeholders = ",".join("?" * len(uniq))
    try:
        rows = q(
            f"SELECT stock_id, stock_name FROM stocks WHERE stock_id IN ({placeholders})",
            tuple(uniq),
        )
    except Exception:
        return {}
    return {row[0]: row[1] for row in rows if row and row[0]}


def api_alerts(qs) -> dict:
    days = _clamp_int((qs.get("days", ["30"])[0] or "").strip(), 30, 1, 365)
    since = str(freshness_mod.taiwan_today() - timedelta(days=days))
    conn, owns = None, False
    try:
        conn, owns = _open_alerts_conn()
        if conn is None:
            return _empty_alerts(days, since)
        rows = list_alerts(since=since, limit=500, conn=conn)
    except Exception:
        return _empty_alerts(days, since)
    finally:
        if owns and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    names = _stock_names([r.get("ticker") for r in rows])
    data = [
        {
            "alert_date": r.get("alert_date"),
            "ticker": r.get("ticker"),
            "name": names.get(r.get("ticker")),
            "pattern_type": r.get("pattern_type"),
            "price_at_alert": r.get("price_at_alert"),
            "theme": None,
        }
        for r in rows
    ]
    return {"data": data, "empty": not data, "days": days, "since": since}


def api_performance() -> dict:
    conn, owns = None, False
    try:
        conn, owns = _open_alerts_conn()
        if conn is None:
            return _empty_performance()
        return performance_by_horizon(horizons=DASHBOARD_HORIZONS, conn=conn)
    except Exception:
        return _empty_performance()
    finally:
        if owns and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _max_date(sql, params=()):
    try:
        rows = q(sql, params)
    except Exception:
        return None
    if not rows:
        return None
    val = rows[0][0]
    return val or None


def _alerts_last_date():
    if market_db.using_turso():
        conn, owns = None, False
        try:
            conn, owns = _open_alerts_conn()
            if conn is None:
                return None
            row = conn.execute("SELECT MAX(alert_date) FROM alerts").fetchone()
        except Exception:
            return None
        finally:
            if owns and conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        if not row:
            return None
        return row[0] or None
    path = get_db_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT MAX(alert_date) FROM alerts").fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row:
        return None
    return row[0] or None


def build_freshness(now=None) -> dict:
    """Per-key-table last date, days-ago, stale/empty. Same connection as market APIs."""
    tw = freshness_mod.taiwan_now(now)
    today = tw.date()
    expected = freshness_mod.expected_tw_trade_date(now)
    specs = (
        ("foreign_daily", "SELECT MAX(trade_date) FROM foreign_daily"),
        ("stock_daily", "SELECT MAX(trade_date) FROM stock_daily"),
        ("taifex", "SELECT MAX(trade_date) FROM taifex_fut_oi"),
    )
    tables = [
        freshness_mod.table_status(name, _max_date(sql), today, expected)
        for name, sql in specs
    ]
    tables.append(
        freshness_mod.table_status("alerts", _alerts_last_date(), today, expected)
    )
    return {
        "as_of": today.isoformat(),
        "expected_trade_date": expected.isoformat(),
        "calendar": freshness_mod.CALENDAR,
        "calendar_note": freshness_mod.CALENDAR_NOTE,
        "stale": any(t["stale"] for t in tables),
        "empty": any(t["empty"] for t in tables),
        "tables": tables,
    }


def health_payload() -> dict:
    """Process liveness. Always HTTP 200; staleness lives in the payload."""
    body = {
        "status": "ok",
        "ok": True,
        "note": freshness_mod.HEALTH_NOTE,
    }
    try:
        if not market_db.available():
            body["freshness"] = {
                "stale": True,
                "empty": True,
                "tables": [],
                "error": "db_unavailable",
                "calendar": freshness_mod.CALENDAR,
                "calendar_note": freshness_mod.CALENDAR_NOTE,
            }
            return body
        body["freshness"] = api("/api/freshness", {})
    except Exception:
        _log.exception("health freshness check failed")
        body["freshness"] = {
            "stale": True,
            "empty": True,
            "tables": [],
            "error": "freshness_unavailable",
            "calendar": freshness_mod.CALENDAR,
            "calendar_note": freshness_mod.CALENDAR_NOTE,
        }
    return body


def api(path, qs):
    conn = market_db.connect()
    token = _request_conn.set(conn)
    try:
        return _api(path, qs)
    finally:
        _request_conn.reset(token)
        conn.close()


def _api(path, qs):
    try:
        days = int(qs.get("days", ["90"])[0])
    except (TypeError, ValueError):
        days = 90
    if path == "/api/summary":
        idx = q("SELECT ts, index_value, change FROM taiex_hourly ORDER BY ts DESC LIMIT 1")
        latest = q("SELECT MAX(trade_date) FROM foreign_daily")[0][0]
        tot = q("SELECT SUM(foreign_net), COUNT(*) FROM foreign_daily WHERE trade_date=?", (latest,))
        span = q("SELECT MIN(trade_date), MAX(trade_date) FROM foreign_daily")
        return {"index": idx[0] if idx else None, "latest_date": latest,
                "foreign_net_total": tot[0][0], "stock_count": tot[0][1],
                "date_range": span[0], "freshness": build_freshness()}
    if path == "/api/freshness":
        return build_freshness()
    if path == "/api/ohlc":
        if qs.get("interval", ["day"])[0] == "hour":
            rows = q("SELECT ts, open, high, low, close FROM taiex_hourly_ohlc "
                     "WHERE trade_date >= date('now', ?) ORDER BY ts", (f"-{days} day",))
        else:
            rows = q("SELECT trade_date, open, high, low, close FROM taiex_daily "
                     "WHERE trade_date >= date('now', ?) ORDER BY trade_date", (f"-{days} day",))
        return {"data": rows}
    if path == "/api/taiex":
        rows = q("SELECT ts, index_value FROM taiex_hourly "
                 "WHERE trade_date >= date('now', ?) ORDER BY ts", (f"-{days} day",))
        return {"data": rows}
    if path == "/api/foreign_total":
        rows = q("SELECT trade_date, SUM(foreign_net) FROM foreign_daily "
                 "WHERE trade_date >= date('now', ?) GROUP BY trade_date ORDER BY trade_date",
                 (f"-{days} day",))
        return {"data": rows}
    if path == "/api/top":
        start, end = _foreign_window(qs)
        if not start:
            return {"date": None, "start": None, "end": None,
                    "trading_days": 0, "buy": [], "sell": []}
        buy = q(
            "SELECT stock_id, MAX(stock_name), SUM(foreign_net) FROM foreign_daily "
            "WHERE trade_date BETWEEN ? AND ? GROUP BY stock_id "
            "ORDER BY SUM(foreign_net) DESC LIMIT 15",
            (start, end),
        )
        sell = q(
            "SELECT stock_id, MAX(stock_name), SUM(foreign_net) FROM foreign_daily "
            "WHERE trade_date BETWEEN ? AND ? GROUP BY stock_id "
            "ORDER BY SUM(foreign_net) ASC LIMIT 15",
            (start, end),
        )
        n_days = q(
            "SELECT COUNT(DISTINCT trade_date) FROM foreign_daily "
            "WHERE trade_date BETWEEN ? AND ?",
            (start, end),
        )[0][0]
        label = start if start == end else f"{start} ~ {end}"
        return {"date": label, "start": start, "end": end,
                "trading_days": n_days, "buy": buy, "sell": sell}
    if path == "/api/margin_total":
        fin = q("SELECT trade_date, balance FROM margin_total "
                "WHERE item LIKE '融資金額%' AND trade_date >= date('now', ?) "
                "ORDER BY trade_date", (f"-{days} day",))
        short = q("SELECT trade_date, balance FROM margin_total "
                  "WHERE item LIKE '融券%' AND trade_date >= date('now', ?) "
                  "ORDER BY trade_date", (f"-{days} day",))
        return {"fin": fin, "short": short}
    if path == "/api/stock_margin":
        sid = qs.get("id", [""])[0].strip()
        rows = q("SELECT trade_date, margin_balance, short_balance FROM margin_stock "
                 "WHERE stock_id=? AND trade_date >= date('now', ?) ORDER BY trade_date",
                 (sid, f"-{days} day",))
        return {"id": sid, "data": rows}
    if path == "/api/taifex_oi":
        rows = q("SELECT trade_date, investor, oi_net_lots FROM taifex_fut_oi "
                 "WHERE product='臺股期貨' AND trade_date >= date('now', ?) "
                 "ORDER BY trade_date", (f"-{days} day",))
        by_date = {}
        for d, inv, net in rows:
            by_date.setdefault(d, {})[inv] = net
        dates = sorted(by_date)
        foreign = [by_date[d].get("外資及陸資") for d in dates]
        trust = [by_date[d].get("投信") for d in dates]
        ratio = [round(f / t, 3) if (f is not None and t not in (None, 0)) else None
                 for f, t in zip(foreign, trust)]
        return {"dates": dates, "foreign": foreign, "trust": trust, "ratio": ratio}
    if path == "/api/stock":
        sid = qs.get("id", [""])[0].strip()
        rows = q("SELECT trade_date, stock_name, foreign_buy, foreign_sell, foreign_net "
                 "FROM foreign_daily WHERE stock_id=? AND trade_date >= date('now', ?) "
                 "ORDER BY trade_date", (sid, f"-{days} day"))
        return {"id": sid, "data": rows}
    if path == "/api/stocks":
        needle = (qs.get("q", [""])[0] or "").strip()[:32]
        if not needle:
            return {"data": []}
        like, prefix = f"%{needle}%", f"{needle}%"
        rows = q(
            "SELECT stock_id, stock_name FROM stocks "
            "WHERE stock_id LIKE ? OR stock_name LIKE ? "
            "ORDER BY CASE WHEN stock_id=? THEN 0 WHEN stock_id LIKE ? THEN 1 "
            "WHEN stock_name LIKE ? THEN 2 ELSE 3 END, stock_id LIMIT 20",
            (like, like, needle, prefix, prefix),
        )
        return {"data": rows}
    if path == "/api/stock_ohlc":
        sid = qs.get("id", [""])[0].strip()
        rows = q(
            "SELECT trade_date, close, stock_name FROM stock_daily "
            "WHERE stock_id=? AND trade_date >= date('now', ?) ORDER BY trade_date",
            (sid, f"-{days} day"),
        )
        name = rows[0][2] if rows else None
        if not name:
            got = q("SELECT stock_name FROM stocks WHERE stock_id=?", (sid,))
            name = got[0][0] if got else sid
        return {"id": sid, "name": name, "data": [[d, c] for d, c, _n in rows]}
    if path == "/api/alerts":
        return api_alerts(qs)
    if path == "/api/performance":
        return api_performance()
    if path == "/api/broker_branch/top":
        day = _ymd(qs, "date")
        try:
            k = int(qs.get("k", [str(broker_branch_mod.DEFAULT_K)])[0])
        except (TypeError, ValueError):
            k = broker_branch_mod.DEFAULT_K
        days_raw = (qs.get("days", [""])[0] or "").strip()
        days_n = _clamp_int(days_raw, 1, 1, 730) if days_raw.isdigit() else None
        return broker_branch_mod.top_branches(
            _request_conn.get(), day, k, days=days_n,
        )
    if path == "/api/broker_branch/broker":
        return broker_branch_mod.broker_stocks(
            _request_conn.get(),
            (qs.get("broker_id", [""])[0] or "").strip(),
            _ymd(qs, "date"),
        )
    if path == "/api/broker_branch/stock":
        return broker_branch_mod.stock_branches(
            _request_conn.get(),
            (qs.get("id", [""])[0] or qs.get("stock_id", [""])[0] or "").strip(),
            _ymd(qs, "date"),
        )
    if path == "/api/broker_branch/freshness":
        body = broker_branch_mod.freshness_payload(_request_conn.get())
        body.update(broker_branch_mod.ingest_status())
        body["data_mode"] = broker_branch_mod.data_mode(_request_conn.get())
        return body
    if path == "/api/scanner/chip_zscore":
        return chip_zscore_mod.api_chip_zscore(_request_conn.get(), qs)
    if path == "/api/scanner/broker_main_force":
        return broker_main_force_mod.api_broker_main_force(_request_conn.get(), qs)
    return None


STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML_PATH = STATIC_DIR / "index.html"
_STATIC_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_STATIC_CACHE_CONTROL = "private, max-age=300, must-revalidate"


def load_index_html() -> str:
    return INDEX_HTML_PATH.read_text(encoding="utf-8")


# Tests still read dashboard.HTML; the page itself is served from web/static/.
HTML = load_index_html()


def static_etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def static_content_type(path: Path) -> str:
    if path.suffix == ".html":
        return "text/html; charset=utf-8"
    if path.suffix == ".js":
        return "text/javascript; charset=utf-8"
    if path.suffix == ".css":
        return "text/css; charset=utf-8"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def resolve_static_path(url_path: str) -> Path | None:
    """Map / , /index.html , /static/<file> onto web/static/. None if unsafe/missing."""
    if url_path in ("/", "/index.html"):
        name = "index.html"
    elif url_path.startswith("/static/"):
        name = url_path[len("/static/"):]
        if "/" in name or "\\" in name or not _STATIC_NAME.fullmatch(name):
            return None
    else:
        return None
    try:
        path = (STATIC_DIR / name).resolve()
        path.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def read_static(url_path: str) -> tuple[bytes, str] | None:
    path = resolve_static_path(url_path)
    if path is None:
        return None
    return path.read_bytes(), static_content_type(path)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self._wrote_response = False
        super().setup()

    def _send_bytes(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self._wrote_response = True
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, obj, extra_headers=None):
        body = json.dumps(obj).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", extra_headers)

    def _reject_unauthorized(self):
        self._send_json(
            401,
            UNAUTHORIZED_JSON,
            {"WWW-Authenticate": f'Basic realm="{AUTH_REALM}"'},
        )

    def _reject_anonymous_disabled(self):
        self._send_json(403, ANONYMOUS_DISABLED_JSON)

    def _send_unexpected_error(self, path: str):
        _log.exception("unexpected error %s %s", self.command, path)
        if self._wrote_response:
            return
        self._send_json(500, INTERNAL_ERROR_JSON)

    def _drain_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length > 0:
            self.rfile.read(length)

    def _is_authorized(self, path: str) -> bool:
        if not path_requires_auth(path):
            return True
        if auth_enabled():
            creds = dashboard_credentials()
            return bool(
                creds
                and valid_basic_header(self.headers.get("Authorization"), creds[0], creds[1])
            )
        return allow_anonymous()

    def _reject_access(self):
        if auth_enabled():
            self._reject_unauthorized()
        else:
            self._reject_anonymous_disabled()

    def _authorize(self, path: str) -> bool:
        if self._is_authorized(path):
            return True
        self._reject_access()
        return False

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/health":
                body = json.dumps(health_payload()).encode("utf-8")
                self._send_bytes(200, body, "application/json; charset=utf-8")
                return
            if not self._authorize(u.path):
                return
            static = read_static(u.path)
            if static is not None:
                body, ctype = static
                etag = static_etag(body)
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self._wrote_response = True
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", _STATIC_CACHE_CONTROL)
                    self.end_headers()
                    return
                self._send_bytes(
                    200,
                    body,
                    ctype,
                    {"ETag": etag, "Cache-Control": _STATIC_CACHE_CONTROL},
                )
                return
            result = api(u.path, parse_qs(u.query))
            if result is None:
                self.send_response(404)
                self._wrote_response = True
                self.end_headers()
                return
            self._send_json(200, result)
        except Exception:
            self._send_unexpected_error(u.path)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if not self._is_authorized(u.path):
                self._drain_body()
                self._reject_access()
                return
            if u.path not in ("/api/backtest", "/api/backtest/stock"):
                self._drain_body()
                self.send_response(404)
                self._wrote_response = True
                self.end_headers()
                return
            if not _backtest_limiter.allow(client_ip(self), max_hits=backtest_max_hits()):
                self._drain_body()
                self._send_json(429, RATE_LIMITED_JSON)
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
                _log.warning("invalid JSON POST %s from %s", u.path, client_ip(self))
                self._send_json(400, INVALID_JSON)
                return
            if not isinstance(payload, dict):
                _log.warning("non-object JSON POST %s from %s", u.path, client_ip(self))
                self._send_json(400, INVALID_JSON)
                return
            _synthetic_handler_hold()
            if u.path == "/api/backtest/stock":
                conn = market_db.connect()
                try:
                    result = execute_stock_backtest(conn, payload)
                finally:
                    conn.close()
            else:
                conn = market_db.connect_for_backtest()
                try:
                    result = execute_index_backtest(conn, payload)
                finally:
                    conn.close()
            self._send_json(200, result)
        except Exception:
            _log.exception("unexpected error POST %s", u.path)
            if not self._wrote_response:
                self._send_json(500, BACKTEST_FAILED_JSON)

    def log_message(self, fmt, *args):
        try:
            msg = fmt % args
        except Exception:
            msg = " ".join(str(x) for x in (fmt,) + args)
        _log.info("%s %s", self.address_string(), str(msg).rstrip())

    def log_error(self, fmt, *args):
        try:
            msg = fmt % args
        except Exception:
            msg = " ".join(str(x) for x in (fmt,) + args)
        _log.error("%s %s", self.address_string(), str(msg).rstrip())


_MISSING_DATA = (
    "找不到市場資料。本機請先跑 python -m market.update_market_data;"
    " Cloud Run 請設 TURSO_DATABASE_URL 跟 TURSO_AUTH_TOKEN。"
)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Multi-threaded server so Cloud Run /health probes stay up during a backtest.

    A single-threaded HTTPServer would hold the only worker on POST /api/backtest
    and fail liveness/startup probes, recycling the revision. daemon_threads
    lets the process exit without waiting for a long backtest. Each request
    thread owns its own sqlite/Turso connection via ContextVar (_request_conn).
    """

    daemon_threads = True
    block_on_close = False


def make_server(host: str, port: int, handler=Handler):
    return ThreadingHTTPServer((host, port), handler)


def _ensure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )


def main() -> int:
    if not market_db.available():
        if not market_db.must_listen():
            raise SystemExit(_MISSING_DATA)
        print(_MISSING_DATA)
        print("仍會綁 PORT,讓 Cloud Run 探活通過;頁面與 API 在有資料前會是空的。")

    _ensure_logging()
    host, port = market_db.listen_host_port()
    try_ports = [port] if host == "0.0.0.0" else list(range(port, port + 10))
    server = None
    bound = port
    for candidate in try_ports:
        try:
            server = make_server(host, candidate)
            bound = candidate
            break
        except OSError as e:
            if host == "0.0.0.0" or not (
                e.errno == 48 or "Address already in use" in str(e)
            ):
                raise
    if server is None:
        raise SystemExit(
            f"連續 {len(try_ports)} 個埠({try_ports[0]}~{try_ports[-1]})都被占用。"
            f"本機可執行 `lsof -ti:{port} | xargs kill -9`。"
        )
    if bound != port:
        print(f"預設埠 {port} 已被占用,改用 {bound}。")

    url = f"http://{host}:{bound}"
    print(f"儀表板啟動:{url} source={'turso' if market_db.using_turso() else 'sqlite'}")
    if auth_enabled():
        print("HTTP Basic Auth 已啟用（DASHBOARD_USER / DASHBOARD_PASSWORD）。")
    elif allow_anonymous():
        print(
            "匿名模式（回測有限流）。本機預設；Cloud Run 請設帳密或 "
            "DASHBOARD_ALLOW_ANONYMOUS=1。"
        )
    else:
        print("未設帳密且未允許匿名：業務 API 已關閉（僅 GET /health）。")
    if market_db.should_open_browser():
        webbrowser.open(f"http://localhost:{bound}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
