#!/usr/bin/env bash
# 一次補齊「最近資料」的更新腳本 —— 把各個收集器都追到今天。
#
# 用法:
#   ./update_recent.sh              # 預設補最近 14 天(補平常忘記跑的空隙)
#   ./update_recent.sh 30           # 補最近 30 天
#   ./update_recent.sh 14 --stocks  # 同時回補個股日K(較慢,視股票數量可能要數十分鐘)
#
# 全部指令都是冪等的(INSERT OR REPLACE / 已有日期自動跳過),
# 重複跑同一段日期不會出問題,天數抓寬一點也無妨。

set -e
cd "$(dirname "$0")"

DAYS="${1:-14}"
DO_STOCKS=0
[ "$2" = "--stocks" ] && DO_STOCKS=1

PY="venv/bin/python"
[ -x "$PY" ] || PY="python3"   # 沒有 venv 就退回系統 python3

echo "=== 更新最近 ${DAYS} 天資料 $(date '+%Y-%m-%d %H:%M') ==="

echo "--- [1/4] 大盤日K + 小時K + 外資買賣超 + 融資融券 (backfill.py) ---"
$PY backfill.py "$DAYS"

echo "--- [2/4] 期交所三大法人未平倉:期貨 + 選擇權 (taifex_collector.py) ---"
$PY taifex_collector.py recent "$DAYS"

echo "--- [3/4] 美股 ETF 小時K (us_collector.py) ---"
$PY us_collector.py hourly SPY,QQQ,IWM "$DAYS"

if [ "$DO_STOCKS" = "1" ]; then
    echo "--- [4/4] 個股日K(全部成分股,可能較慢) ---"
    yes | $PY backfill.py stock all "$DAYS"
else
    echo "--- [4/4] 略過個股日K(加 --stocks 參數才會執行) ---"
fi

echo ""
echo "=== 完成 $(date '+%Y-%m-%d %H:%M') ==="
echo ""
echo "現況檢查:"
$PY taifex_collector.py summary
