"""Configurable five-minute rise signal engine for the local desktop app.

The monitor is deliberately local-only: it stores cooldown and event history in
SQLite and never sends data to Slack or a cloud scheduler. ``local_app`` owns
the UI and displays alerts in its event table.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, time as day_time, timedelta
import fcntl
import hashlib
import json
import logging
from pathlib import Path
import re
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
POLL_SECONDS = 60
MAX_SYMBOLS_PER_CYCLE = 50
DEFAULT_SETTINGS = {"threshold_pct": 2.0, "universe": "0050", "custom_codes": []}
UNIVERSES = {"0050", "all", "custom"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_state (
  code TEXT NOT NULL, day TEXT NOT NULL, last_bar TEXT NOT NULL,
  armed INTEGER NOT NULL, cooldown_until TEXT,
  PRIMARY KEY (code, day)
);
CREATE TABLE IF NOT EXISTS intraday_events (
  event_id TEXT PRIMARY KEY, code TEXT NOT NULL, day TEXT NOT NULL,
  bar_end TEXT NOT NULL, payload TEXT NOT NULL
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


def parse_codes(value):
    """Normalize comma/space/newline separated Taiwan stock codes."""
    if isinstance(value, str):
        values = re.split(r"[\s,，;；]+", value.strip()) if value.strip() else []
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise ValueError("custom_codes must be text or a list")
    codes = []
    for raw in values:
        code = str(raw).strip().upper()
        if code.endswith(".TW"):
            code = code[:-3]
        if not re.fullmatch(r"[1-9][0-9]{3}", code):
            raise ValueError(f"Invalid Taiwan stock code: {raw}")
        if code not in codes:
            codes.append(code)
    return codes


def validate_settings(value):
    value = value if isinstance(value, dict) else {}
    universe = str(value.get("universe", "0050"))
    if universe not in UNIVERSES:
        raise ValueError("universe must be 0050, all, or custom")
    try:
        threshold = float(value.get("threshold_pct", 2))
    except (TypeError, ValueError) as exc:
        raise ValueError("threshold_pct must be a number") from exc
    if not 0.1 <= threshold <= 20:
        raise ValueError("threshold_pct must be between 0.1 and 20")
    codes = parse_codes(value.get("custom_codes", []))
    if universe == "custom" and not codes:
        raise ValueError("Custom list cannot be empty")
    return {"threshold_pct": threshold, "universe": universe, "custom_codes": codes}


def load_settings(path):
    try:
        return validate_settings(json.loads(Path(path).read_text()))
    except FileNotFoundError:
        return dict(DEFAULT_SETTINGS)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log.warning("Invalid intraday settings (%s); using defaults", type(exc).__name__)
        return dict(DEFAULT_SETTINGS)


def save_settings(path, settings):
    settings = validate_settings(settings)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)
    return settings


def load_all_stock_codes(path=repo_file("taiwan_stocks.txt")):
    codes = parse_codes(Path(path).read_text().splitlines())
    if not codes:
        raise ValueError("Taiwan stock list is empty")
    return codes


def local_snapshot(kind, codes, now):
    version = hashlib.sha256((kind + ":" + ",".join(codes)).encode()).hexdigest()[:16]
    return {
        "etf": kind, "asof": now.date().isoformat(), "version": version,
        "source": "taiwan_stocks.txt" if kind == "all" else "custom",
        "stocks": [{"code": code, "name": code, "weight": 1} for code in codes],
    }


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
                uuid.NAMESPACE_URL, "local-rise-v2:" + code + ":" + end.isoformat()
            ))
            event = {
                **signal,
                "event_id": event_id,
                "code": code,
                "name": name,
                "membership_asof": snapshot["asof"],
                "membership_version": snapshot["version"],
                "universe": snapshot["etf"],
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


def recent_events(conn, limit=100):
    """Return recent persisted alerts, newest first, for the local event table."""
    limit = max(1, min(int(limit), 500))
    rows = conn.execute(
        "SELECT payload FROM intraday_events ORDER BY bar_end DESC LIMIT ?", (limit,)
    ).fetchall()
    events = []
    for row in rows:
        try:
            events.append(json.loads(row["payload"]))
        except (TypeError, ValueError):
            log.warning("Skipping invalid intraday event payload")
    return events


class Monitor:
    def __init__(self, conn, *, members_path=etf_members.DEFAULT_CACHE,
                 all_stocks_path=repo_file("taiwan_stocks.txt"), settings=None,
                 loader=None, fetcher=None):
        self.conn = conn
        self.members_path = members_path
        self.all_stocks_path = all_stocks_path
        self.loader = loader or etf_members.load_members
        self.fetcher = fetcher or fetch_minutes
        self.settings = validate_settings(settings or DEFAULT_SETTINGS)
        self.snapshot = None
        self.members_checked = None
        self.batch_cursor = 0
        self.day = None
        self.ready_at = {}
        self.last_good = {}

    def configure(self, settings):
        settings = validate_settings(settings)
        universe_changed = (settings["universe"], settings["custom_codes"]) != (
            self.settings["universe"], self.settings["custom_codes"])
        self.settings = settings
        self.ready_at.clear()
        self.last_good.clear()
        if universe_changed:
            self.snapshot = None
            self.members_checked = None
            self.batch_cursor = 0

    def refresh_members(self, now):
        if self.settings["universe"] == "all":
            if not self.snapshot:
                self.snapshot = local_snapshot(
                    "all", load_all_stock_codes(self.all_stocks_path), now)
            return
        if self.settings["universe"] == "custom":
            if not self.snapshot:
                self.snapshot = local_snapshot("custom", self.settings["custom_codes"], now)
            return
        if (not self.snapshot or not self.members_checked
                or self.members_checked.date() != now.date()
                or (now - self.members_checked).total_seconds() >= 900):
            self.snapshot = self.loader(self.members_path, now=now)
            self.members_checked = now
        etf_members.validate(self.snapshot, now.date())

    def next_batch(self, members):
        codes = sorted(members)
        if len(codes) <= MAX_SYMBOLS_PER_CYCLE:
            return {code: members[code] for code in codes}
        start = self.batch_cursor % len(codes)
        selected = codes[start:start + MAX_SYMBOLS_PER_CYCLE]
        self.batch_cursor = (0 if start + MAX_SYMBOLS_PER_CYCLE >= len(codes)
                             else start + MAX_SYMBOLS_PER_CYCLE)
        return {code: members[code] for code in selected}

    def cycle(self, now=None):
        live_clock = now is None
        now = taiwan_now(now)
        summary = {"at": now.isoformat(), "mode": "local",
                   "universe": self.settings["universe"],
                   "threshold_pct": self.settings["threshold_pct"]}
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
            self.batch_cursor = 0
            if self.settings["universe"] != "0050":
                self.snapshot = None
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
            log.error("Monitoring universe unavailable: %s", type(exc).__name__)
            return {**summary, "status": "membership_unavailable", "event_items": []}
        members = {row["code"]: row["name"] for row in self.snapshot["stocks"]}
        batch = self.next_batch(members)
        rotating = len(members) > MAX_SYMBOLS_PER_CYCLE
        try:
            quotes = self.fetcher(batch)
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
        for code, name in batch.items():
            signal = evaluate(quotes.get(code, []), now, self.settings["threshold_pct"])
            counts[signal["status"]] += 1
            if "delay_seconds" in signal:
                delays.append(signal["delay_seconds"])
            if signal["status"] != "ok":
                self.ready_at.pop(code, None)
                self.last_good.pop(code, None)
                continue
            if (code not in self.ready_at
                    or (not rotating and
                        (now - self.last_good.get(code, now)).total_seconds() > MAX_DELAY)):
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
            "status": ("ok" if counts["ok"] == len(batch)
                       else "degraded" if counts["ok"] else "quote_unavailable"),
            "members": len(members),
            "checked": len(batch),
            "membership_asof": self.snapshot["asof"],
            "counts": dict(counts),
            "max_delay_seconds": max(delays, default=None),
            "events": len(events),
            "event_items": events,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one diagnostic cycle")
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
                delay = (POLL_SECONDS if summary["status"] not in
                         ("quote_error", "quote_unavailable", "membership_unavailable") else 300)
                time.sleep(max(1, delay - (time.monotonic() - began)))
        except KeyboardInterrupt:
            return 0
        finally:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
