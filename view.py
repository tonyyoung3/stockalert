#!/usr/bin/env python3
"""快速查看已收集的資料
  python view.py              # 總覽:指數最近10筆 + 最新一日外資買賣超前後10名
  python view.py 2330         # 查個股外資買賣超歷史
  python view.py csv          # 全部匯出成 CSV (taiex.csv, foreign.csv)
"""
import sys
import sqlite3
import csv
from pathlib import Path

DB = Path(__file__).parent / "twse_data.db"
conn = sqlite3.connect(DB)


def show(title, rows, headers):
    print(f"\n== {title} ==")
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
              for i, h in enumerate(headers)]
    print("  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
    for r in rows:
        print("  ".join(str(v).rjust(w) if isinstance(v, (int, float)) else str(v).ljust(w)
                        for v, w in zip(r, widths)))


arg = sys.argv[1] if len(sys.argv) > 1 else None

if arg == "csv":
    for table, fname in [("taiex_hourly", "taiex.csv"), ("foreign_daily", "foreign.csv")]:
        cur = conn.execute(f"SELECT * FROM {table}")
        with open(Path(__file__).parent / fname, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            w.writerows(cur)
        print(f"已匯出 {fname}")
elif arg:  # 個股代號
    rows = conn.execute(
        "SELECT trade_date, stock_name, foreign_buy, foreign_sell, foreign_net "
        "FROM foreign_daily WHERE stock_id=? ORDER BY trade_date DESC LIMIT 30", (arg,)).fetchall()
    show(f"{arg} 外資買賣超(股)", rows, ["日期", "名稱", "買進", "賣出", "買賣超"])
else:
    rows = conn.execute(
        "SELECT ts, index_value, change FROM taiex_hourly ORDER BY ts DESC LIMIT 10").fetchall()
    show("大盤指數(最近10筆)", rows, ["時間", "指數", "漲跌"])
    latest = conn.execute("SELECT MAX(trade_date) FROM foreign_daily").fetchone()[0]
    top = conn.execute(
        "SELECT stock_id, stock_name, foreign_net FROM foreign_daily "
        "WHERE trade_date=? ORDER BY foreign_net DESC LIMIT 10", (latest,)).fetchall()
    bot = conn.execute(
        "SELECT stock_id, stock_name, foreign_net FROM foreign_daily "
        "WHERE trade_date=? ORDER BY foreign_net ASC LIMIT 10", (latest,)).fetchall()
    show(f"{latest} 外資買超前10(股)", top, ["代號", "名稱", "買賣超"])
    show(f"{latest} 外資賣超前10(股)", bot, ["代號", "名稱", "買賣超"])
