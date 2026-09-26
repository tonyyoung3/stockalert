"""Desktop app for local 0050 intraday monitoring."""
from __future__ import annotations

import argparse
from datetime import datetime
import logging
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from data.paths import repo_file
from market import etf_members
from notify.intraday_alert import Monitor, open_store, recent_events, worker_lock
from web.tw_calendar import taiwan_now

log = logging.getLogger(__name__)
DEFAULT_STATE = repo_file(".cache", "intraday.db")
STATUS_TEXT = {
    "starting": "啟動中",
    "paused": "已暫停",
    "preopen": "盤前更新成分股",
    "off_hours": "非交易時間",
    "unknown_calendar": "交易日曆尚未更新",
    "ok": "監控正常",
    "degraded": "部分行情異常",
    "quote_unavailable": "行情無法使用",
    "quote_error": "行情下載失敗",
    "membership_unavailable": "0050 名單無法使用",
    "error": "執行錯誤",
}


def event_row(event):
    """Format one alert as a single table row."""
    end = datetime.fromisoformat(event["bar_end"]).strftime("%Y-%m-%d %H:%M")
    stock = f"{event['name']}（{event['code']}）"
    rise = f"+{event['rise_pct']:.2f}%"
    prices = f"{event['low']:g} → {event['close']:g}"
    return end, stock, rise, prices


class IntradayApp:
    def __init__(self, root, *, state=DEFAULT_STATE, interval=60,
                 members_cache=etf_members.DEFAULT_CACHE):
        self.root = root
        self.state = Path(state)
        self.interval = interval
        self.members_cache = Path(members_cache)
        self.messages = queue.SimpleQueue()
        self.stop_event = threading.Event()
        self.paused = threading.Event()
        self.wake = threading.Event()
        self.thread = None

        root.title("StockAlert 0050 盤中監控")
        root.geometry("720x460")
        root.minsize(620, 380)
        root.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="啟動中")
        self.detail = tk.StringVar(value="等待第一次檢查")
        ttk.Label(frame, textvariable=self.status, font=("Helvetica", 20, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.detail).pack(anchor="w", pady=(4, 14))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(0, 14))
        self.pause_button = ttk.Button(buttons, text="暫停", command=self.toggle_pause)
        self.pause_button.pack(side="left")
        ttk.Button(buttons, text="立即檢查", command=self.check_now).pack(side="left", padx=8)
        ttk.Button(buttons, text="加入測試警示", command=self.add_test_event).pack(side="left")

        ttk.Label(frame, text="最近警示", font=("Helvetica", 13, "bold")).pack(anchor="w")
        columns = ("time", "stock", "rise", "prices")
        self.events = ttk.Treeview(frame, columns=columns, show="headings", height=14)
        for key, title, width, anchor in (
            ("time", "時間（台灣）", 150, "w"),
            ("stock", "股票", 220, "w"),
            ("rise", "五分鐘漲幅", 100, "e"),
            ("prices", "最低價 → 收盤價", 150, "e"),
        ):
            self.events.heading(key, text=title)
            self.events.column(key, width=width, anchor=anchor)
        self.events.pack(fill="both", expand=True, pady=(6, 0))

        self.thread = threading.Thread(target=self._worker, name="intraday-monitor", daemon=True)
        self.thread.start()
        root.after(200, self._drain_messages)

    def _worker(self):
        try:
            import yfinance as yf
            yf.set_tz_cache_location(str(self.state.parent / "yfinance"))
            with worker_lock(self.state):
                conn = open_store(self.state)
                monitor = Monitor(conn, members_path=self.members_cache)
                try:
                    self.messages.put(("history", recent_events(conn)))
                    while not self.stop_event.is_set():
                        if self.paused.is_set():
                            self.messages.put(("summary", {"status": "paused", "at": taiwan_now().isoformat()}))
                            self.wake.wait(1)
                            self.wake.clear()
                            continue
                        began = time.monotonic()
                        summary = monitor.cycle()
                        events = summary.pop("event_items", [])
                        self.messages.put(("summary", summary))
                        for event in events:
                            self.messages.put(("event", event))
                        delay = (self.interval if summary["status"] not in
                                 ("quote_error", "quote_unavailable", "membership_unavailable") else 300)
                        self.wake.wait(max(1, delay - (time.monotonic() - began)))
                        self.wake.clear()
                finally:
                    conn.close()
        except Exception as exc:
            log.exception("Local intraday app stopped")
            self.messages.put(("fatal", exc))

    def _drain_messages(self):
        while True:
            try:
                kind, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "summary":
                self._show_summary(value)
            elif kind == "history":
                for event in value:
                    self._show_event(event, prepend=False)
            elif kind == "event":
                self._show_event(value)
            elif kind == "fatal":
                self.status.set("監控已停止")
                self.detail.set(str(value))
                messagebox.showerror("StockAlert", str(value))
        if not self.stop_event.is_set():
            self.root.after(200, self._drain_messages)

    def _show_summary(self, summary):
        status = summary.get("status", "error")
        self.status.set(STATUS_TEXT.get(status, status))
        checked = datetime.fromisoformat(summary["at"]).strftime("%Y-%m-%d %H:%M:%S")
        parts = [f"最後檢查：{checked}（台灣時間）"]
        if summary.get("members"):
            parts.append(f"成分股：{summary['members']} 檔")
        if summary.get("max_delay_seconds") is not None:
            parts.append(f"最大行情延遲：{summary['max_delay_seconds']:.0f} 秒")
        self.detail.set("｜".join(parts))

    def _show_event(self, event, *, prepend=True):
        self.events.insert("", 0 if prepend else "end", values=event_row(event))

    def toggle_pause(self):
        if self.paused.is_set():
            self.paused.clear()
            self.pause_button.configure(text="暫停")
            self.wake.set()
        else:
            self.paused.set()
            self.pause_button.configure(text="繼續")
            self.status.set("已暫停")

    def check_now(self):
        if self.paused.is_set():
            self.paused.clear()
            self.pause_button.configure(text="暫停")
        self.status.set("檢查中")
        self.wake.set()

    def add_test_event(self):
        now = taiwan_now().isoformat()
        event = {
            "name": "測試股票", "code": "0000", "window_start": now,
            "bar_end": now, "low": 100, "close": 102, "rise_pct": 2,
        }
        self._show_event(event)

    def close(self):
        self.stop_event.set()
        self.wake.set()
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=60, choices=range(30, 61), metavar="30..60")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--members-cache", type=Path, default=etf_members.DEFAULT_CACHE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = tk.Tk()
    IntradayApp(root, state=args.state, interval=args.interval, members_cache=args.members_cache)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
