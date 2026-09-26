"""0050 five-minute +2% alerts; dry-run by default.

Run a single persistent worker on a host with durable local storage. A file
lock prevents overlapping processes on that host. Do not run per web request.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, time as day_time, timedelta
import fcntl
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode, urlsplit
import uuid

from data.paths import repo_file
from market import etf_members
from market.intraday_prices import MAX_DELAY, evaluate, fetch_minutes, monitoring_session
from web.tw_calendar import HOLIDAY_YEARS, is_tw_trading_day, taiwan_now

log = logging.getLogger(__name__)
COOLDOWN_SECONDS = 15 * 60
WARMUP_SECONDS = 5 * 60
MAX_ATTEMPTS = 5
SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intraday_state (
  code TEXT NOT NULL, day TEXT NOT NULL, last_bar TEXT NOT NULL,
  armed INTEGER NOT NULL, cooldown_until TEXT,
  PRIMARY KEY (code, day)
);
CREATE TABLE IF NOT EXISTS intraday_outbox (
  event_id TEXT PRIMARY KEY, code TEXT NOT NULL, day TEXT NOT NULL,
  bar_end TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt TEXT NOT NULL,
  slack_ts TEXT, last_error TEXT
);
CREATE INDEX IF NOT EXISTS intraday_pending ON intraday_outbox(status, next_attempt);
"""


