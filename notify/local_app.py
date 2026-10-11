"""Desktop app for configurable local intraday monitoring."""
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
from notify.intraday_alert import (
    Monitor, load_settings, open_store, parse_codes, recent_events,
    save_settings, validate_settings, worker_lock,
)
from web.tw_calendar import taiwan_now

log = logging.getLogger(__name__)
DEFAULT_STATE = repo_file(".cache", "intraday.db")
POLL_SECONDS = 60
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
    "membership_unavailable": "監控清單無法使用",
    "error": "執行錯誤",
}
UNIVERSE_LABELS = {"0050": "0050 成分股", "all": "全部上市股票", "custom": "自訂清單"}
LABEL_UNIVERSES = {value: key for key, value in UNIVERSE_LABELS.items()}
QUOTE_COUNT_LABELS = {
    "stale": "行情過期",
    "missing": "沒有行情",
    "incomplete": "分鐘線不完整",
    "invalid": "行情無效",
}


def summary_status_text(summary):
    """Return an actionable UI status for quote availability summaries."""
    status = summary.get("status", "error")
    counts = summary.get("counts", {})
    checked = summary.get("checked", 0)
    if status == "quote_unavailable" and checked:
        if counts.get("stale") == checked:
            return "Yahoo 行情延遲"
        if counts.get("missing") == checked:
            return "Yahoo 沒有行情資料"
    return STATUS_TEXT.get(status, status)


def quote_problem_text(summary):
    counts = summary.get("counts", {})
    return "、".join(
        f"{label} {counts[key]} 檔"
        for key, label in QUOTE_COUNT_LABELS.items()
        if counts.get(key)
    )


def event_row(event):
    """Format one alert as a single table row."""
    end = datetime.fromisoformat(event["bar_end"]).strftime("%Y-%m-%d %H:%M")
    stock = f"{event['name']}（{event['code']}）"
    rise = f"+{event['rise_pct']:.2f}%"
    prices = f"{event['low']:g} → {event['close']:g}"
    return end, stock, rise, prices


