#!/usr/bin/env python3
"""回補 0050(元大台灣50)成分股的日 K 資料

用法:
    python -m market.backfill_0050            # 回補全部 50 檔 × 730 天(預設)
    python -m market.backfill_0050 365        # 指定天數
    python -m market.backfill_0050 730 --top 20   # 只補權重前 20 大
    python -m market.backfill_0050 --list     # 只列出清單不回補

預估:50 檔 × 約 25 個月 = 約 1,250 次請求,以 4 秒間隔約 1.4 小時。
可隨時 Ctrl+C 中斷,重跑會自動跳過已完成的股票。

成分股由 market.etf_members 從元大官方同步；缺少有效名單時停止。

⚠️ 存活者偏誤警告:此清單是「今天的」成分股。用它回測歷史會高估報酬——
   兩年前就在名單內、後來被剔除的股票(通常是表現差的)不會出現在這裡。
   若要做嚴謹的歷史回測,需要逐季的歷史成分股名單。
"""
import sys

from market.backfill import backfill_stocks, SLEEP
import market.backfill as backfill

from market.etf_members import load_members


def main():
    args = [a for a in sys.argv[1:]]
    snapshot = load_members()
    constituents = sorted([(r["code"], r["name"], r["weight"])
                           for r in snapshot["stocks"]], key=lambda r: -r[2])
    if "--list" in args:
        print(f"0050 成分股({len(constituents)} 檔,資料日期 {snapshot['asof']}):\n")
        for i, (sid, name, w) in enumerate(constituents, 1):
            print(f"{i:3d}. {sid}  {name:10s} {w:6.2f}%")
        print(f"\n權重合計 {sum(w for _, _, w in constituents):.2f}%")
        return

    top = None
    if "--top" in args:
        i = args.index("--top")
        top = int(args[i + 1])
        args = args[:i] + args[i + 2:]
    days = int(args[0]) if args else 730

    picks = constituents[:top] if top else constituents
    ids = [sid for sid, _, _ in picks]
    months = days // 30 + 1
    est = len(ids) * months
    print(f"回補 {len(ids)} 檔 × 約 {months} 個月 = 約 {est:,} 次請求")
    print(f"以 {SLEEP} 秒間隔預估 {est * SLEEP / 3600:.1f} 小時(可中斷續跑)")
    if top:
        print(f"(權重前 {top} 大,涵蓋 {sum(w for _, _, w in picks):.1f}% 指數權重)")
    if input("確定執行?(y/N) ").strip().lower() != "y":
        raise SystemExit("已取消")

    backfill_stocks(ids, days)


if __name__ == "__main__":
    main()
