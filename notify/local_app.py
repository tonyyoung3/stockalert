"""macOS desktop app for local 0050 intraday monitoring."""
from __future__ import annotations

import argparse
from datetime import datetime
import logging
from pathlib import Path
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from data.paths import repo_file
from market import etf_members
from notify.intraday_alert import Monitor, mark_notification, open_store, worker_lock
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


def event_text(event):
    start = datetime.fromisoformat(event["window_start"]).strftime("%H:%M")
    end = datetime.fromisoformat(event["bar_end"]).strftime("%H:%M")
    return (
        f"{event['name']}（{event['code']}） {start}–{end}\n"
        f"{event['low']:g} → {event['close']:g}，+{event['rise_pct']:.2f}%"
    )


def send_macos_notification(event, *, runner=subprocess.run):
    """Send a native notification without interpolating data into AppleScript."""
    script = (
        "on run argv\n"
        "display notification (item 1 of argv) with title (item 2 of argv) "
        "subtitle (item 3 of argv)\n"
        "end run"
    )
    return runner(
        ["/usr/bin/osascript", "-e", script, event_text(event),
         "0050 盤中急拉", f"最新完成分鐘 +{event['rise_pct']:.2f}%"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


def deliver_events(conn, events, *, notifier=send_macos_notification, now_fn=taiwan_now):
    failures = []
    for event in events:
        try:
            notifier(event)
        except Exception as exc:
            mark_notification(conn, event["event_id"], error=exc)
            failures.append((event, exc))
            log.warning("Local notification failed event=%s type=%s",
                        event["event_id"], type(exc).__name__)
        else:
            mark_notification(conn, event["event_id"], notified_at=now_fn())
    return failures


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
        ttk.Button(buttons, text="測試通知", command=self.test_notification).pack(side="left")

        ttk.Label(frame, text="最近事件", font=("Helvetica", 13, "bold")).pack(anchor="w")
        self.events = tk.Text(frame, height=14, wrap="word", state="disabled")
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
                    while not self.stop_event.is_set():
                        if self.paused.is_set():
                            self.messages.put(("summary", {"status": "paused", "at": taiwan_now().isoformat()}))
                            self.wake.wait(1)
                            self.wake.clear()
                            continue
                        began = time.monotonic()
                        summary = monitor.cycle()
                        events = summary.pop("event_items", [])
                        failures = deliver_events(conn, events)
                        summary["notification_errors"] = len(failures)
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
        if summary.get("notification_errors"):
            parts.append("macOS 通知失敗，請檢查通知權限")
        self.detail.set("｜".join(parts))

    def _show_event(self, event):
        line = f"{datetime.fromisoformat(event['bar_end']).strftime('%Y-%m-%d %H:%M')}  {event_text(event)}\n\n"
        self.events.configure(state="normal")
        self.events.insert("1.0", line)
        self.events.configure(state="disabled")

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

    def test_notification(self):
        event = {
            "name": "測試股票", "code": "0000", "window_start": taiwan_now().isoformat(),
            "bar_end": taiwan_now().isoformat(), "low": 100, "close": 102, "rise_pct": 2,
        }
        try:
            send_macos_notification(event)
        except Exception as exc:
            messagebox.showerror("通知失敗", f"{type(exc).__name__}: {exc}")

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
