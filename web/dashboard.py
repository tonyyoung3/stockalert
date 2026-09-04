#!/usr/bin/env python3
"""台股資料儀表板。

  python -m web.dashboard        # 本機 http://localhost:8765,讀 twse_data.db
  PORT=8080 python -m web.dashboard   # Cloud Run / 任何 PaaS

設了 TURSO_DATABASE_URL 跟 TURSO_AUTH_TOKEN 就讀雲端,否則讀本機 sqlite。
告警／績效：本機讀 screener.db 的 alerts、performance；Turso 則與市場表同一顆遠端 DB。
"""
import json
import re
import sqlite3
import webbrowser
from contextvars import ContextVar
from datetime import date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
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
from web import freshness as freshness_mod

_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# One sqlite/Turso connection per HTTP API request (Cloud Run round-trips are expensive).
_request_conn: ContextVar = ContextVar("dashboard_db_conn", default=None)


def q(sql, params=()):
    conn = _request_conn.get()
    if conn is not None:
        return conn.execute(sql, params).fetchall()
    return market_db.fetchall(sql, params)


def _ymd(qs, key):
    v = (qs.get(key, [""])[0] or "").strip()
    return v if _YMD.fullmatch(v) else None


_STOCK_ID = re.compile(r"^[0-9A-Za-z]{2,10}$")


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
    since = str(date.today() - timedelta(days=days))
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
    except Exception as e:
        body["freshness"] = {
            "stale": True,
            "empty": True,
            "tables": [],
            "error": str(e),
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
    return None


HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>台股資料儀表板</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-chart-financial@0.2.1"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.2.0"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{max-width:100%;}
body{font-family:-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif;background:#f8f9fa;color:#212529;line-height:1.5}
.wrap{max-width:1400px;margin:0 auto;padding:16px;min-width:0}
.sticky-top{position:sticky;top:0;z-index:50;background:#f8f9fa;padding-top:16px;margin-bottom:16px}
header{background:#1a1a2e;color:#fff;padding:18px 24px;border-radius:8px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
header h1{font-size:19px;font-weight:600}
select,input,button{max-width:100%}
select,input{padding:6px 10px;border:1px solid #ccc;border-radius:4px;font-size:13px}
header select{background:rgba(255,255,255,.1);color:#fff;border-color:rgba(255,255,255,.25)}
header select option{background:#1a1a2e}
.days-ctl{display:flex;flex-direction:column;align-items:flex-end;gap:4px}
header .days-hint{color:rgba(255,255,255,.72);max-width:340px;text-align:right;line-height:1.4}
.page-nav{display:flex;flex-wrap:wrap;gap:4px;background:#fff;border-radius:8px;padding:4px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.page-nav a{flex:1;min-width:72px;text-align:center;padding:10px 12px;border-radius:6px;text-decoration:none;color:#495057;font-size:14px;font-weight:600}
.page-nav a.is-active{background:#1a1a2e;color:#fff}
.page-nav a:focus-visible{outline:2px solid #4C72B0;outline-offset:2px}
.page-section{min-width:0}
.page-section[hidden]{display:none!important}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,200px),1fr));gap:16px;margin-bottom:16px}
.card{background:#fff;border-radius:8px;padding:18px 22px;box-shadow:0 1px 3px rgba(0,0,0,.08);min-width:0}
.kpi-label{font-size:12px;color:#6c757d;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.kpi-value{font-size:26px;font-weight:700;overflow-wrap:anywhere}
.kpi-sub{font-size:13px;color:#6c757d}
.kpi-warn{background:#fff8e8;box-shadow:0 0 0 2px #e0a800}
.kpi-warn .kpi-value{color:#c0392b}
.fresh-banner{padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:14px;font-weight:600;line-height:1.55}
.fresh-banner.is-stale{background:#fff3cd;border:2px solid #d39e00;color:#6b4f00}
.fresh-banner.is-empty{background:#fdecea;border:2px solid #c0392b;color:#7b241c}
.chart-empty{margin-top:8px;padding:20px 14px;background:#fdecea;border:1px solid #f5c2c0;border-radius:6px;color:#922b21;font-size:14px;font-weight:600;text-align:center}
.pos{color:#c0392b}.neg{color:#27ae60} /* 台股習慣:紅漲綠跌 */
.charts{display:grid;grid-template-columns:1fr;gap:16px;margin-bottom:16px;min-width:0}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:16px;margin-bottom:16px;min-width:0}
.two>div{min-width:0}
.card h3{font-size:14px;font-weight:600;margin-bottom:14px}
.chart-head{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:14px;overflow:visible}
.chart-head h3{margin:0}
.hint{font-size:12px;color:#adb5bd}
.reset-btn{padding:3px 10px;font-size:12px;border:1px solid #dee2e6;border-radius:4px;background:#fff;color:#6c757d;cursor:pointer}
.reset-btn:hover{background:#f0f0f0}
.top-range{display:flex;align-items:center;flex-wrap:wrap;gap:8px;overflow:visible;min-width:0}
.chart-head > span{display:flex;flex-wrap:wrap;gap:8px;align-items:center;min-width:0}
.top-range input[type="date"]{min-width:9.5em;height:44px!important;min-height:44px;max-height:44px;flex:1 1 9.5em;font-size:16px;padding:8px 10px;-webkit-appearance:none;appearance:none}
.ov-wrap{display:flex;align-items:center;flex-wrap:wrap;gap:8px;flex:1 1 220px;min-width:0}
.ov-search{position:relative;flex:1 1 220px;min-width:0;max-width:100%}
.ov-search input{width:100%;min-width:0}
.ov-menu{position:absolute;z-index:60;left:0;right:0;top:calc(100% + 2px);background:#fff;border:1px solid #dee2e6;border-radius:4px;max-height:260px;overflow:auto;box-shadow:0 6px 16px rgba(0,0,0,.12);-webkit-overflow-scrolling:touch}
.ov-menu button{display:flex;justify-content:space-between;align-items:center;gap:8px;width:100%;text-align:left;padding:12px 10px;min-height:44px;border:none;background:#fff;cursor:pointer;font-size:13px;touch-action:manipulation}
.ov-menu button:hover,.ov-menu button.active{background:#eef3fa}
.ov-chip{font-size:11px;color:#6c757d;white-space:nowrap}
canvas{display:block;max-width:100%;max-height:320px}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 10px;border-bottom:2px solid #dee2e6;color:#6c757d;font-size:12px;white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid #f0f0f0}
tr:hover td{background:#f8f9fa}
.num{text-align:right;font-variant-numeric:tabular-nums}
.search{display:flex;gap:8px;margin-bottom:14px;align-items:flex-start}
.search button{padding:6px 16px;border:none;border-radius:4px;background:#4C72B0;color:#fff;cursor:pointer}
.sid-search{position:relative;flex:1;max-width:360px;min-width:0}
.sid-search input{width:100%}
.empty{color:#6c757d;font-size:13px;padding:8px 0}
.ticker-link{color:#4C72B0;text-decoration:none;font-weight:600;cursor:pointer;background:none;border:none;padding:0;font:inherit}
.ticker-link:hover{text-decoration:underline}
.assumptions{font-size:12px;color:#6c757d;margin-top:10px;line-height:1.45}
.pat{font-size:12px;color:#495057}
footer{text-align:center;color:#adb5bd;font-size:12px;padding:12px}
.bt-grid{display:grid;grid-template-columns:1fr;gap:14px}
.bt-box{border:1px solid #eee;border-radius:6px;padding:12px 14px}
.bt-label{font-size:12px;font-weight:600;color:#6c757d;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
details.bt-box>summary.bt-label{cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;gap:8px;user-select:none}
details.bt-box>summary.bt-label::-webkit-details-marker{display:none}
details.bt-box>summary.bt-label::after{content:"▾";font-size:12px;color:#adb5bd;flex-shrink:0}
details.bt-box:not([open])>summary.bt-label{margin-bottom:0}
details.bt-box:not([open])>summary.bt-label::after{content:"▸"}
.bt-row{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.bt-row:last-child{margin-bottom:0}
.bt-sub{font-size:12px;color:#6c757d;margin-right:2px}
.bt-warn{margin-top:8px;padding:8px 10px;background:#fff3cd;border:1px solid #ffe08a;border-radius:4px;font-size:12.5px;color:#7a5c00}
.bt-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,150px),1fr));gap:12px;margin-bottom:14px}
.bt-kpi{background:#f8f9fa;border-radius:6px;padding:10px 14px;min-width:0}
.bt-kpi .l{font-size:11px;color:#6c757d;text-transform:uppercase}
.bt-kpi .v{font-size:20px;font-weight:700}
.bt-section-title{font-size:13px;font-weight:600;margin:16px 0 8px}
.bt-error{padding:12px;background:#fdecea;border:1px solid #f5c2c0;border-radius:4px;color:#a94442}
@media(max-width:768px){
  .wrap{padding:12px}
  header{flex-direction:column;align-items:stretch;padding:14px 16px}
  header h1{font-size:17px}
  .days-ctl{align-items:stretch}
  header .days-hint{text-align:left;max-width:none}
  .page-nav a{min-width:0;padding:12px 8px}
  .kpis,.two,.bt-kpis{grid-template-columns:1fr}
  .card{padding:14px 14px}
  .chart-head{flex-direction:column;align-items:stretch}
  .chart-head > span{flex-direction:column;align-items:stretch}
  .ov-wrap,.top-range,.search{flex-direction:column;align-items:stretch;flex:none;width:100%}
  .ov-search,.sid-search{max-width:none;width:100%;flex:none}
  .ov-search input,.sid-search input,.search button,.top-range select,.top-range input[type="date"]{width:100%;min-width:0}
  .top-range input[type="date"]{width:100%;min-width:0;height:44px;min-height:44px;font-size:16px}
  select,input:not([type="checkbox"]):not([type="radio"]){font-size:16px}
  .reset-btn,.search button,.page-nav a{min-height:44px}
  .bt-row{flex-direction:column;align-items:stretch}
  .bt-row select,.bt-row input[type="number"],.bt-row input:not([type="checkbox"]):not([type="radio"]){width:100%!important}
  .bt-box label{display:block;margin:4px 0}
  .bt-box label[style]{margin-left:0!important}
  #bt-form button{width:100%;margin-left:0!important;min-height:44px}
  .kpi-value{font-size:22px}
}
</style>
</head>
<body>
<div class="wrap">
<div class="sticky-top">
<header>
  <h1>台股資料儀表板</h1>
  <div class="days-ctl">
    <div>
      <label for="days">顯示範圍</label>
      <select id="days" onchange="loadAll()">
        <option value="30">近 30 天</option>
        <option value="90" selected>近 90 天</option>
        <option value="365">近 1 年</option>
        <option value="730">近 2 年</option>
      </select>
    </div>
    <p class="hint days-hint">全域天數影響指數／籌碼／個股圖；外資排行用自己的日期區間。</p>
  </div>
</header>
<nav class="page-nav" role="tablist" aria-label="儀表板分區">
  <a href="#market" id="tab-market" class="is-active" role="tab" data-section="market" aria-controls="section-market" aria-selected="true">市場</a>
  <a href="#stock" id="tab-stock" role="tab" data-section="stock" aria-controls="section-stock" aria-selected="false">個股</a>
  <a href="#alerts" id="tab-alerts" role="tab" data-section="alerts" aria-controls="section-alerts" aria-selected="false">告警</a>
  <a href="#backtest" id="tab-backtest" role="tab" data-section="backtest" aria-controls="section-backtest" aria-selected="false">回測</a>
</nav>
</div>

<div id="fresh-banner" class="fresh-banner" hidden role="alert"></div>

<section class="kpis" id="fresh-kpis">
  <div class="card" id="fk-foreign_daily"><div class="kpi-label">外資最後交易日</div><div class="kpi-value">–</div><div class="kpi-sub">foreign_daily</div></div>
  <div class="card" id="fk-stock_daily"><div class="kpi-label">個股日K最後交易日</div><div class="kpi-value">–</div><div class="kpi-sub">stock_daily</div></div>
  <div class="card" id="fk-taifex"><div class="kpi-label">台指期最後交易日</div><div class="kpi-value">–</div><div class="kpi-sub">taifex_fut_oi</div></div>
  <div class="card" id="fk-alerts"><div class="kpi-label">告警最後日期</div><div class="kpi-value">–</div><div class="kpi-sub">alerts</div></div>
</section>

<div id="section-market" class="page-section" role="tabpanel" aria-labelledby="tab-market">
<section class="kpis">
  <div class="card"><div class="kpi-label">加權指數(最新)</div><div class="kpi-value" id="k-idx">–</div><div class="kpi-sub" id="k-chg"></div></div>
  <div class="card"><div class="kpi-label">外資合計買賣超</div><div class="kpi-value" id="k-net">–</div><div class="kpi-sub" id="k-netdate"></div></div>
  <div class="card"><div class="kpi-label">收錄個股數</div><div class="kpi-value" id="k-cnt">–</div><div class="kpi-sub">最新交易日</div></div>
  <div class="card"><div class="kpi-label">資料期間</div><div class="kpi-value" id="k-span" style="font-size:16px">–</div><div class="kpi-sub">foreign_daily</div></div>
</section>

<section class="charts">
  <div class="card">
    <div class="chart-head"><h3>加權指數 K 線(開高低收)</h3>
      <span>
        <select id="kint" onchange="loadKline()" style="margin-right:8px">
          <option value="day" selected>日 K</option>
          <option value="hour">小時 K</option>
        </select>
        <span class="hint">拖曳／雙指縮放　</span><button class="reset-btn" onclick="resetZoom('c-kline')">重置</button></span></div>
    <canvas id="c-kline"></canvas></div>
  <div class="card">
    <div class="chart-head"><h3>加權指數走勢(每小時)</h3>
      <span class="ov-wrap">
        <div class="ov-search">
          <input id="ov-q" placeholder="搜尋疊圖: 2330、台積電、外資、融資" autocomplete="off"
                 oninput="onOverlayInput()" onfocus="onOverlayFocus()" onkeydown="onOverlayKey(event)">
          <div id="ov-menu" class="ov-menu" hidden></div>
        </div>
        <button type="button" id="ov-clear" class="reset-btn" onclick="clearOverlay()" hidden>清除疊圖</button>
        <span class="hint">拖曳／雙指縮放　</span><button class="reset-btn" onclick="resetZoom('c-taiex')">重置</button></span></div>
    <canvas id="c-taiex"></canvas></div>
  <div class="card">
    <div class="chart-head"><h3>台指期未平倉:外資 / 投信 淨額口數 與比值</h3>
      <span><span class="hint">虛線為外資÷投信比值(右軸,恆為負)　拖曳／雙指縮放　</span><button class="reset-btn" onclick="resetZoom('c-taifex')">重置</button></span></div>
    <canvas id="c-taifex"></canvas></div>
  <div class="card">
    <div class="chart-head"><h3>整體融資融券餘額</h3>
      <span><span class="hint">拖曳／雙指縮放　</span><button class="reset-btn" onclick="resetZoom('c-margin')">重置</button></span></div>
    <canvas id="c-margin"></canvas></div>
  <div class="card">
    <div class="chart-head"><h3>外資每日合計買賣超(張)</h3>
      <span><span class="hint">拖曳／雙指縮放　</span><button class="reset-btn" onclick="resetZoom('c-net')">重置</button></span></div>
    <canvas id="c-net"></canvas></div>
</section>

<section class="card" style="margin-bottom:16px">
  <div class="chart-head">
    <h3>外資買賣超排行</h3>
    <span class="top-range">
      <select id="top-preset" onchange="onTopPreset()">
        <option value="1" selected>當日</option>
        <option value="5">近 5 日累計</option>
        <option value="20">近 20 日累計</option>
        <option value="60">近 60 日累計</option>
        <option value="custom">自訂區間</option>
      </select>
      <input type="date" id="top-start" onchange="onTopDates()" aria-label="起始日期">
      <span class="hint">～</span>
      <input type="date" id="top-end" onchange="onTopDates()" aria-label="結束日期">
      <span class="hint">此區間只控制排行，與上方全域天數無關。點長條可開啟「個股」分頁</span>
    </span>
  </div>
  <div class="two" style="margin-bottom:0">
    <div><h3 id="t-buy">外資買超前 15</h3><canvas id="c-buy"></canvas></div>
    <div><h3 id="t-sell">外資賣超前 15</h3><canvas id="c-sell"></canvas></div>
  </div>
</section>
</div>

<div id="section-stock" class="page-section" role="tabpanel" aria-labelledby="tab-stock" hidden>
<section class="card" style="margin-bottom:16px" id="stock-lookup">
  <div class="chart-head"><h3>個股外資買賣超查詢</h3>
    <span><span class="hint">拖曳／雙指縮放　</span><button class="reset-btn" onclick="resetZoom('c-stock')">重置</button></span></div>
  <div class="search">
    <div class="sid-search" id="sid-search">
      <input id="sid" placeholder="代號或名稱,例如 2330、台積電" autocomplete="off"
             oninput="onStockInput()" onfocus="onStockFocus()" onkeydown="onStockKey(event)">
      <div id="sid-menu" class="ov-menu" hidden></div>
    </div>
    <button type="button" onclick="submitStockSearch()">查詢</button>
  </div>
  <canvas id="c-stock" style="display:none"></canvas>
  <canvas id="c-stock-margin" style="display:none;margin-top:16px"></canvas>
  <div id="stock-table" class="table-scroll"></div>
</section>
</div>

<div id="section-alerts" class="page-section" role="tabpanel" aria-labelledby="tab-alerts" hidden>
<section class="two" style="margin-bottom:16px">
  <div class="card">
    <div class="chart-head">
      <h3>今日／近期告警</h3>
      <select id="alert-days" onchange="loadAlerts()">
        <option value="7">近 7 天</option>
        <option value="30" selected>近 30 天</option>
        <option value="90">近 90 天</option>
      </select>
    </div>
    <div id="alerts-box" class="table-scroll"><p class="empty">尚無告警</p></div>
  </div>
  <div class="card">
    <div class="chart-head"><h3>績效摘要</h3></div>
    <div id="perf-box" class="table-scroll"><p class="empty">尚未結算</p></div>
  </div>
</section>
</div>

<div id="section-backtest" class="page-section" role="tabpanel" aria-labelledby="tab-backtest" hidden>
<section class="card" style="margin-bottom:16px">
  <h3 style="margin-bottom:8px">策略回測</h3>
  <p class="hint" style="margin-bottom:14px">此區與市場圖表分開；首次進入不會自動執行。篩選／進場／出場可折疊（手機預設收合，避免首屏被表單佔滿）。</p>

  <div class="bt-grid" id="bt-form">
    <details class="bt-box" data-bt-fold="dataset" open>
      <summary class="bt-label">資料集</summary>
      <select id="bt-dataset" onchange="btOnDatasetChange()">
        <option value="2y_hourly" selected>2年小時K(支援日內事件)</option>
        <option value="15y_daily">15年日K(只能整天賭注,見下方警告)</option>
      </select>
      <div id="bt-stale-warning" class="bt-warn" style="display:none">
        ⚠️ 官方日K開盤價 99.7% 等於前一天收盤價(陳舊開盤價陷阱),用它模擬「日內事件觸發」是幻覺。
        這個資料集只開放「隔夜模式」(前一天收盤進場、隔日出場),日內模式已停用。
      </div>
    </details>

    <details class="bt-box" data-bt-fold="filters" open>
      <summary class="bt-label">篩選條件(用「前一交易日已知」的資訊,不含未來資訊)</summary>
      <div class="bt-row">
        <span class="bt-sub">星期</span>
        <label><input type="checkbox" class="bt-dow" value="0" checked>一</label>
        <label><input type="checkbox" class="bt-dow" value="1" checked>二</label>
        <label><input type="checkbox" class="bt-dow" value="2" checked>三</label>
        <label><input type="checkbox" class="bt-dow" value="3" checked>四</label>
        <label><input type="checkbox" class="bt-dow" value="4" checked>五</label>
      </div>
      <div class="bt-row">
        <span class="bt-sub">趨勢濾網</span>
        <select id="bt-trend">
          <option value="none" selected>不篩</option>
          <option value="above_ma20">前收 > MA20</option>
          <option value="below_ma20">前收 < MA20</option>
          <option value="above_ma60">前收 > MA60(季線)</option>
          <option value="below_ma60">前收 < MA60(季線)</option>
          <option value="above_ma20_today">今收 > MA20(今日)</option>
          <option value="below_ma20_today">今收 < MA20(今日)</option>
          <option value="above_ma60_today">今收 > MA60/季線(今日)</option>
          <option value="below_ma60_today">今收 < MA60/季線(今日)</option>
        </select>
        <span class="bt-sub">前一日漲跌</span>
        <select id="bt-prevday">
          <option value="none" selected>不篩</option>
          <option value="up">前一日上漲</option>
          <option value="down">前一日下跌</option>
        </select>
      </div>
      <div class="bt-row">
        <span class="bt-sub">當日跳空方向</span>
        <select id="bt-gapdir">
          <option value="any" selected>不篩</option>
          <option value="up">跳空漲</option>
          <option value="down">跳空跌</option>
        </select>
        <span class="bt-sub">最小 |跳空| %</span>
        <input type="number" id="bt-gapmin" value="0" step="0.1" style="width:70px">
      </div>
      <div class="bt-row">
        <span class="bt-sub">當日漲跌方向(收盤才確定,適合收盤進場規則)</span>
        <select id="bt-dayretdir">
          <option value="any" selected>不篩</option>
          <option value="up">當日上漲</option>
          <option value="down">當日下跌</option>
        </select>
        <span class="bt-sub">最小 |當日漲跌| %</span>
        <input type="number" id="bt-dayretmin" value="0" step="0.1" style="width:70px">
      </div>
      <div class="bt-row">
        <span class="bt-sub">均線交叉(MA20 vs MA60,收盤才確定)</span>
        <select id="bt-macross">
          <option value="none" selected>不篩</option>
          <option value="golden">黃金交叉(今天)</option>
          <option value="death">死亡交叉(今天)</option>
        </select>
        <span class="bt-sub">N日新高/新低突破(收盤才確定)</span>
        <select id="bt-breakout">
          <option value="none" selected>不篩</option>
          <option value="n_day_high">創N日新高</option>
          <option value="n_day_low">破N日新低</option>
        </select>
        <span class="bt-sub">N=</span>
        <input type="number" id="bt-breakoutwindow" value="20" step="1" min="2" style="width:60px">
      </div>
      <div class="bt-row">
        <span class="bt-sub">外資/投信台指期未平倉比(前一交易日值,只有近~3年TAIFEX資料)</span>
        <select id="bt-oiratio">
          <option value="none" selected>不篩</option>
          <option value="below_pctile">低於分位門檻(比值相對更負)</option>
          <option value="above_pctile">高於分位門檻(比值相對較不負)</option>
        </select>
        <span class="bt-sub">分位門檻(0-100)</span>
        <input type="number" id="bt-oipctile" value="25" step="1" min="0" max="100" style="width:60px">
        <span class="bt-sub">回看天數</span>
        <input type="number" id="bt-oiwindow" value="60" step="5" min="10" style="width:60px">
      </div>
      <p class="hint" id="bt-close-decided-hint" style="display:none">⚠️ 均線交叉 / N日新高新低突破 / 當日漲跌 / 今日均線 這幾種濾網要等收盤才能確定,不能用在「日內模式」(會偷看未來資訊)。切到隔夜或波段模式才能套用。</p>
    </details>

    <details class="bt-box" data-bt-fold="entry" open>
      <summary class="bt-label">模式</summary>
      <label><input type="radio" name="bt-mode" value="intraday" checked onchange="btOnModeChange()"> 日內(當天進出)</label>
      <label style="margin-left:14px"><input type="radio" name="bt-mode" value="overnight" onchange="btOnModeChange()"> 隔夜(收盤進、隔日出)</label>
      <label style="margin-left:14px"><input type="radio" name="bt-mode" value="swing" onchange="btOnModeChange()"> 波段(收盤進、固定%停損、可多日持有)</label>
    </details>

    <details class="bt-box" id="bt-intraday-box" data-bt-fold="entry" open>
      <summary class="bt-label">日內進場／出場</summary>
      <div class="bt-row">
        <span class="bt-sub">進場參考價</span>
        <select id="bt-ref">
          <option value="first_hour_high">第一小時高點</option>
          <option value="first_hour_low">第一小時低點</option>
          <option value="day_open">當日開盤價</option>
          <option value="prev_close">前一日收盤價</option>
        </select>
        <span class="bt-sub">偏移 %(可負,例如回檔0.5%填-0.5)</span>
        <input type="number" id="bt-offset" value="0" step="0.1" style="width:80px">
      </div>
      <div class="bt-row">
        <span class="bt-sub">觸發方式</span>
        <select id="bt-trigger">
          <option value="touch_from_below">價格向下觸及(跌破/回檔)</option>
          <option value="touch_from_above">價格向上觸及(突破)</option>
        </select>
        <span class="bt-sub">方向</span>
        <select id="bt-direction">
          <option value="long">做多</option>
          <option value="short">做空</option>
        </select>
      </div>
      <div class="bt-row">
        <span class="bt-sub">最早檢查時間</span>
        <select id="bt-earliest">
          <option value="9">09:00</option>
          <option value="10" selected>10:00</option>
          <option value="11">11:00</option>
          <option value="12">12:00</option>
        </select>
        <span class="bt-sub">出場時間</span>
        <select id="bt-exithour">
          <option value="10">10:00收盤</option>
          <option value="11">11:00收盤</option>
          <option value="12">12:00收盤</option>
          <option value="13" selected>當日收盤(13:30)</option>
        </select>
      </div>
      <div class="bt-row">
        <label><input type="checkbox" id="bt-stop-on" onchange="btOnStopToggle()"> 啟用停損</label>
      </div>
      <div class="bt-row" id="bt-stop-box" style="display:none">
        <span class="bt-sub">停損參考價</span>
        <select id="bt-stopref">
          <option value="day_open">當日開盤價</option>
          <option value="entry_price">進場價</option>
          <option value="first_hour_high">第一小時高點</option>
          <option value="first_hour_low">第一小時低點</option>
        </select>
        <span class="bt-sub">偏移 %</span>
        <input type="number" id="bt-stopoffset" value="0" step="0.1" style="width:80px">
      </div>
    </details>

    <details class="bt-box" id="bt-overnight-box" data-bt-fold="exit" style="display:none" open>
      <summary class="bt-label">隔夜出場</summary>
      <div class="bt-row">
        <span class="bt-sub">方向</span>
        <select id="bt-on-direction">
          <option value="long">做多</option>
          <option value="short">做空</option>
        </select>
        <span class="bt-sub">出場時機</span>
        <select id="bt-holdto">
          <option value="next_open">隔日開盤</option>
          <option value="next_close">隔日收盤</option>
          <option value="next_hour">隔日某小時收盤(僅2年小時K)</option>
        </select>
        <span class="bt-sub" id="bt-holdhour-label" style="display:none">小時</span>
        <select id="bt-holdhour" style="display:none">
          <option value="10" selected>10:00</option>
          <option value="11">11:00</option>
          <option value="12">12:00</option>
        </select>
      </div>
      <div class="bt-row">
        <label><input type="checkbox" id="bt-skipweekend" checked> 跳過週末(週五收盤不留倉)</label>
      </div>
    </details>

    <details class="bt-box" id="bt-swing-box" data-bt-fold="exit" style="display:none" open>
      <summary class="bt-label">波段進場／出場(收盤進場,固定 % 停損)</summary>
      <div class="bt-row">
        <span class="bt-sub">方向</span>
        <select id="bt-swing-direction">
          <option value="long">做多</option>
          <option value="short">做空</option>
        </select>
        <span class="bt-sub">停損 %(相對進場價)</span>
        <input type="number" id="bt-swing-stoppct" value="2" step="0.1" min="0.1" style="width:70px">
        <span class="bt-sub">最長持有天數</span>
        <input type="number" id="bt-swing-maxhold" value="60" step="1" min="1" style="width:70px">
      </div>
      <div class="bt-row">
        <label><input type="checkbox" id="bt-swing-tpon" onchange="btOnSwingTpToggle()"> 啟用停利</label>
        <span class="bt-sub" id="bt-swing-tp-label" style="display:none">停利 %(相對進場價)</span>
        <input type="number" id="bt-swing-tppct" value="5" step="0.1" min="0.1" style="width:70px; display:none">
      </div>
      <p class="hint">同一天停損停利都可能觸發時,保守假設先停損。訊號出現在資料尾端、還沒等到出場資料就用完的交易會被排除(不計入統計),不會用未知結果硬猜。</p>
    </details>

    <div class="bt-box" data-bt-fold="cost">
      <div class="bt-row">
        <span class="bt-sub">來回成本 %</span>
        <input type="number" id="bt-cost" value="0.03" step="0.01" style="width:80px">
        <button onclick="runBacktest()" style="margin-left:16px;padding:8px 24px;background:#4C72B0;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:14px">執行回測</button>
      </div>
    </div>
  </div>

  <div id="bt-results" class="table-scroll" style="margin-top:20px"></div>
</section>
</div>

<footer>資料來源:台灣證券交易所 · 買賣超單位:張(千股)</footer>
</div>

<script>
const charts = {};
const ZOOM = {pan:{enabled:true, mode:'x'},
              zoom:{wheel:{enabled:true}, pinch:{enabled:true}, mode:'x'},
              limits:{x:{minRange:5}}};
function resetZoom(id){ if(charts[id]) charts[id].resetZoom(); }
const fmt = n => n==null ? '–' : n.toLocaleString('zh-TW');
const zhang = n => Math.round(n/1000);  // 股 -> 張

function mk(id, cfg){ if(charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), cfg); return charts[id]; }

async function j(u){
  const resp = await fetch(u);
  if(!resp.ok) throw new Error('HTTP '+resp.status);
  return resp.json();
}
const days = () => document.getElementById('days').value;
const EMPTY_MARKET = '尚無資料。請跑 python -m market.update_market_data';
const PAGE_SECTIONS = ['market','stock','alerts','backtest'];

function parseSectionHash(hash){
  let h = (hash==null ? location.hash : hash).replace(/^#/, '').split('?')[0];
  if(h.indexOf('section-')===0) h = h.slice(8);
  return PAGE_SECTIONS.indexOf(h)>=0 ? h : '';
}
function resolveSection(){
  return parseSectionHash() || (parseStockQuery(location.search) ? 'stock' : 'market');
}
function resizeCharts(){
  Object.keys(charts).forEach(id=>{
    try{ if(charts[id]) charts[id].resize(); }catch(e){}
  });
}
function showSection(name, opts){
  opts = opts || {};
  if(PAGE_SECTIONS.indexOf(name)<0) name = 'market';
  PAGE_SECTIONS.forEach(s=>{
    const el = document.getElementById('section-'+s);
    if(el) el.hidden = s!==name;
  });
  document.querySelectorAll('.page-nav [data-section]').forEach(btn=>{
    const on = btn.dataset.section===name;
    btn.classList.toggle('is-active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  if(opts.updateHash !== false){
    const u = new URL(location.href);
    history.replaceState(null, '', u.pathname + u.search + '#'+name);
  }
  requestAnimationFrame(resizeCharts);
}
document.querySelector('.page-nav').addEventListener('click', ev=>{
  const a = ev.target.closest('[data-section]');
  if(!a) return;
  ev.preventDefault();
  showSection(a.dataset.section);
});
window.addEventListener('hashchange', ()=> showSection(resolveSection(), {updateHash:false}));

function setChartEmpty(id, msg){
  if(charts[id]){ charts[id].destroy(); delete charts[id]; }
  const canvas = document.getElementById(id);
  if(!canvas) return;
  canvas.style.display = 'none';
  let el = canvas.parentElement.querySelector(':scope > .chart-empty');
  if(!el){
    el = document.createElement('div');
    el.className = 'chart-empty';
    canvas.insertAdjacentElement('afterend', el);
  }
  el.hidden = false;
  el.textContent = msg || EMPTY_MARKET;
}
function setChartReady(id){
  const canvas = document.getElementById(id);
  if(!canvas) return;
  canvas.style.display = '';
  const el = canvas.parentElement.querySelector(':scope > .chart-empty');
  if(el) el.hidden = true;
}

function renderFreshness(f){
  if(!f) return;
  (f.tables||[]).forEach(t=>{
    const card = document.getElementById('fk-'+t.table);
    if(!card) return;
    const val = card.querySelector('.kpi-value');
    const sub = card.querySelector('.kpi-sub');
    val.textContent = t.last_date || '無資料';
    if(t.empty){
      sub.textContent = t.table+' · 空白表';
    } else if(t.days_ago==0){
      sub.textContent = t.table+' · 今天';
    } else {
      sub.textContent = t.table+' · 距今 '+t.days_ago+' 天';
    }
    card.classList.toggle('kpi-warn', !!(t.stale || t.empty));
  });
  const banner = document.getElementById('fresh-banner');
  if(!banner) return;
  if(f.empty || f.stale){
    const bad = (f.tables||[]).filter(t=>t.empty||t.stale);
    const parts = bad.map(t=> t.empty ? (t.table+' 空白') : (t.table+' 最後日 '+t.last_date+'（距今 '+t.days_ago+' 天）'));
    const marketBad = bad.some(t=>t.table!=='alerts');
    const action = marketBad ? '請跑 python -m market.update_market_data' : '請確認篩選排程 python -m notify.screener';
    banner.hidden = false;
    banner.className = 'fresh-banner ' + (f.empty ? 'is-empty' : 'is-stale');
    banner.textContent = '⚠️ 資料'+(f.empty?'空白':'過期（早於上一個台股交易日）')+'：'
      +parts.join('；')+'。'+action+'（'+ (f.calendar_note||'週末／未內建國定假日')+'）。';
  } else {
    banner.hidden = true;
    banner.className = 'fresh-banner';
    banner.textContent = '';
  }
}
const PAT = {upper_shadow_reversal:'上影線反轉', inside_day:'Inside Day'};
function patName(p){ return PAT[p] || p || '–'; }
function pct(n){ return n==null ? '–' : ((n>=0?'+':'')+Number(n).toFixed(2)+'%'); }
function money(n){ return n==null ? '–' : Number(n).toLocaleString('zh-TW', {maximumFractionDigits:2}); }

let stockId = '';
let sidHits = [];
let sidActive = -1;
let sidTimer = null;

function parseStockQuery(search){
  try{
    const v = new URLSearchParams(search || location.search).get('stock');
    return parseStockId(v||'');
  }catch(e){ return ''; }
}
function parseStockId(raw){
  const token = ((raw||'').trim().split(/\\s+/)[0] || '');
  return /^[0-9A-Za-z]{2,10}$/.test(token) ? token : '';
}
function syncStockUrl(id){
  try{
    const u = new URL(location.href);
    if(id) u.searchParams.set('stock', id);
    else u.searchParams.delete('stock');
    history.replaceState(null, '', u.pathname + u.search + u.hash);
  }catch(e){}
}
function selectStock(id, name, opts){
  opts = opts || {};
  id = (id||'').trim();
  if(!id) return;
  stockId = id;
  const el = document.getElementById('sid');
  if(el) el.value = name ? (id+' '+name) : id;
  hideStockMenu();
  if(opts.section !== false) showSection('stock');
  syncStockUrl(id);
  if(opts.scroll !== false){
    const box = document.getElementById('stock-lookup');
    if(box) box.scrollIntoView({behavior:'smooth', block:'start'});
    if(el) el.focus();
  }
  if(opts.load !== false) loadStock();
}
function showStock(id, name){ selectStock(id, name); }

function bindSuggestPick(menu, items, pick){
  menu.querySelectorAll('button').forEach(btn=>{
    btn.addEventListener('mousedown', ev=> ev.preventDefault());
    btn.addEventListener('click', ev=>{
      ev.preventDefault();
      pick(items[+btn.dataset.i]);
    });
  });
}
function hideStockMenu(){
  const menu = document.getElementById('sid-menu');
  if(menu) menu.hidden = true;
  sidActive = -1;
}
async function fetchStockHits(q){
  const t = (q||'').trim();
  if(!t) return [];
  const r = await j('/api/stocks?q='+encodeURIComponent(t));
  return (r.data||[]).map(row=>({id:row[0], name:row[1]||row[0]}));
}
function renderStockMenu(items){
  sidHits = items;
  if(sidActive >= items.length) sidActive = items.length ? 0 : -1;
  const menu = document.getElementById('sid-menu');
  if(!menu) return;
  if(!items.length){
    menu.innerHTML = '<div class="hint" style="padding:8px 10px">查無此股</div>';
    menu.hidden = false;
    return;
  }
  menu.innerHTML = items.map((it,i)=>
    '<button type="button" class="'+(i===sidActive?'active':'')+'" data-i="'+i+'">'
    +'<span>'+esc(it.id+' '+(it.name||''))+'</span></button>'
  ).join('');
  menu.hidden = false;
  bindSuggestPick(menu, items, pickStock);
}
function pickStock(item){
  if(!item) return;
  selectStock(item.id, item.name);
}
function onStockFocus(){
  if(stockId) return;
  onStockInput();
}
function onStockInput(){
  const q = document.getElementById('sid').value;
  stockId = '';
  clearTimeout(sidTimer);
  if(!q.trim()){ hideStockMenu(); syncStockUrl(''); return; }
  sidTimer = setTimeout(async ()=>{
    sidActive = -1;
    renderStockMenu(await fetchStockHits(q));
  }, 160);
}
function onStockKey(ev){
  const menu = document.getElementById('sid-menu');
  if(ev.key==='Escape'){ hideStockMenu(); return; }
  if(ev.key==='ArrowDown' || ev.key==='ArrowUp'){
    ev.preventDefault();
    if(!menu || menu.hidden){ onStockInput(); return; }
    if(!sidHits.length) return;
    const dir = ev.key==='ArrowDown' ? 1 : -1;
    sidActive = (sidActive + dir + sidHits.length) % sidHits.length;
    renderStockMenu(sidHits);
    const el = menu.querySelector('button.active');
    if(el) el.scrollIntoView({block:'nearest'});
    return;
  }
  if(ev.key==='Enter'){
    ev.preventDefault();
    submitStockSearch();
  }
}
async function submitStockSearch(){
  const q = (document.getElementById('sid').value||'').trim();
  if(!q){ hideStockMenu(); return; }
  const menu = document.getElementById('sid-menu');
  if(menu && !menu.hidden && sidHits.length){
    pickStock(sidHits[sidActive<0?0:sidActive]);
    return;
  }
  const hits = await fetchStockHits(q);
  if(hits.length){ pickStock(hits[0]); return; }
  const direct = parseStockId(q);
  if(direct){ selectStock(direct); return; }
  renderStockMenu([]);
}

async function loadAlerts(){
  const box = document.getElementById('alerts-box');
  if(!box) return;
  try{
    const n = document.getElementById('alert-days').value;
    const r = await j('/api/alerts?days='+encodeURIComponent(n));
    if(!r || r.empty || !(r.data||[]).length){
      box.innerHTML = '<p class="empty">尚無告警</p>';
      return;
    }
    box.innerHTML = '<table><thead><tr><th>日期</th><th>代號</th><th>名稱</th><th>型態</th><th class="num">告警價</th><th>題材</th></tr></thead><tbody>'
      + r.data.map(a => '<tr><td>'+esc(a.alert_date||'')+'</td>'
        +'<td><button type="button" class="ticker-link" data-ticker="'+esc(a.ticker||'')+'">'+esc(a.ticker||'')+'</button></td>'
        +'<td>'+esc(a.name||'–')+'</td>'
        +'<td class="pat">'+esc(patName(a.pattern_type))+'</td>'
        +'<td class="num">'+money(a.price_at_alert)+'</td>'
        +'<td>'+esc(a.theme||'–')+'</td></tr>').join('')
      + '</tbody></table>';
    box.querySelectorAll('[data-ticker]').forEach(btn=>{
      btn.addEventListener('click', ()=> showStock(btn.getAttribute('data-ticker')));
    });
  }catch(e){
    box.innerHTML = '<p class="empty">尚無告警</p>';
  }
}

async function loadPerformance(){
  const box = document.getElementById('perf-box');
  if(!box) return;
  try{
    const r = await j('/api/performance');
    if(!r || r.empty){
      box.innerHTML = '<p class="empty">尚未結算</p>';
      return;
    }
    const rows = r.horizons || [];
    let html = '<table><thead><tr><th>區間</th><th class="num">n</th><th class="num">勝率</th><th class="num">平均報酬</th></tr></thead><tbody>';
    html += rows.map(h => {
      const wr = h.win_rate_pct==null ? '–' : h.win_rate_pct+'%';
      return '<tr><td>T+'+h.horizon_td+'</td><td class="num">'+h.n+'</td>'
        +'<td class="num">'+wr+'</td>'
        +'<td class="num '+(h.avg_return_pct>0?'pos':(h.avg_return_pct<0?'neg':''))+'">'+pct(h.avg_return_pct)+'</td></tr>';
    }).join('');
    html += '</tbody></table>';
    const pats = [];
    rows.forEach(h => (h.by_pattern||[]).forEach(p => {
      pats.push({horizon_td:h.horizon_td, ...p});
    }));
    if(pats.length){
      html += '<div class="bt-section-title">依型態</div>';
      html += '<table><thead><tr><th>區間</th><th>型態</th><th class="num">n</th><th class="num">勝率</th><th class="num">平均報酬</th></tr></thead><tbody>';
      html += pats.map(p => {
        const wr = p.win_rate_pct==null ? '–' : p.win_rate_pct+'%';
        return '<tr><td>T+'+p.horizon_td+'</td><td class="pat">'+esc(patName(p.pattern_type))+'</td>'
          +'<td class="num">'+p.n+'</td><td class="num">'+wr+'</td>'
          +'<td class="num '+(p.avg_return_pct>0?'pos':(p.avg_return_pct<0?'neg':''))+'">'+pct(p.avg_return_pct)+'</td></tr>';
      }).join('');
      html += '</tbody></table>';
    }
    html += '<p class="assumptions">假設：'+esc(r.assumptions||'')+' 勝率與平均報酬只計已結算列；n 為該區間樣本數。</p>';
    box.innerHTML = html;
  }catch(e){
    box.innerHTML = '<p class="empty">尚未結算</p>';
  }
}

async function loadSummary(){
  let s;
  try { s = await j('/api/summary'); }
  catch(e){
    renderFreshness({stale:true, empty:true, tables:[], calendar_note:'載入失敗'});
    return;
  }
  if(s.index){
    document.getElementById('k-idx').textContent = fmt(s.index[1]);
    const c = s.index[2];
    const el = document.getElementById('k-chg');
    if(c!=null){ el.textContent = (c>=0?'+':'')+fmt(c); el.className = 'kpi-sub '+(c>=0?'pos':'neg'); }
  }
  const net = s.foreign_net_total;
  const kn = document.getElementById('k-net');
  if(net==null){
    kn.textContent = '–';
    kn.className = 'kpi-value';
  } else {
    kn.textContent = (net>=0?'+':'')+fmt(zhang(net))+' 張';
    kn.className = 'kpi-value '+(net>=0?'pos':'neg');
  }
  document.getElementById('k-netdate').textContent = s.latest_date || '無資料';
  document.getElementById('k-cnt').textContent = fmt(s.stock_count);
  if(s.date_range && s.date_range[0] && s.date_range[1]){
    document.getElementById('k-span').textContent = s.date_range[0]+' ~ '+s.date_range[1];
    ['top-start','top-end'].forEach(id=>{
      const el = document.getElementById(id);
      el.min = s.date_range[0]; el.max = s.date_range[1];
    });
  } else {
    document.getElementById('k-span').textContent = '無資料';
  }
  renderFreshness(s.freshness);
}

async function loadKline(){
  const itv = document.getElementById('kint').value;
  try {
    const r = await j('/api/ohlc?days='+days()+'&interval='+itv);
    if(!(r.data||[]).length){
      const msg = itv==='hour'
        ? '尚無小時K資料。請跑 python -m market.update_market_data'
        : EMPTY_MARKET;
      setChartEmpty('c-kline', msg);
      return;
    }
    setChartReady('c-kline');
    mk('c-kline', {type:'candlestick',
      data:{datasets:[{data:r.data.map(d=>({x:new Date(d[0]).getTime(), o:d[1], h:d[2], l:d[3], c:d[4]})),
        color:{up:'#c0392b', down:'#27ae60', unchanged:'#6c757d'},
        borderColor:{up:'#c0392b', down:'#27ae60', unchanged:'#6c757d'}}]},
      options:{animation:false, plugins:{legend:{display:false}, zoom:ZOOM},
        scales:{x:{type:'timeseries', time:{unit: itv==='hour' ? 'day' : 'month'},
                   ticks:{maxTicksLimit:14}},
                y:{grace:'2%'}}}});
  } catch(e){ setChartEmpty('c-kline', EMPTY_MARKET); }
}

const MARKET_OVERLAYS = [
  {type:'market', kind:'taifex_ratio', label:'外資÷投信 台指期未平倉比', chip:'市場'},
  {type:'market', kind:'foreign_net', label:'外資每日買賣超(張)', chip:'市場'},
  {type:'market', kind:'margin_fin', label:'融資餘額(億元)', chip:'市場'},
  {type:'market', kind:'margin_short', label:'融券餘額(千張)', chip:'市場'},
];

let ovChoice = null;
let ovHits = [];
let ovActive = -1;
let ovTimer = null;

function esc(s){
  return String(s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function overlayLabel(c){
  if(!c) return '';
  if(c.type==='stock_close') return c.id+' '+c.name+' 收盤';
  if(c.type==='stock_foreign') return c.id+' '+c.name+' 外資買賣超';
  return c.label;
}

function hideOverlayMenu(){
  document.getElementById('ov-menu').hidden = true;
  ovActive = -1;
}

function renderOverlayMenu(items){
  ovHits = items;
  if(ovActive >= items.length) ovActive = items.length ? 0 : -1;
  const menu = document.getElementById('ov-menu');
  if(!items.length){
    menu.innerHTML = '<div class="hint" style="padding:8px 10px">沒有符合的疊圖</div>';
    menu.hidden = false;
    return;
  }
  menu.innerHTML = items.map((it,i)=>
    '<button type="button" class="'+(i===ovActive?'active':'')+'" data-i="'+i+'">'
    +'<span>'+esc(overlayLabel(it))+'</span><span class="ov-chip">'+esc(it.chip||'')+'</span></button>'
  ).join('');
  menu.hidden = false;
  bindSuggestPick(menu, items, pickOverlay);
}

function marketOverlayHits(q){
  const t = q.trim().toLowerCase();
  if(!t) return MARKET_OVERLAYS.slice();
  return MARKET_OVERLAYS.filter(o =>
    o.label.toLowerCase().includes(t) || o.kind.toLowerCase().includes(t) ||
    (t==='外資' && o.kind!=='margin_fin' && o.kind!=='margin_short'));
}

async function fetchOverlayHits(q){
  const market = marketOverlayHits(q);
  const t = q.trim();
  if(!t) return market;
  const r = await j('/api/stocks?q='+encodeURIComponent(t));
  const stocks = [];
  (r.data||[]).forEach(row=>{
    const id = row[0], name = row[1]||id;
    stocks.push({type:'stock_close', id, name, chip:'收盤'});
    stocks.push({type:'stock_foreign', id, name, chip:'外資買賣超'});
  });
  return market.concat(stocks);
}

function onOverlayFocus(){ onOverlayInput(); }
function onOverlayInput(){
  const q = document.getElementById('ov-q').value;
  clearTimeout(ovTimer);
  ovTimer = setTimeout(async ()=>{
    ovActive = -1;
    renderOverlayMenu(await fetchOverlayHits(q));
  }, 160);
}
function onOverlayKey(ev){
  const menu = document.getElementById('ov-menu');
  if(ev.key==='Escape'){ hideOverlayMenu(); return; }
  if(ev.key==='ArrowDown' || ev.key==='ArrowUp'){
    ev.preventDefault();
    if(menu.hidden){ onOverlayInput(); return; }
    if(!ovHits.length) return;
    const dir = ev.key==='ArrowDown' ? 1 : -1;
    ovActive = (ovActive + dir + ovHits.length) % ovHits.length;
    renderOverlayMenu(ovHits);
    const el = menu.querySelector('button.active');
    if(el) el.scrollIntoView({block:'nearest'});
    return;
  }
  if(ev.key==='Enter'){
    ev.preventDefault();
    if(!menu.hidden && ovHits.length){
      pickOverlay(ovHits[ovActive<0?0:ovActive]);
    }
  }
}
function pickOverlay(item){
  ovChoice = item;
  document.getElementById('ov-q').value = overlayLabel(item);
  document.getElementById('ov-clear').hidden = false;
  hideOverlayMenu();
  loadTaiex();
}
function clearOverlay(){
  ovChoice = null;
  document.getElementById('ov-q').value = '';
  document.getElementById('ov-clear').hidden = true;
  hideOverlayMenu();
  loadTaiex();
}
document.addEventListener('click', ev=>{
  if(!ev.target.closest('.ov-search')) hideOverlayMenu();
  if(!ev.target.closest('#sid-search')) hideStockMenu();
});

// 疊圖資料都是日頻,指數走勢是小時頻 —— 用「當日值」對齊到當天每個小時點上,
// 畫出來像階梯狀,一天內同一條水平線,換日才跳到新值,方便跟指數同框比較。
async function buildOverlayMap(choice){
  const map = new Map();
  if(!choice) return map;
  if(choice.type==='market' && choice.kind==='taifex_ratio'){
    const r = await j('/api/taifex_oi?days='+days());
    r.dates.forEach((dt,i)=>{ if(r.ratio[i]!=null) map.set(dt, r.ratio[i]); });
  } else if(choice.type==='market' && choice.kind==='foreign_net'){
    const r = await j('/api/foreign_total?days='+days());
    r.data.forEach(d=>map.set(d[0], zhang(d[1])));
  } else if(choice.type==='market' && choice.kind==='margin_fin'){
    const r = await j('/api/margin_total?days='+days());
    r.fin.forEach(d=>map.set(d[0], Math.round(d[1]/100000)));
  } else if(choice.type==='market' && choice.kind==='margin_short'){
    const r = await j('/api/margin_total?days='+days());
    r.short.forEach(d=>map.set(d[0], Math.round(d[1]/1000)));
  } else if(choice.type==='stock_close'){
    const r = await j('/api/stock_ohlc?id='+encodeURIComponent(choice.id)+'&days='+days());
    r.data.forEach(d=>map.set(d[0], d[1]));
  } else if(choice.type==='stock_foreign'){
    const r = await j('/api/stock?id='+encodeURIComponent(choice.id)+'&days='+days());
    r.data.forEach(d=>map.set(d[0], zhang(d[4])));
  }
  return map;
}

async function loadTaiex(){
  try {
    const r = await j('/api/taiex?days='+days());
    if(!(r.data||[]).length){ setChartEmpty('c-taiex', EMPTY_MARKET); return; }
    setChartReady('c-taiex');
    const labels = r.data.map(d=>d[0].slice(0,16).replace('T',' '));
    const datasets = [{label:'加權指數', data:r.data.map(d=>d[1]), borderColor:'#4C72B0',
      borderWidth:1.5, pointRadius:0, tension:.2}];

    const ov = ovChoice;
    const scales = {x:{ticks:{maxTicksLimit:12}}, y:{grace:'2%'}};
    if(ov){
      const map = await buildOverlayMap(ov);
      const vals = r.data.map(d => { const dt=d[0].slice(0,10); return map.has(dt) ? map.get(dt) : null; });
      datasets.push({label:overlayLabel(ov), data:vals, borderColor:'#c0392b',
        borderDash:[5,4], borderWidth:2, pointRadius:0, stepped:true, spanGaps:true, yAxisID:'y2'});
      if(ov.type==='market' && ov.kind==='taifex_ratio'){
        const vv = vals.filter(v=>v!=null);
        if(vv.length){
          const lo=Math.min(...vv), hi=Math.max(...vv), pad=(hi-lo)*0.15||0.1;
          scales.y2 = {position:'right', grid:{display:false}, min:lo-pad, max:hi+pad};
        }
      } else {
        scales.y2 = {position:'right', grid:{display:false}};
      }
    }

    mk('c-taiex', {type:'line', data:{labels, datasets},
      options:{animation:false, plugins:{legend:{display: !!ov}, zoom:ZOOM},
        interaction:{mode:'index',intersect:false},
        scales}});
  } catch(e){ setChartEmpty('c-taiex', EMPTY_MARKET); }
}

async function loadNet(){
  try {
    const r = await j('/api/foreign_total?days='+days());
    if(!(r.data||[]).length){ setChartEmpty('c-net', EMPTY_MARKET); return; }
    setChartReady('c-net');
    mk('c-net', {type:'bar', data:{labels:r.data.map(d=>d[0]),
      datasets:[{data:r.data.map(d=>zhang(d[1])),
        backgroundColor:r.data.map(d=>d[1]>=0?'#c0392bcc':'#27ae60cc')}]},
      options:{animation:false, plugins:{legend:{display:false}, zoom:ZOOM},
        scales:{x:{ticks:{maxTicksLimit:15}}}}});
  } catch(e){ setChartEmpty('c-net', EMPTY_MARKET); }
}

async function loadMargin(){
  try {
    const r = await j('/api/margin_total?days='+days());
    if(!(r.fin||[]).length && !(r.short||[]).length){
      setChartEmpty('c-margin', EMPTY_MARKET);
      return;
    }
    setChartReady('c-margin');
    mk('c-margin', {type:'line', data:{labels:(r.fin.length?r.fin:r.short).map(d=>d[0]),
      datasets:[
        {label:'融資餘額(億元)', data:r.fin.map(d=>Math.round(d[1]/100000)),
         borderColor:'#DD8452', backgroundColor:'#DD845220', borderWidth:2, pointRadius:0, tension:.2, fill:true},
        {label:'融券餘額(千張)', data:r.short.map(d=>Math.round(d[1]/1000)),
         borderColor:'#8172B3', borderWidth:2, pointRadius:0, tension:.2, yAxisID:'y2'}]},
      options:{animation:false, interaction:{mode:'index',intersect:false},
        plugins:{zoom:ZOOM},
        scales:{x:{ticks:{maxTicksLimit:12}},
                y2:{position:'right', grid:{display:false}}}}});
  } catch(e){ setChartEmpty('c-margin', EMPTY_MARKET); }
}

async function loadTaifexOi(){
  try {
    const r = await j('/api/taifex_oi?days='+days());
    if(!(r.dates||[]).length){
      setChartEmpty('c-taifex', EMPTY_MARKET);
      return;
    }
    setChartReady('c-taifex');
    // 比值恆為負;顯式鎖定右軸範圍,避免自動刻度把 0 / 正值也畫進刻度列表造成誤讀
    const rv = r.ratio.filter(v=>v!=null);
    const rMin = Math.min(...rv), rMax = Math.max(...rv);
    const pad = (rMax-rMin)*0.15 || 0.1;
    mk('c-taifex', {type:'line', data:{labels:r.dates,
      datasets:[
        {label:'外資淨額(口)', data:r.foreign, borderColor:'#c0392b',
         borderWidth:1.5, pointRadius:0, tension:.2},
        {label:'投信淨額(口)', data:r.trust, borderColor:'#27ae60',
         borderWidth:1.5, pointRadius:0, tension:.2},
        {label:'外資÷投信比(恆為負)', data:r.ratio, borderColor:'#4C72B0', borderDash:[5,4],
         borderWidth:2, pointRadius:0, tension:.2, yAxisID:'y2'}]},
      options:{animation:false, interaction:{mode:'index',intersect:false},
        plugins:{zoom:ZOOM},
        scales:{x:{ticks:{maxTicksLimit:12}},
                y2:{position:'right', grid:{display:false},
                    min: rMin-pad, max: rMax+pad}}}});
  } catch(e){ setChartEmpty('c-taifex', EMPTY_MARKET); }
}

function topRangeParams(){
  const preset = document.getElementById('top-preset').value;
  if(preset==='custom'){
    const s = document.getElementById('top-start').value;
    const e = document.getElementById('top-end').value;
    const q = [];
    if(s) q.push('start='+s);
    if(e) q.push('end='+e);
    return q.length ? '?'+q.join('&') : '';
  }
  return '?days='+encodeURIComponent(preset);
}
function onTopPreset(){
  if(document.getElementById('top-preset').value==='custom') return;
  loadTop();
}
function onTopDates(){
  document.getElementById('top-preset').value = 'custom';
  loadTop();
}
async function loadTop(){
  try {
    const r = await j('/api/top'+topRangeParams());
    if(r.start){
      const preset = document.getElementById('top-preset').value;
      if(preset!=='custom' || !document.getElementById('top-start').value){
        document.getElementById('top-start').value = r.start;
        document.getElementById('top-end').value = r.end;
      }
    }
    const span = (!r.start) ? '' : (r.start===r.end ? r.start : r.start+'～'+r.end);
    const daysHint = r.trading_days>1 ? '（'+r.trading_days+' 個交易日）' : '';
    document.getElementById('t-buy').textContent = span+' 外資買超前 15(張)'+daysHint;
    document.getElementById('t-sell').textContent = span+' 外資賣超前 15(張)'+daysHint;
    if(!(r.buy||[]).length && !(r.sell||[]).length){
      setChartEmpty('c-buy', EMPTY_MARKET);
      setChartEmpty('c-sell', EMPTY_MARKET);
      return;
    }
    setChartReady('c-buy');
    setChartReady('c-sell');
    const cfg = (rows, color) => ({type:'bar',
      data:{labels:rows.map(d=>d[0]+' '+d[1]),
        datasets:[{data:rows.map(d=>Math.abs(zhang(d[2]))), backgroundColor:color}]},
      options:{indexAxis:'y', animation:false, plugins:{legend:{display:false}},
        scales:{y:{ticks:{font:{size:11}}}},
        onHover:(evt, els)=>{
          const t = evt.native && evt.native.target;
          if(t) t.style.cursor = els.length ? 'pointer' : 'default';
        },
        onClick:(evt, els)=>{
          if(!els.length) return;
          const row = rows[els[0].index];
          if(row) selectStock(row[0], row[1]);
        }}});
    mk('c-buy', cfg(r.buy||[], '#c0392bcc'));
    mk('c-sell', cfg(r.sell||[], '#27ae60cc'));
  } catch(e){
    setChartEmpty('c-buy', EMPTY_MARKET);
    setChartEmpty('c-sell', EMPTY_MARKET);
  }
}

async function loadStock(){
  const sid = stockId || parseStockId(document.getElementById('sid').value);
  if(!sid) return;
  const r = await j('/api/stock?id='+encodeURIComponent(sid)+'&days='+days());
  const cv = document.getElementById('c-stock');
  const tb = document.getElementById('stock-table');
  if(!r.data.length){
    cv.style.display='none';
    tb.innerHTML='<div class="chart-empty">查無 '+esc(sid)+' 的資料。請跑 python -m market.update_market_data</div>';
    return;
  }
  cv.style.display='block';
  const nm = r.data[0][1];
  const el = document.getElementById('sid');
  if(el && sid && nm) el.value = sid+' '+nm;
  let cum = 0; const cumData = r.data.map(d=>cum += zhang(d[4]));
  mk('c-stock', {type:'bar', data:{labels:r.data.map(d=>d[0]),
    datasets:[
      {label:'每日買賣超(張)', data:r.data.map(d=>zhang(d[4])),
       backgroundColor:r.data.map(d=>d[4]>=0?'#c0392bcc':'#27ae60cc'), order:2},
      {label:'累計(張)', data:cumData, type:'line', borderColor:'#4C72B0',
       pointRadius:0, borderWidth:2, tension:.2, yAxisID:'y2', order:1}]},
    options:{animation:false, interaction:{mode:'index',intersect:false},
      plugins:{zoom:ZOOM},
      scales:{x:{ticks:{maxTicksLimit:15}}, y2:{position:'right', grid:{display:false}}}}});
  // 個股融資融券餘額
  const m = await j('/api/stock_margin?id='+encodeURIComponent(sid)+'&days='+days());
  const cvm = document.getElementById('c-stock-margin');
  if(m.data.length){
    cvm.style.display='block';
    mk('c-stock-margin', {type:'line', data:{labels:m.data.map(d=>d[0]),
      datasets:[
        {label:'融資餘額(張)', data:m.data.map(d=>d[1]), borderColor:'#DD8452',
         borderWidth:2, pointRadius:0, tension:.2},
        {label:'融券餘額(張)', data:m.data.map(d=>d[2]), borderColor:'#8172B3',
         borderWidth:2, pointRadius:0, tension:.2, yAxisID:'y2'}]},
      options:{animation:false, interaction:{mode:'index',intersect:false},
        plugins:{zoom:ZOOM},
        scales:{x:{ticks:{maxTicksLimit:12}}, y2:{position:'right', grid:{display:false}}}}});
  } else { cvm.style.display='none'; }
  const last = r.data.slice(-15).reverse();
  tb.innerHTML = '<table><thead><tr><th>日期</th><th>名稱</th><th class="num">買進(張)</th><th class="num">賣出(張)</th><th class="num">買賣超(張)</th></tr></thead><tbody>'
    + last.map(d=>'<tr><td>'+d[0]+'</td><td>'+d[1]+'</td><td class="num">'+fmt(zhang(d[2]))
      +'</td><td class="num">'+fmt(zhang(d[3]))+'</td><td class="num '+(d[4]>=0?'pos':'neg')+'">'
      +(d[4]>=0?'+':'')+fmt(zhang(d[4]))+'</td></tr>').join('')
    + '</tbody></table>';
}

// ---------------------------------------------------------------- 策略回測

const DOW_NAME = ['週一','週二','週三','週四','週五'];

function btOnDatasetChange(){
  const ds = document.getElementById('bt-dataset').value;
  const warn = document.getElementById('bt-stale-warning');
  const intradayRadio = document.querySelector('input[name="bt-mode"][value="intraday"]');
  const overnightRadio = document.querySelector('input[name="bt-mode"][value="overnight"]');
  const holdHourOpt = document.querySelector('#bt-holdto option[value="next_hour"]');
  if(ds === '15y_daily'){
    warn.style.display = 'block';
    intradayRadio.disabled = true;
    if(intradayRadio.checked) overnightRadio.checked = true;
    holdHourOpt.disabled = true;
    btOnModeChange();
  } else {
    warn.style.display = 'none';
    intradayRadio.disabled = false;
    holdHourOpt.disabled = false;
  }
}

function btOnModeChange(){
  const mode = document.querySelector('input[name="bt-mode"]:checked').value;
  document.getElementById('bt-intraday-box').style.display = mode==='intraday' ? 'block' : 'none';
  document.getElementById('bt-overnight-box').style.display = mode==='overnight' ? 'block' : 'none';
  document.getElementById('bt-swing-box').style.display = mode==='swing' ? 'block' : 'none';

  // 「收盤才確定」的濾網(均線交叉/N日突破/當日漲跌/今日均線)在日內模式下會偷看未來
  // 資訊,直接停用對應控制項並提示,而不是等送出去才被後端擋掉
  const closeDecided = mode === 'intraday';
  document.getElementById('bt-close-decided-hint').style.display = closeDecided ? 'block' : 'none';
  ['bt-macross','bt-breakout','bt-breakoutwindow','bt-dayretdir','bt-dayretmin'].forEach(id=>{
    document.getElementById(id).disabled = closeDecided;
  });
  document.querySelectorAll('#bt-trend option[value$="_today"]').forEach(opt=>{ opt.disabled = closeDecided; });
  if(closeDecided){
    document.getElementById('bt-macross').value = 'none';
    document.getElementById('bt-breakout').value = 'none';
    document.getElementById('bt-dayretdir').value = 'any';
    document.getElementById('bt-dayretmin').value = '0';
    const trendSel = document.getElementById('bt-trend');
    if(trendSel.value.endsWith('_today')) trendSel.value = 'none';
  }
}

function btOnStopToggle(){
  document.getElementById('bt-stop-box').style.display =
    document.getElementById('bt-stop-on').checked ? 'flex' : 'none';
}

function btOnSwingTpToggle(){
  const show = document.getElementById('bt-swing-tpon').checked;
  document.getElementById('bt-swing-tp-label').style.display = show ? 'inline' : 'none';
  document.getElementById('bt-swing-tppct').style.display = show ? 'inline-block' : 'none';
}

function btOnHoldToChange(){
  const show = document.getElementById('bt-holdto').value === 'next_hour';
  document.getElementById('bt-holdhour-label').style.display = show ? 'inline' : 'none';
  document.getElementById('bt-holdhour').style.display = show ? 'inline-block' : 'none';
}
document.getElementById('bt-holdto').addEventListener('change', btOnHoldToChange);

function btBuildRule(){
  const dow = [...document.querySelectorAll('.bt-dow:checked')].map(el=>parseInt(el.value));
  const mode = document.querySelector('input[name="bt-mode"]:checked').value;
  const rule = {
    dataset: document.getElementById('bt-dataset').value,
    mode: mode,
    filters: {
      weekdays: dow,
      trend: document.getElementById('bt-trend').value,
      prev_day: document.getElementById('bt-prevday').value,
      gap_dir: document.getElementById('bt-gapdir').value,
      gap_abs_min_pct: parseFloat(document.getElementById('bt-gapmin').value)||0,
      day_ret_dir: document.getElementById('bt-dayretdir').value,
      day_ret_min_pct: parseFloat(document.getElementById('bt-dayretmin').value)||0,
      ma_cross: document.getElementById('bt-macross').value,
      breakout: document.getElementById('bt-breakout').value,
      breakout_window: parseInt(document.getElementById('bt-breakoutwindow').value)||20,
      oi_ratio_mode: document.getElementById('bt-oiratio').value,
      oi_ratio_pctile: parseFloat(document.getElementById('bt-oipctile').value)||25,
      oi_ratio_window: parseInt(document.getElementById('bt-oiwindow').value)||60,
    },
    cost_pct: parseFloat(document.getElementById('bt-cost').value)||0,
  };
  if(mode==='intraday'){
    rule.entry = {
      reference: document.getElementById('bt-ref').value,
      offset_pct: parseFloat(document.getElementById('bt-offset').value)||0,
      trigger: document.getElementById('bt-trigger').value,
      direction: document.getElementById('bt-direction').value,
      earliest_hour: parseInt(document.getElementById('bt-earliest').value),
    };
    rule.exit_hour = parseInt(document.getElementById('bt-exithour').value);
    rule.stop = {
      enabled: document.getElementById('bt-stop-on').checked,
      reference: document.getElementById('bt-stopref').value,
      offset_pct: parseFloat(document.getElementById('bt-stopoffset').value)||0,
    };
  } else if(mode==='overnight'){
    rule.direction = document.getElementById('bt-on-direction').value;
    rule.hold_to = document.getElementById('bt-holdto').value;
    rule.hold_to_hour = parseInt(document.getElementById('bt-holdhour').value);
    rule.skip_weekend = document.getElementById('bt-skipweekend').checked;
  } else {
    rule.direction = document.getElementById('bt-swing-direction').value;
    rule.stop_pct = parseFloat(document.getElementById('bt-swing-stoppct').value)||2;
    rule.max_hold_days = parseInt(document.getElementById('bt-swing-maxhold').value)||60;
    rule.take_profit_on = document.getElementById('bt-swing-tpon').checked;
    rule.take_profit_pct = parseFloat(document.getElementById('bt-swing-tppct').value)||0;
  }
  return rule;
}

async function runBacktest(){
  const box = document.getElementById('bt-results');
  box.innerHTML = '<p class="hint">回測中…</p>';
  let data;
  try{
    const resp = await fetch('/api/backtest', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(btBuildRule())});
    data = await resp.json();
  }catch(e){ box.innerHTML = '<div class="bt-error">請求失敗:'+e+'</div>'; return; }
  renderBacktestResult(data);
}

function renderBacktestResult(d){
  const box = document.getElementById('bt-results');
  if(d.error){ box.innerHTML = '<div class="bt-error">'+d.error+'</div>'; return; }
  if(!d.n){ box.innerHTML = '<div class="bt-error">沒有任何交易被觸發(篩選後 '+d.days_passed_filter+' / '+d.total_days_in_dataset+' 天符合條件,但沒有一天觸發進場)。試試放寬篩選條件或調整進場規則。</div>'; return; }

  let html = '';
  if(d.stale_open_warning){
    html += '<div class="bt-warn">⚠️ 這個資料集用的是官方開盤價,90%以上時間等於前一天收盤價,任何用到「開盤價」的日內判斷都不可信,只看隔夜(收盤→收盤/開盤)的結果。</div>';
  }
  const evClass = d.ev_pct>=0 ? 'pos' : 'neg';
  html += '<div class="bt-kpis">'
    + '<div class="bt-kpi"><div class="l">交易數</div><div class="v">'+d.n+'</div></div>'
    + '<div class="bt-kpi"><div class="l">每筆淨益</div><div class="v '+evClass+'">'+(d.ev_pct>=0?'+':'')+d.ev_pct+'%</div></div>'
    + '<div class="bt-kpi"><div class="l">勝率</div><div class="v">'+d.win_rate+'%</div></div>'
    + '<div class="bt-kpi"><div class="l">t 值</div><div class="v">'+(d.t_stat??'–')+'</div></div>'
    + '<div class="bt-kpi"><div class="l">p 值</div><div class="v">'+(d.p_value??'–')+'</div></div>'
    + '<div class="bt-kpi"><div class="l">Sharpe(粗估年化)</div><div class="v">'+(d.sharpe??'–')+'</div></div>'
    + '<div class="bt-kpi"><div class="l">最慘 / 最好</div><div class="v" style="font-size:15px">'+d.worst_pct+'% / +'+d.best_pct+'%</div></div>'
    + (d.stopped_rate!=null ? '<div class="bt-kpi"><div class="l">停損觸發率</div><div class="v">'+d.stopped_rate+'%</div></div>' : '')
    + (d.avg_hold_days!=null ? '<div class="bt-kpi"><div class="l">平均持有天數</div><div class="v">'+d.avg_hold_days+'</div></div>' : '')
    + (d.max_drawdown&&d.max_drawdown.mdd_pct!=null ? '<div class="bt-kpi"><div class="l">最大回撤(MDD)</div><div class="v neg">'+d.max_drawdown.mdd_pct+'%</div></div>' : '')
    + (d.profit_factor!=null ? '<div class="bt-kpi"><div class="l">獲利因子</div><div class="v '+(d.profit_factor>=1?'pos':'neg')+'">'+d.profit_factor+'</div></div>' : '')
    + (d.payoff_ratio!=null ? '<div class="bt-kpi"><div class="l">賺賠比(均賺/均賠)</div><div class="v">'+d.payoff_ratio+'</div></div>' : '')
    + '<div class="bt-kpi"><div class="l">最大連續虧損</div><div class="v">'+(d.max_consec_losses??'–')+' 筆</div></div>'
    + '</div>';

  if(d.exit_reason_pct){
    const RN = {stop:'停損', take_profit:'停利', max_hold:'到期(未觸發停損/停利)'};
    html += '<div class="bt-section-title">出場原因分布</div>';
    html += '<p>' + Object.entries(d.exit_reason_pct).map(([k,v])=>(RN[k]||k)+': <b>'+v+'%</b>').join('　') + '</p>';
  }
  if(d.overlap_pct!=null){
    const warnCls = d.overlap_pct > 20 ? 'bt-warn' : '';
    html += '<div class="'+warnCls+'" style="margin:8px 0">'
      + (d.overlap_pct > 20 ? '⚠️ ' : '')
      + '重疊部位比例:<b>'+d.overlap_pct+'%</b> 的交易在進場時,上一筆同規則的倉位理論上還沒出場'
      + (d.overlap_pct > 20 ? '(比例偏高,樣本之間並不獨立,顯著性檢定的參考價值會被高估,區塊拔靴的結論比 t/p 值更可信)' : '')
      + '。</div>';
  }
  if(d.unresolved_trades){
    html += '<p class="hint">另有 '+d.unresolved_trades+' 個訊號出現在資料尾端,出場前資料就用完了(結果未知),已從統計中排除。</p>';
  }

  html += '<div class="bt-section-title">前後半穩定性(判斷是否為單一時段拖動整體結果)</div>';
  html += '<p>前半 EV = <b>'+(d.front_half_ev_pct??'–')+'%</b>　後半 EV = <b>'+(d.back_half_ev_pct??'–')+'%</b></p>';

  if(d.block_bootstrap_ci){
    const b = d.block_bootstrap_ci;
    const sig = (b.lo_pct*b.hi_pct>0);
    html += '<div class="bt-section-title">區塊拔靴 95% 信賴區間(以 '+b.block_days+' 天為區塊,處理事件群聚/重疊問題)</div>';
    html += '<p>[' + b.lo_pct + '%, ' + b.hi_pct + '%]　' + (sig ? '<b class="pos">不含0,統計上站得住</b>' : '<b class="neg">含0,不顯著,建議保守看待</b>') + '　(區塊數='+b.n_blocks+')</p>';
  } else {
    html += '<div class="bt-section-title">區塊拔靴</div><p class="hint">樣本數太少(&lt;30),略過區塊拔靴檢定。</p>';
  }

  html += '<div class="bt-section-title">成本敏感度(來回成本從0倍到3倍現有設定,EV怎麼變)</div>';
  html += '<table><thead><tr><th>成本%</th>' + d.cost_sensitivity.map(c=>'<th class="num">'+c.cost_pct+'%</th>').join('') + '</tr></thead>'
    + '<tbody><tr><td>每筆EV</td>' + d.cost_sensitivity.map(c=>'<td class="num '+(c.ev_pct>=0?'pos':'neg')+'">'+(c.ev_pct>=0?'+':'')+c.ev_pct+'%</td>').join('') + '</tr></tbody></table>';

  html += '<div class="bt-section-title">依星期拆解</div>';
  html += '<table><thead><tr><th>星期</th><th class="num">n</th><th class="num">EV</th><th class="num">勝率</th></tr></thead><tbody>'
    + d.by_weekday.map(w=>'<tr><td>'+DOW_NAME[w.dow]+'</td><td class="num">'+w.n+'</td><td class="num '+(w.ev_pct>=0?'pos':'neg')+'">'+(w.ev_pct>=0?'+':'')+w.ev_pct+'%</td><td class="num">'+w.win_rate+'%</td></tr>').join('')
    + '</tbody></table>';

  if(d.max_drawdown && d.max_drawdown.mdd_pct!=null){
    const m = d.max_drawdown;
    html += '<div class="bt-section-title">回撤分析</div>';
    html += '<p>最大回撤 <b class="neg">'+m.mdd_pct+'%</b>(第 '+(m.peak_idx+1)+' 筆的高點跌到第 '+(m.trough_idx+1)+' 筆)　'
      + '最長未創新高:<b>'+m.longest_dd_trades+'</b> 筆交易　'
      + '回撤修復:'+(m.trades_to_recover!=null ? '<b>'+m.trades_to_recover+'</b> 筆後回到前高' : '<b class="neg">至今尚未回到前高</b>')
      + '</p>';
    html += '<p class="hint">這是「每筆交易等權複利」的回撤,不是真實帳戶回撤(沒有部位大小、保證金、閒置資金的概念)。用途是看策略連續虧損能有多久多深,不能直接當「我會賠多少」。</p>';
  }

  if(d.excursion && d.excursion.all){
    const e = d.excursion;
    const row = (nm,o)=> o ? '<tr><td>'+nm+'</td><td class="num">'+o.n+'</td><td class="num neg">'+o.mae_avg_pct+'%</td><td class="num neg">'+o.mae_p90_pct+'%</td><td class="num pos">+'+o.mfe_avg_pct+'%</td></tr>' : '';
    html += '<div class="bt-section-title">MAE / MFE(最大不利 / 有利偏移 —— 調停損停利的直接依據)</div>';
    html += '<table><thead><tr><th></th><th class="num">n</th><th class="num">平均MAE</th><th class="num">最差10%的MAE</th><th class="num">平均MFE</th></tr></thead><tbody>'
      + row('全部', e.all) + row('賺錢的單', e.winners) + row('賠錢的單', e.losers)
      + '</tbody></table>';
    if(e.winners && e.losers){
      html += '<p class="hint">看法:賺錢的單平均最深被套 <b>'+e.winners.mae_avg_pct+'%</b>(最差10%到 '+e.winners.mae_p90_pct+'%)。'
        + '停損若設得比這還窄,會把本來會賺的單先洗掉。反過來,賺錢單的平均MFE是 +'+e.winners.mfe_avg_pct+'%,'
        + '若實際獲利遠低於它,代表出場太晚或缺停利。</p>';
    }
    if(e.coverage) html += '<p class="hint">MAE/MFE 涵蓋率 '+e.coverage+'(隔夜模式持倉多在休市時段,無盤中路徑者不計算)。</p>';
  }

  if(d.monthly && d.monthly.length){
    html += '<div class="bt-section-title">逐月報酬(該月所有交易複利,共 '+d.monthly.length+' 個月)'
      + '<button id="bt-monthly-toggle" style="margin-left:12px;padding:3px 12px;font-size:12px;cursor:pointer">展開/收合</button></div>';
    html += '<div id="bt-monthly-wrap" style="display:none;max-height:320px;overflow:auto;margin-top:8px">';
    html += '<table><thead><tr><th>月份</th><th class="num">交易數</th><th class="num">報酬</th><th class="num">勝率</th></tr></thead><tbody>'
      + d.monthly.map(m=>'<tr><td>'+m.month+'</td><td class="num">'+m.n+'</td>'
        + '<td class="num '+(m.ret_pct>=0?'pos':'neg')+'">'+(m.ret_pct>=0?'+':'')+m.ret_pct+'%</td>'
        + '<td class="num">'+m.win_rate+'%</td></tr>').join('')
      + '</tbody></table></div>';
    const pos = d.monthly.filter(m=>m.ret_pct>0).length;
    html += '<p>正報酬月份:<b>'+pos+' / '+d.monthly.length+'</b>('+(pos/d.monthly.length*100).toFixed(1)+'%)</p>';
  }

  if(d.price_series && d.price_series.length){
    html += '<div class="bt-section-title">進出場位置(標在指數走勢上)</div>';
    html += '<p class="hint">▲ 進場　▼ 出場。日內模式進出場同一天,兩個標記會重疊在同一個X位置。</p>';
    html += '<canvas id="bt-marker-chart" style="max-height:320px"></canvas>';
  }

  html += '<div class="bt-section-title">權益曲線(每筆交易複利,不代表資金曲線,只看形狀是否平穩)</div>';
  html += '<canvas id="bt-equity-chart" style="max-height:280px"></canvas>';

  if(d.trades && d.trades.length){
    const RN = {stop:'停損', take_profit:'停利', max_hold:'到期', exit_hour:'出場時間到', hold_to:'持有到期'};
    html += '<div class="bt-section-title">交易明細(每筆進出場時間與價位,共 '+d.trades.length+' 筆)'
      + '<button id="bt-trades-toggle" style="margin-left:12px;padding:3px 12px;font-size:12px;cursor:pointer">展開/收合</button>'
      + '<button id="bt-trades-csv" style="margin-left:6px;padding:3px 12px;font-size:12px;cursor:pointer">下載CSV</button>'
      + '</div>';
    html += '<p class="hint">觸價進場/停損只能定位到那一根K棒之內(日內為小時K、波段為日K),故標為區間或「盤中觸價」,不是精確到分秒的成交時點。</p>';
    html += '<div id="bt-trades-wrap" style="display:none;max-height:420px;overflow:auto;margin-top:8px">';
    html += '<table><thead><tr><th>#</th><th>星期</th><th>進場時間</th><th class="num">進場價</th>'
      + '<th>出場時間</th><th class="num">出場價</th>'
      + (d.mode==='swing' ? '<th class="num">持有天數</th>' : '')
      + '<th>出場原因</th><th class="num">MAE</th><th class="num">MFE</th><th class="num">淨報酬</th></tr></thead><tbody>';
    html += d.trades.map((t,i)=>'<tr>'
      + '<td>'+(i+1)+'</td>'
      + '<td>'+(DOW_NAME[t.dow]||'')+'</td>'
      + '<td>'+(t.entry_time||'–')+'</td>'
      + '<td class="num">'+t.entry_price+'</td>'
      + '<td>'+(t.exit_time||'–')+'</td>'
      + '<td class="num">'+t.exit_price+'</td>'
      + (d.mode==='swing' ? '<td class="num">'+(t.hold_days??'–')+'</td>' : '')
      + '<td>'+(RN[t.exit_reason]||t.exit_reason||'–')+'</td>'
      + '<td class="num neg">'+(t.mae_pct!=null ? t.mae_pct+'%' : '–')+'</td>'
      + '<td class="num pos">'+(t.mfe_pct!=null ? '+'+t.mfe_pct+'%' : '–')+'</td>'
      + '<td class="num '+(t.ret_net_pct>=0?'pos':'neg')+'">'+(t.ret_net_pct>=0?'+':'')+t.ret_net_pct+'%</td>'
      + '</tr>').join('');
    html += '</tbody></table></div>';
  }

  box.innerHTML = html;

  const tw = document.getElementById('bt-trades-wrap');
  if(tw){
    document.getElementById('bt-trades-toggle').addEventListener('click', ()=>{
      tw.style.display = tw.style.display==='none' ? 'block' : 'none';
    });
    document.getElementById('bt-trades-csv').addEventListener('click', ()=>{
      const hdr = ['#','日期','星期','進場時間','進場價','出場時間','出場價','持有天數','出場原因','MAE%','MFE%','淨報酬%'];
      const lines = [hdr.join(',')].concat(d.trades.map((t,i)=>[
        i+1, t.date, DOW_NAME[t.dow]||'', '"'+(t.entry_time||'')+'"', t.entry_price,
        '"'+(t.exit_time||'')+'"', t.exit_price, t.hold_days??'', t.exit_reason||'',
        t.mae_pct??'', t.mfe_pct??'', t.ret_net_pct
      ].join(',')));
      const blob = new Blob(['\\ufeff'+lines.join('\\n')], {type:'text/csv;charset=utf-8'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'backtest_trades.csv';
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }

  const mwrap = document.getElementById('bt-monthly-wrap');
  if(mwrap){
    document.getElementById('bt-monthly-toggle').addEventListener('click', ()=>{
      mwrap.style.display = mwrap.style.display==='none' ? 'block' : 'none';
    });
  }

  if(d.price_series && d.price_series.length){
    const labels = d.price_series.map(p=>p.date);
    const idxOf = new Map(labels.map((dt,i)=>[dt,i]));
    const entryArr = new Array(labels.length).fill(null);
    const exitArr  = new Array(labels.length).fill(null);
    d.trades.forEach(t=>{
      const ei = idxOf.get(t.date);
      if(ei!=null) entryArr[ei] = t.entry_price;
      // 出場日期從 exit_time 前10碼取(格式固定為 YYYY-MM-DD ...)
      const xd = (t.exit_time||'').slice(0,10);
      const xi = idxOf.get(xd);
      if(xi!=null) exitArr[xi] = t.exit_price;
    });
    const isLong = d.direction !== 'short';
    mk('bt-marker-chart', {type:'line', data:{labels, datasets:[
      {label:'加權指數', data:d.price_series.map(p=>p.close), borderColor:'#adb5bd',
       borderWidth:1, pointRadius:0, tension:.1, order:3},
      {label:'進場', data:entryArr, showLine:false, pointStyle:'triangle', pointRadius:6,
       pointRotation:0, backgroundColor: isLong?'#c0392b':'#27ae60',
       borderColor: isLong?'#c0392b':'#27ae60', order:1},
      {label:'出場', data:exitArr, showLine:false, pointStyle:'triangle', pointRadius:6,
       pointRotation:180, backgroundColor:'#4C72B0', borderColor:'#4C72B0', order:2},
    ]},
      options:{animation:false, plugins:{zoom:ZOOM, legend:{display:true}},
        interaction:{mode:'nearest', intersect:true},
        spanGaps:false,
        scales:{x:{ticks:{maxTicksLimit:12}}, y:{grace:'2%'}}}});
  }

  mk('bt-equity-chart', {type:'line', data:{labels:d.equity_curve.map(p=>p.date),
    datasets:[{data:d.equity_curve.map(p=>p.equity), borderColor:'#4C72B0', borderWidth:1.5,
      pointRadius:0, tension:.1}]},
    options:{animation:false, plugins:{legend:{display:false}, zoom:ZOOM},
      interaction:{mode:'index',intersect:false},
      scales:{x:{ticks:{maxTicksLimit:12}}, y:{grace:'5%'}}}});
}

btOnHoldToChange();
btOnModeChange();

function loadAll(){ loadSummary(); loadKline(); loadTaiex(); loadMargin(); loadNet(); loadTaifexOi(); loadTop();
  loadAlerts(); loadPerformance();
  if(stockId) loadStock(); }
function initBtFolds(){
  const mobile = window.matchMedia('(max-width:768px)').matches;
  document.querySelectorAll('details[data-bt-fold]').forEach(el=>{
    const key = el.dataset.btFold;
    if(key==='dataset'){ el.open = true; return; }
    if(key==='filters' || key==='entry' || key==='exit') el.open = !mobile;
  });
}
window.addEventListener('resize', resizeCharts);
(function(){
  const id = parseStockQuery(location.search);
  if(id) selectStock(id, null, {load:false, scroll:false, section:false});
  initBtFolds();
  loadAll();
  showSection(resolveSection(), {updateHash:false});
})();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            body = json.dumps(health_payload()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        elif u.path == "/" or u.path == "/index.html":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            result = api(u.path, parse_qs(u.query))
            if result is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/backtest":
            self.send_response(404); self.end_headers(); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            rule = json.loads(self.rfile.read(length) or b"{}")
            conn = market_db.connect_for_backtest()
            try:
                from web import backtest_engine
                result = backtest_engine.run_backtest(conn, rule)
            finally:
                conn.close()
        except Exception as e:
            result = {"error": f"回測執行失敗: {e}"}
        body = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main() -> int:
    if not market_db.available():
        raise SystemExit(
            "找不到市場資料。本機請先跑 python -m market.update_market_data;"
            " Cloud Run 請設 TURSO_DATABASE_URL 跟 TURSO_AUTH_TOKEN。"
        )

    host, port = market_db.listen_host_port()
    try_ports = [port] if host == "0.0.0.0" else list(range(port, port + 10))
    server = None
    bound = port
    for candidate in try_ports:
        try:
            server = HTTPServer((host, candidate), Handler)
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
    if market_db.should_open_browser():
        webbrowser.open(f"http://localhost:{bound}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