class IntradayApp:
    def __init__(self, root, *, state=DEFAULT_STATE,
                 members_cache=etf_members.DEFAULT_CACHE, settings_path=None):
        self.root = root
        self.state = Path(state)
        self.members_cache = Path(members_cache)
        self.settings_path = Path(settings_path or self.state.with_name("intraday-settings.json"))
        self.settings = load_settings(self.settings_path)
        self.messages = queue.SimpleQueue()
        self.settings_updates = queue.SimpleQueue()
        self.stop_event = threading.Event()
        self.paused = threading.Event()
        self.wake = threading.Event()
        self.thread = None

        root.title("StockAlert 盤中監控")
        root.geometry("840x620")
        root.minsize(720, 520)
        root.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="啟動中")
        self.detail = tk.StringVar(value="等待第一次檢查")
        ttk.Label(frame, textvariable=self.status, font=("Helvetica", 20, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.detail).pack(anchor="w", pady=(4, 14))

        settings_box = ttk.LabelFrame(frame, text="警示設定", padding=10)
        settings_box.pack(fill="x", pady=(0, 12))
        self.threshold = tk.StringVar(value=f"{self.settings['threshold_pct']:g}")
        self.universe = tk.StringVar(value=UNIVERSE_LABELS[self.settings["universe"]])
        self.custom_codes = tk.StringVar(value=",".join(self.settings["custom_codes"]))
        self.settings_hint = tk.StringVar()
        ttk.Label(settings_box, text="五分鐘漲幅門檻").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(settings_box, from_=0.1, to=20, increment=0.1,
                    textvariable=self.threshold, width=7).grid(row=0, column=1, padx=(6, 3))
        ttk.Label(settings_box, text="%").grid(row=0, column=2, sticky="w")
        ttk.Label(settings_box, text="監控範圍").grid(row=0, column=3, padx=(18, 6))
        universe = ttk.Combobox(settings_box, textvariable=self.universe, state="readonly",
                                values=list(LABEL_UNIVERSES), width=14)
        universe.grid(row=0, column=4, sticky="w")
        universe.bind("<<ComboboxSelected>>", self._universe_changed)
        ttk.Button(settings_box, text="套用", command=self.apply_settings).grid(
            row=0, column=5, padx=(12, 0))
        ttk.Label(settings_box, text="自訂代號").grid(row=1, column=0, sticky="w", pady=(9, 0))
        self.custom_entry = ttk.Entry(settings_box, textvariable=self.custom_codes)
        self.custom_entry.grid(row=1, column=1, columnspan=5, sticky="ew", padx=(6, 0), pady=(9, 0))
        settings_box.columnconfigure(5, weight=1)
        ttk.Label(settings_box, textvariable=self.settings_hint, foreground="#7a4f00").grid(
            row=2, column=0, columnspan=6, sticky="w", pady=(7, 0))
        self._universe_changed()

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
                monitor = Monitor(conn, members_path=self.members_cache, settings=self.settings)
                try:
                    self.messages.put(("history", recent_events(conn)))
                    while not self.stop_event.is_set():
                        while True:
                            try:
                                monitor.configure(self.settings_updates.get_nowait())
                            except queue.Empty:
                                break
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
                        delay = (POLL_SECONDS if summary["status"] not in
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
        self.status.set(summary_status_text(summary))
        checked = datetime.fromisoformat(summary["at"]).strftime("%Y-%m-%d %H:%M:%S")
        parts = [f"最後檢查：{checked}（台灣時間）"]
        if summary.get("members"):
            label = UNIVERSE_LABELS.get(summary.get("universe"), summary.get("universe", ""))
            scope = f"範圍：{label} {summary['members']} 檔"
            if summary.get("checked") and summary["checked"] < summary["members"]:
                scope += f"（本輪 {summary['checked']} 檔）"
            parts.append(scope)
        if summary.get("threshold_pct") is not None:
            parts.append(f"門檻：{summary['threshold_pct']:g}%")
        if summary.get("max_delay_seconds") is not None:
            parts.append(f"最大行情延遲：{summary['max_delay_seconds']:.0f} 秒")
        problems = quote_problem_text(summary)
        if problems:
            parts.append(problems)
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

    def _universe_changed(self, _event=None):
        kind = LABEL_UNIVERSES.get(self.universe.get(), "0050")
        self.custom_entry.configure(state="normal" if kind == "custom" else "disabled")
        if kind == "all":
            self.settings_hint.set("全部約 1,000 檔，每輪 50 檔；每檔約 21 分鐘輪到一次，避免 Yahoo 限流。")
        elif kind == "custom":
            self.settings_hint.set("輸入四位數代號，以逗號或空格分隔；超過 50 檔會分批輪詢。")
        else:
            self.settings_hint.set("0050 約 50 檔，每 60 秒全部檢查一次。")

    def apply_settings(self):
        try:
            settings = validate_settings({
                "threshold_pct": self.threshold.get(),
                "universe": LABEL_UNIVERSES[self.universe.get()],
                "custom_codes": parse_codes(self.custom_codes.get()),
            })
            self.settings = save_settings(self.settings_path, settings)
        except (KeyError, OSError, ValueError) as exc:
            messagebox.showerror("警示設定", str(exc))
            return
        self.settings_updates.put(self.settings)
        self.status.set("設定已套用")
        self.wake.set()

    def add_test_event(self):
        now = taiwan_now().isoformat()
        event = {
            "name": "測試股票", "code": "0000", "window_start": now,
            "bar_end": now, "low": 100, "close": 102,
            "rise_pct": self.settings["threshold_pct"],
        }
        self._show_event(event)

    def close(self):
        self.stop_event.set()
        self.wake.set()
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--members-cache", type=Path, default=etf_members.DEFAULT_CACHE)
    parser.add_argument("--settings", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = tk.Tk()
    IntradayApp(root, state=args.state, members_cache=args.members_cache,
                settings_path=args.settings)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
