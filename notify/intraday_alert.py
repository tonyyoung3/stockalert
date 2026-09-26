"""0050 five-minute +2% signal engine for the local desktop app.

The monitor is deliberately local-only: it stores cooldown and event history in
SQLite and never sends data to Slack or a cloud scheduler. ``local_app`` owns
the UI and native notification delivery.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, time as day_time, timedelta
import fcntl
import json
import logging
from pathlib import Path
import sqlite3
import time
import uuid

from data.paths import repo_file
from market import etf_members
from market.intraday_prices import MAX_DELAY, evaluate, fetch_minutes, monitoring_session
from web.tw_calendar import HOLIDAY_YEARS, is_tw_trading_day, taiwan_now

log = logging.getLogger(__name__)
COOLDOWN_SECONDS = 15 * 60
WARMUP_SECONDS = 5 * 60
SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_state (
  code TEXT NOT NULL, day TEXT NOT NULL, last_bar TEXT NOT NULL,
  armed INTEGER NOT NULL, cooldown_until TEXT,
  PRIMARY KEY (code, day)
);
CREATE TABLE IF NOT EXISTS intraday_events (
  event_id TEXT PRIMARY KEY, code TEXT NOT NULL, day TEXT NOT NULL,
  bar_end TEXT NOT NULL, payload TEXT NOT NULL,
  notified_at TEXT, notification_error TEXT
);
CREATE INDEX IF NOT EXISTS intraday_events_day ON intraday_events(day, bar_end);
"""