def open_store(path, *, send=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    mode = "send" if send else "dry_run"
    with conn:
        conn.execute("INSERT OR IGNORE INTO intraday_settings VALUES ('mode', ?)", (mode,))
    if conn.execute("SELECT value FROM intraday_settings WHERE key='mode'").fetchone()[0] != mode:
        conn.close()
        raise ValueError("Use separate state databases for dry-run and send mode")
    return conn


@contextmanager
def worker_lock(path):
    path = Path(str(path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another worker owns this state database") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def record_signal(conn, code, name, signal, snapshot, *, send=False):
    """Atomically update per-stock cooldown and enqueue a unique bar event."""
    if code not in {r["code"] for r in snapshot["stocks"]} or signal["status"] != "ok":
        return None
    end = datetime.fromisoformat(signal["bar_end"])
    day = end.date().isoformat()
    with conn:
        row = conn.execute("SELECT * FROM intraday_state WHERE code=? AND day=?",
                           (code, day)).fetchone()
        if row and end.isoformat() <= row["last_bar"]:
            return None
        armed = bool(row["armed"]) if row else True
        cooldown = row["cooldown_until"] if row else None
        event = None
        if not signal["triggered"]:
            armed = True
        elif armed and (not cooldown or end >= datetime.fromisoformat(cooldown)):
            event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "0050-rise-v1:"+code+":"+end.isoformat()))
            event = {**signal, "event_id": event_id, "code": code, "name": name,
                     "membership_asof": snapshot["asof"], "membership_version": snapshot["version"]}
            conn.execute(
                "INSERT OR IGNORE INTO intraday_outbox "
                "(event_id,code,day,bar_end,payload,status,next_attempt) VALUES (?,?,?,?,?,?,?)",
                (event_id, code, day, end.isoformat(), json.dumps(event, ensure_ascii=False),
                 "pending" if send else "dry_run", end.isoformat()),
            )
            cooldown = (end + timedelta(seconds=COOLDOWN_SECONDS)).isoformat()
            armed = False
        elif signal["triggered"]:
            # A crossing suppressed during cooldown still belongs to that wave.
            armed = False
        conn.execute("INSERT OR REPLACE INTO intraday_state VALUES (?,?,?,?,?)",
                     (code, day, end.isoformat(), int(armed), cooldown))
    return event


def slack_text(event, dashboard_url=""):
    def escape(text):
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    start = datetime.fromisoformat(event["window_start"]).strftime("%H:%M")
    end = datetime.fromisoformat(event["bar_end"]).strftime("%H:%M")
    baseline = datetime.fromisoformat(event["baseline_minute"]).strftime("%H:%M")
    text = (f"🚀 *0050 成分股五分鐘急拉*｜{escape(event['name'])}（{event['code']}）\n"
            f"{start}–{end} 台灣時間：{event['low']:g} → {event['close']:g}，"
            f"*+{event['rise_pct']:.2f}%*\n"
            f"基準：{baseline} 分鐘最低價；最新完成分鐘收盤：{end}\n"
            f"Yahoo 分鐘資料，可能延遲；偵測時距分鐘結束 {event['delay_seconds']:.0f} 秒。\n"
            f"0050 名單日期：{event['membership_asof']}")
    parsed = urlsplit(dashboard_url)
    if parsed.scheme in ("http", "https") and parsed.netloc and not parsed.username and not parsed.password:
        url = dashboard_url.split("#")[0].split("?")[0].rstrip("/") + "/?" + urlencode({"stock": event["code"]}) + "#stock"
        text += f"\n<{escape(url)}|查看個股頁面>"
    return text


def dispatch(conn, client, channel, now, member_codes, dashboard_url="", clock=None):
    """Bounded retry outbox. Old, removed or cross-day events expire unsent."""
    now = taiwan_now(now)
    gate = conn.execute("SELECT value FROM intraday_settings WHERE key='slack_retry_after'").fetchone()
    if gate and datetime.fromisoformat(gate[0]) > now:
        return {"sent": 0, "expired": 0}
    rows = conn.execute("SELECT * FROM intraday_outbox WHERE status='pending' ORDER BY bar_end LIMIT 50").fetchall()
    sent = expired = 0
    for row in rows:
        if clock is not None:
            now = taiwan_now(clock())
        age = (now - datetime.fromisoformat(row["bar_end"])).total_seconds()
        if (row["day"] != now.date().isoformat() or age > MAX_DELAY
                or age < 0 or row["code"] not in member_codes):
            with conn:
                conn.execute("UPDATE intraday_outbox SET status='expired' WHERE event_id=?", (row["event_id"],))
            expired += 1
            continue
        if datetime.fromisoformat(row["next_attempt"]) > now:
            break
        try:
            result = client.chat_postMessage(
                channel=channel, text=slack_text(json.loads(row["payload"]), dashboard_url),
                client_msg_id=row["event_id"], unfurl_links=False, unfurl_media=False,
            )
            if not result.get("ok"):
                raise RuntimeError("Slack did not acknowledge message")
        except Exception as exc:
            attempts = row["attempts"] + 1
            delay = min(60, 5 * 2 ** (attempts - 1))
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    delay = max(delay, int(response.headers.get("Retry-After", delay)))
                except (AttributeError, TypeError, ValueError):
                    pass
            with conn:
                conn.execute("INSERT OR REPLACE INTO intraday_settings VALUES ('slack_retry_after', ?)",
                             ((now+timedelta(seconds=delay)).isoformat(),))
                conn.execute("UPDATE intraday_outbox SET attempts=?,next_attempt=?,last_error=?,status=? WHERE event_id=?",
                             (attempts, (now+timedelta(seconds=delay)).isoformat(), type(exc).__name__,
                              "failed" if attempts >= MAX_ATTEMPTS else "pending", row["event_id"]))
            log.warning("Slack send failed event=%s type=%s attempt=%d", row["event_id"], type(exc).__name__, attempts)
            # Respect global/channel rate limiting before attempting another event.
            break
        else:
            with conn:
                conn.execute("UPDATE intraday_outbox SET status='sent',slack_ts=?,attempts=attempts+1,last_error=NULL WHERE event_id=?",
                             (result.get("ts"), row["event_id"]))
            sent += 1
    return {"sent": sent, "expired": expired}


class Monitor:
    def __init__(self, conn, *, send=False, client=None, channel="", dashboard_url="",
                 members_path=etf_members.DEFAULT_CACHE, loader=None, fetcher=None):
        self.conn, self.send, self.client, self.channel = conn, send, client, channel
        self.dashboard_url = dashboard_url
        self.members_path = members_path
        self.loader = loader or etf_members.load_members
        self.fetcher = fetcher or fetch_minutes
        self.snapshot = None
        self.members_checked = None
        self.day = None
        self.ready_at = {}
        self.last_good = {}

    def refresh_members(self, now):
        if (not self.snapshot or not self.members_checked or self.members_checked.date() != now.date()
                or (now-self.members_checked).total_seconds() >= 900):
            self.snapshot = self.loader(self.members_path, now=now)
            self.members_checked = now
        etf_members.validate(self.snapshot, now.date())

    def cycle(self, now=None):
        live_clock = now is None
        now = taiwan_now(now)
        summary = {"at": now.isoformat(), "mode": "send" if self.send else "dry_run"}
        if (now.year in HOLIDAY_YEARS and is_tw_trading_day(now.date())
                and day_time(8, 45) <= now.time() < day_time(9)):
            try:
                self.refresh_members(now)
                return {**summary, "status": "preopen", "members": len(self.snapshot["stocks"]),
                        "membership_asof": self.snapshot["asof"]}
            except Exception as exc:
                log.error("Preopen membership refresh failed: %s", type(exc).__name__)
                return {**summary, "status": "membership_unavailable"}
        if not monitoring_session(now):
            self.ready_at.clear()
            self.last_good.clear()
            return {**summary, "status": "off_hours" if now.year in HOLIDAY_YEARS else "unknown_calendar"}
        if self.day != now.date():
            self.day = now.date()
            self.ready_at.clear()
            self.last_good.clear()
            with self.conn:
                self.conn.execute("DELETE FROM intraday_state WHERE day<?", ((now-timedelta(days=7)).date().isoformat(),))
                self.conn.execute("DELETE FROM intraday_outbox WHERE day<?", ((now-timedelta(days=30)).date().isoformat(),))
        try:
            self.refresh_members(now)
        except Exception as exc:
            self.ready_at.clear()
            self.last_good.clear()
            log.error("0050 membership unavailable: %s", type(exc).__name__)
            return {**summary, "status": "membership_unavailable"}
        members = {r["code"]: r["name"] for r in self.snapshot["stocks"]}
        try:
            quotes = self.fetcher(members)
        except Exception as exc:
            self.ready_at.clear()
            self.last_good.clear()
            log.error("Yahoo fetch failed: %s", type(exc).__name__)
            return {**summary, "status": "quote_error"}
        # Freshness must be measured after network I/O, not before it.
        if live_clock:
            now = taiwan_now()
            summary["at"] = now.isoformat()
        if not monitoring_session(now):
            return {**summary, "status": "off_hours"}
        counts = Counter()
        delays = []
        events = []
        for code, name in members.items():
            signal = evaluate(quotes.get(code, []), now)
            counts[signal["status"]] += 1
            if "delay_seconds" in signal:
                delays.append(signal["delay_seconds"])
            if signal["status"] != "ok":
                self.ready_at.pop(code, None)
                self.last_good.pop(code, None)
                continue
            if code not in self.ready_at or (now-self.last_good.get(code, now)).total_seconds() > MAX_DELAY:
                self.ready_at[code] = now + timedelta(seconds=WARMUP_SECONDS)
            self.last_good[code] = now
            if now < self.ready_at[code]:
                counts["warming"] += 1
                continue
            event = record_signal(self.conn, code, name, signal, self.snapshot, send=self.send)
            if event:
                events.append(event)
                log.info("%s", json.dumps(event, ensure_ascii=False))
        delivery = dispatch(self.conn, self.client, self.channel, now, set(members), self.dashboard_url,
                            clock=taiwan_now if live_clock else None) if self.send else {"sent": 0, "expired": 0}
        return {**summary, "status": ("ok" if counts["ok"] == len(members) else
                           "degraded" if counts["ok"] else "quote_unavailable"),
                "members": len(members), "membership_asof": self.snapshot["asof"],
                "counts": dict(counts), "max_delay_seconds": max(delays, default=None),
                "events": len(events), **delivery}


def main(argv=None):
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Explicitly enable Slack delivery")
    parser.add_argument("--once", action="store_true", help="One diagnostic cycle; no startup alerts")
    parser.add_argument("--interval", type=int, default=60, choices=range(30, 61), metavar="30..60")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--members-cache", type=Path, default=etf_members.DEFAULT_CACHE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token, channel = os.getenv("SLACK_BOT_TOKEN", "").strip(), os.getenv("SLACK_CHANNEL", "").strip()
    if args.send and (not token or not channel):
        parser.error("--send requires SLACK_BOT_TOKEN and SLACK_CHANNEL")
    client = None
    if args.send:
        from slack_sdk import WebClient
        client = WebClient(token=token, timeout=10, retry_handlers=[])
    state = args.state or repo_file(".cache", "intraday.db" if args.send else "intraday-dry-run.db")
    import yfinance as yf
    yf.set_tz_cache_location(str(state.parent / "yfinance"))
    with worker_lock(state):
        conn = open_store(state, send=args.send)
        monitor = Monitor(conn, send=args.send, client=client, channel=channel,
                          dashboard_url=os.getenv("DASHBOARD_PUBLIC_URL", ""), members_path=args.members_cache)
        try:
            while True:
                began = time.monotonic()
                summary = monitor.cycle()
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                if args.once:
                    return 0 if summary["status"] in ("ok", "off_hours", "preopen") else 1
                delay = args.interval if summary["status"] not in ("quote_error", "quote_unavailable", "membership_unavailable") else 300
                time.sleep(max(1, delay - (time.monotonic()-began)))
        except KeyboardInterrupt:
            return 0
        finally:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