def open_store(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def worker_lock(path):
    """Prevent two local app instances from using the same state database."""
    path = Path(str(path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another intraday app is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def record_signal(conn, code, name, signal, snapshot):
    """Atomically update per-stock cooldown and record one unique local event."""
    if code not in {row["code"] for row in snapshot["stocks"]} or signal["status"] != "ok":
        return None
    end = datetime.fromisoformat(signal["bar_end"])
    day = end.date().isoformat()
    with conn:
        row = conn.execute(
            "SELECT * FROM intraday_state WHERE code=? AND day=?", (code, day)
        ).fetchone()
        if row and end.isoformat() <= row["last_bar"]:
            return None
        armed = bool(row["armed"]) if row else True
        cooldown = row["cooldown_until"] if row else None
        event = None
        if not signal["triggered"]:
            armed = True
        elif armed and (not cooldown or end >= datetime.fromisoformat(cooldown)):
            event_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL, "0050-rise-v1:" + code + ":" + end.isoformat()
            ))
            event = {
                **signal,
                "event_id": event_id,
                "code": code,
                "name": name,
                "membership_asof": snapshot["asof"],
                "membership_version": snapshot["version"],
            }
            conn.execute(
                "INSERT OR IGNORE INTO intraday_events "
                "(event_id,code,day,bar_end,payload) VALUES (?,?,?,?,?)",
                (event_id, code, day, end.isoformat(), json.dumps(event, ensure_ascii=False)),
            )
            cooldown = (end + timedelta(seconds=COOLDOWN_SECONDS)).isoformat()
            armed = False
        elif signal["triggered"]:
            # A crossing suppressed during cooldown still belongs to that wave.
            armed = False
        conn.execute(
            "INSERT OR REPLACE INTO intraday_state VALUES (?,?,?,?,?)",
            (code, day, end.isoformat(), int(armed), cooldown),
        )
    return event


def mark_notification(conn, event_id, *, notified_at=None, error=None):
    """Persist the result of the single local notification attempt."""
    with conn:
        conn.execute(
            "UPDATE intraday_events SET notified_at=?, notification_error=? WHERE event_id=?",
            (taiwan_now(notified_at).isoformat() if notified_at and not error else None,
             type(error).__name__ if error else None, event_id),
        )


class Monitor:
    def __init__(self, conn, *, members_path=etf_members.DEFAULT_CACHE, loader=None, fetcher=None):
        self.conn = conn
        self.members_path = members_path
        self.loader = loader or etf_members.load_members
        self.fetcher = fetcher or fetch_minutes
        self.snapshot = None
        self.members_checked = None
        self.day = None
        self.ready_at = {}
        self.last_good = {}

    def refresh_members(self, now):
        if (not self.snapshot or not self.members_checked
                or self.members_checked.date() != now.date()
                or (now - self.members_checked).total_seconds() >= 900):
            self.snapshot = self.loader(self.members_path, now=now)
            self.members_checked = now
        etf_members.validate(self.snapshot, now.date())

    def cycle(self, now=None):
        live_clock = now is None
        now = taiwan_now(now)
        summary = {"at": now.isoformat(), "mode": "local"}
        if (now.year in HOLIDAY_YEARS and is_tw_trading_day(now.date())
                and day_time(8, 45) <= now.time() < day_time(9)):
            try:
                self.refresh_members(now)
                return {**summary, "status": "preopen", "members": len(self.snapshot["stocks"]),
                        "membership_asof": self.snapshot["asof"], "event_items": []}
            except Exception as exc:
                log.error("Preopen membership refresh failed: %s", type(exc).__name__)
                return {**summary, "status": "membership_unavailable", "event_items": []}
        if not monitoring_session(now):
            self.ready_at.clear()
            self.last_good.clear()
            return {**summary,
                    "status": "off_hours" if now.year in HOLIDAY_YEARS else "unknown_calendar",
                    "event_items": []}
        if self.day != now.date():
            self.day = now.date()
            self.ready_at.clear()
            self.last_good.clear()
            with self.conn:
                self.conn.execute(
                    "DELETE FROM intraday_state WHERE day<?",
                    ((now - timedelta(days=7)).date().isoformat(),),
                )
                self.conn.execute(
                    "DELETE FROM intraday_events WHERE day<?",
                    ((now - timedelta(days=30)).date().isoformat(),),
                )
        try:
            self.refresh_members(now)
        except Exception as exc:
            self.ready_at.clear()
            self.last_good.clear()
            log.error("0050 membership unavailable: %s", type(exc).__name__)
            return {**summary, "status": "membership_unavailable", "event_items": []}
        members = {row["code"]: row["name"] for row in self.snapshot["stocks"]}
        try:
            quotes = self.fetcher(members)
        except Exception as exc:
            self.ready_at.clear()
            self.last_good.clear()
            log.error("Yahoo fetch failed: %s", type(exc).__name__)
            return {**summary, "status": "quote_error", "event_items": []}
        # Freshness must be measured after network I/O, not before it.
        if live_clock:
            now = taiwan_now()
            summary["at"] = now.isoformat()
        if not monitoring_session(now):
            return {**summary, "status": "off_hours", "event_items": []}
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
            if code not in self.ready_at or (now - self.last_good.get(code, now)).total_seconds() > MAX_DELAY:
                self.ready_at[code] = now + timedelta(seconds=WARMUP_SECONDS)
            self.last_good[code] = now
            if now < self.ready_at[code]:
                counts["warming"] += 1
                continue
            event = record_signal(self.conn, code, name, signal, self.snapshot)
            if event:
                events.append(event)
                log.info("%s", json.dumps(event, ensure_ascii=False))
        return {
            **summary,
            "status": ("ok" if counts["ok"] == len(members)
                       else "degraded" if counts["ok"] else "quote_unavailable"),
            "members": len(members),
            "membership_asof": self.snapshot["asof"],
            "counts": dict(counts),
            "max_delay_seconds": max(delays, default=None),
            "events": len(events),
            "event_items": events,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one diagnostic cycle")
    parser.add_argument("--interval", type=int, default=60, choices=range(30, 61), metavar="30..60")
    parser.add_argument("--state", type=Path, default=repo_file(".cache", "intraday.db"))
    parser.add_argument("--members-cache", type=Path, default=etf_members.DEFAULT_CACHE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import yfinance as yf
    yf.set_tz_cache_location(str(args.state.parent / "yfinance"))
    with worker_lock(args.state):
        conn = open_store(args.state)
        monitor = Monitor(conn, members_path=args.members_cache)
        try:
            while True:
                began = time.monotonic()
                summary = monitor.cycle()
                summary.pop("event_items", None)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                if args.once:
                    return 0 if summary["status"] in ("ok", "off_hours", "preopen") else 1
                delay = (args.interval if summary["status"] not in
                         ("quote_error", "quote_unavailable", "membership_unavailable") else 300)
                time.sleep(max(1, delay - (time.monotonic() - began)))
        except KeyboardInterrupt:
            return 0
        finally:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
