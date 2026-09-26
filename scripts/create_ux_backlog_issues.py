#!/usr/bin/env python3
"""Open the UX-review backlog as GitHub (or GitLab) issues.

This agent cannot create issues (GitHub token is read-only; no GitLab remote).
Run locally with a token that can write issues:

  python3 scripts/create_ux_backlog_issues.py          # GitHub, this repo
  python3 scripts/create_ux_backlog_issues.py --gitlab # needs glab + a project
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

ISSUES = [
    {
        "key": 1,
        "title": "[Bug] HTTPServer 為單執行緒，長回測會凍結整台服務並觸發 Cloud Run 回收",
        "gh_labels": ["backend", "infra", "priority:high", "bug"],
        "gl_labels": ["backend", "infra", "priority::0", "type::bug"],
        "body": """## 問題

`web/dashboard.py` 使用 `HTTPServer`，一次只處理一個請求。回測是服務中最重的運算，執行期間所有請求被阻塞，**包含 `/health`**。Cloud Run 存活探測逾時後會回收容器，使用者看到的現象是「回測有時候沒有回應」，且不會產生任何錯誤訊息。

## 修正

```python
from http.server import ThreadingHTTPServer
server = ThreadingHTTPServer((host, candidate), Handler)
```

## 驗收條件

- [ ] 改用 `ThreadingHTTPServer`
- [ ] 確認 `_request_conn` ContextVar 在多執行緒下正確隔離（ContextVar 本身 per-thread，需測試佐證）
- [ ] 確認 `connect_for_backtest()` 每請求開新連線，無跨執行緒共用 sqlite conn
- [ ] `_IpRateLimiter` 已有 `threading.Lock`，確認無其他共享可變狀態
- [ ] 新增測試：一個慢請求執行中，`/health` 仍可在 1 秒內回應

## 風險

低。sqlite 連線本來就是每請求建立。主要風險是 Turso client 的執行緒安全性，需確認。
""",
    },
    {
        "key": 2,
        "title": "[Security] 回測 rate limit 綁在 auth 上，導致無認證的線上服務完全不限流",
        "gh_labels": ["backend", "security", "priority:high", "bug"],
        "gl_labels": ["backend", "security", "priority::0", "type::bug"],
        "body": """## 問題

```python
if auth_enabled() and not _backtest_limiter.allow(client_ip(self)):
    return 429
```

`auth_enabled()` 在 `DASHBOARD_USER` / `DASHBOARD_PASSWORD` 缺任一時回傳 False。線上部署未設這兩個變數（未帶憑證即可載入完整頁面與 API），因此生產環境同時處於「無認證」與「無限流」狀態。

邏輯方向相反：**沒有認證的情況才是最需要限流的**。`POST /api/backtest` 可觸發 15 年資料集全量掃描，配合 Cloud Run 自動擴容即為帳單放大器。

相關：#28（公開 Cloud Run 前加上基本存取控制）已關，但線上仍可能沒設憑證。

## 驗收條件

- [ ] `_backtest_limiter` 無條件生效，移除 `auth_enabled()` 前置條件
- [ ] 未認證時採用更嚴格的限制（例如 3 次/分鐘），已認證時維持 10 次/分鐘
- [ ] 生產環境設定 `DASHBOARD_USER` / `DASHBOARD_PASSWORD`
- [ ] 設定 Cloud Run `max-instances`（自用建議 2～3）作為成本硬上限
- [ ] 新增測試：`auth_enabled() == False` 時超過門檻仍回傳 429

## 備註

考慮改為「缺少認證變數時拒絕啟動」（fail closed），除非明確設定 `DASHBOARD_ALLOW_ANONYMOUS=1`。目前的 fail open 讓一次設定疏漏就等於公開服務。
""",
    },
    {
        "key": 3,
        "title": "[Security] rate limiter 信任 X-Forwarded-For 第一段，可被偽造繞過",
        "gh_labels": ["backend", "security", "priority:high", "bug"],
        "gl_labels": ["backend", "security", "priority::0", "type::bug"],
        "body": """## 問題

```python
xff = handler.headers.get("X-Forwarded-For")
if xff:
    return xff.split(",", 1)[0].strip()
```

XFF 第一段由客戶端提供，可任意偽造。每個請求帶不同假 IP 即可完全繞過限流。

Cloud Run 的負載平衡器將真實來源 IP **附加在末端**。

## 驗收條件

- [ ] 改取 `xff.split(",")[-1].strip()`，或使用 Cloud Run 注入的可信欄位
- [ ] 以環境變數控制信任的 proxy 層數，避免本機開發與雲端行為不一致
- [ ] 新增測試：偽造多段 XFF 時，限流仍以真實末段 IP 計算

**相依：** 與「回測 rate limit 綁在 auth 上」同一段程式碼，建議一起改。
""",
    },
    {
        "key": 4,
        "title": "[Observability] 例外被吞掉、log_message 靜音，服務無任何日誌",
        "gh_labels": ["backend", "infra", "priority:high", "bug"],
        "gl_labels": ["backend", "infra", "priority::0", "type::bug"],
        "body": """## 問題

```python
except Exception:
    result = {"error": "回測執行失敗"}
...
def log_message(self, *a):
    pass
```

回測例外全部被吞，存取日誌被靜音。整個服務沒有可觀測性。且失敗時回傳 **HTTP 200**，Cloud Run 的錯誤率監控會顯示一切正常。

## 驗收條件

- [ ] 例外改用 `logging.exception()` 記錄完整堆疊（stderr 自動進 Cloud Logging）
- [ ] 回測失敗回傳 500；規則格式錯誤回傳 400，與執行期錯誤區分
- [ ] 前端據狀態碼區分「規則有問題」與「服務出錯」，給不同提示
- [ ] `log_message` 保留 WARNING 以上層級，不再全部靜音
- [ ] 錯誤訊息不外洩堆疊內容給前端，僅記錄於伺服器端
""",
    },
    {
        "key": 5,
        "title": "[Test] backtest_engine.py 缺少專屬測試，交易語意無迴歸保護",
        "gh_labels": ["test", "backend", "priority:high"],
        "gl_labels": ["test", "backend", "priority::0", "type::test"],
        "body": """## 問題

倉庫測試覆蓋資料層很好，但 `web/backtest_engine.py` **沒有 `test_backtest_engine.py`**，僅在 `test_strategy_blocks.py` 被間接呼叫，測的是規則格式轉換而非引擎語意。

回測引擎錯誤不會 crash，只會產生看起來合理的錯誤數字。這是最危險的失效模式。

## 驗收條件

新增 `tests/test_backtest_engine.py`，以手工構造、可人工驗算的固定資料，對每個場景斷言**完整交易清單**（進場日、進場價、出場日、出場價、報酬），非僅最終統計量：

- [ ] 日內：第一小時高點突破進場，當日收盤出場
- [ ] 日內：啟用停損且當日觸及，驗證以停損價出場
- [ ] 日內：最早檢查時間之前的觸及不應進場
- [ ] 隔夜：隔日開盤出場 / 隔日收盤出場
- [ ] 隔夜：跳過週末，週五收盤不留倉
- [ ] 波段：停損停利同日皆觸發 → 保守假設先停損（程式碼註解已定義此語意）
- [ ] 波段：達最長持有天數強制出場
- [ ] 訊號落在資料尾端、無後續資料 → 交易被排除且計入 `unresolved`，不計入統計
- [ ] 來回成本正確從 `ret_gross` 扣至 `ret_net`
- [ ] 守門：`15y_daily + intraday` 回傳錯誤
- [ ] 守門：`intraday` + 收盤才確定的濾網回傳錯誤

## 備註

上述語意多數已寫在程式碼註解中。這個 issue 的本質是把註解轉成可執行的斷言。
""",
    },
    {
        "key": 6,
        "title": "[Chore] screener.db 誤入版控",
        "gh_labels": ["chore", "priority:medium"],
        "gl_labels": ["chore", "priority::1", "type::chore"],
        "body": """## 問題

`screener.db`（alerts + performance）在版控中。`.gitignore` 已排除 `twse_data.db`、`us_data.db`、`*.db-shm`、`*.db-wal`，唯獨漏掉此檔。可變狀態進版控會製造無謂的 diff 與合併衝突。

## 驗收條件

- [ ] `.gitignore` 加入 `screener.db`
- [ ] `git rm --cached screener.db`
- [ ] 確認首次啟動能自動建立空 schema（無此檔時不應 crash）
- [ ] README 說明本機如何取得初始資料
""",
    },
    {
        "key": 7,
        "title": "[Bug] date.today() 裸用，Cloud Run 為 UTC 導致台北早上 8 點前日期錯位",
        "gh_labels": ["backend", "priority:medium", "bug"],
        "gl_labels": ["backend", "priority::1", "type::bug"],
        "body": """## 問題

`notify/` 與 `web/freshness.py` 已正確使用 `ZoneInfo("Asia/Taipei")`，`daily_digest.py` 甚至有註解說明 Actions 的 `date.today()` 是 UTC。但以下位置仍裸用系統本地日期：

- `web/dashboard.py`（`api_alerts` 的 since 計算）
- `alertsdb/store.py`
- `data/cloud_db.py`

Cloud Run 執行於 UTC，因此台北時間 00:00–08:00 之間，儀表板的「今天」是昨天。

## 驗收條件

- [ ] 抽出共用模組（例如 `common/tz.py`）提供 `taipei_today()`
- [ ] 上述四處改用共用函式
- [ ] 加入 lint 規則或測試，禁止新程式碼裸用 `date.today()` / `datetime.now()`
- [ ] 新增測試：在 `TZ=UTC` 與 `TZ=Asia/Taipei` 兩種環境下跑同一組查詢，斷言結果相同
""",
    },
    {
        "key": 8,
        "title": "[Feature] compute_stats 缺少最少交易數門檻，小樣本仍顯示夏普值",
        "gh_labels": ["backend", "priority:medium", "enhancement"],
        "gl_labels": ["backend", "priority::1", "type::improvement"],
        "body": """## 問題

`compute_stats` 對 n=3 一樣計算並回傳夏普值與 t 統計量。這些指標在小樣本下沒有意義，但呈現形式與大樣本完全相同，容易誤導。

現有的 block bootstrap CI 在小樣本下至少會誠實給出很寬的區間，夏普值不會。

## 驗收條件

- [ ] 定義 `MIN_TRADES_FOR_STATS = 30`
- [ ] n 低於門檻時，`sharpe`、`t_stat`、`p_value` 回傳 null 並附 `low_sample: true`
- [ ] 前端在低樣本時顯示明確標示，不呈現這些數字
- [ ] EV、勝率、交易清單仍照常顯示（這些在小樣本下仍可解讀）
""",
    },
    {
        "key": 9,
        "title": "[Perf] 118KB HTML/JS 內嵌於 dashboard.py，無快取標頭",
        "gh_labels": ["backend", "web", "priority:medium", "enhancement"],
        "gl_labels": ["backend", "frontend", "priority::1", "type::improvement"],
        "body": """## 問題

`web/dashboard.py` 有一段約 118KB 的字串常數，佔全檔大部分。造成：無語法標示、無法 lint、每次請求重新 UTF-8 編碼、無 `Cache-Control` / `ETag`。

## 驗收條件

- [ ] 抽為獨立 `web/static/index.html`，以 `importlib.resources` 讀取
- [ ] 啟動時編碼一次為 bytes 並快取，不在每次請求重新編碼
- [ ] 加上 `ETag`（內容 hash）與 `Cache-Control`，支援 304
- [ ] 拆出 JS 為獨立檔案後納入 lint
- [ ] 確認 Docker 映像有正確包含 static 檔案
""",
    },
    {
        "key": 10,
        "title": "[Epic] 多檔個股橫向比較：從一檔一檔切分頁到全市場掃描",
        "gh_labels": ["epic", "scan"],
        "gl_labels": ["epic", "scan"],
        "body": """## 問題陳述

目前要判斷「哪幾檔籌碼在轉弱」「誰跟股價背離」，只能在個股分頁一檔一檔切著看。

原始碼審查發現：`/api/stock_margin`、`/api/broker_branch/top`、`/api/broker_branch/stock` 等端點**已經存在**，`market/broker_branch.py` 有實作與文件。資料與 API 都有了，缺的是**橫向比較的視圖與標準化指標**。

## 目標

1. 建立個股籌碼的標準化指標層（利用既有資料表）
2. 提供全市場掃描視圖（散布圖為主、表格為輔）
3. 監控（固定清單）與發掘（全市場）分開設計，共用同一套指標定義

## 成功條件

30 秒內回答：「今天全市場有哪些股票，籌碼行為明顯偏離自己的常態？其中哪些跟股價方向不一致？」

## 子議題

見 backlog #11–#20（腳本建立後會回填實際 issue 編號）。

相關已關 epic：#53 券商分點買賣超（v1 大盤優先）。

## 主線最小切片

#11 + #12 + #13 只做外資、只做 20 日、只做散布圖，跑一週實際資料看誤報率，再決定 z-score 參數與是否改用穩健版本。
""",
    },
    {
        "key": 11,
        "title": "[Data] 個股籌碼指標統一視圖 stock_chips_daily",
        "gh_labels": ["data", "backend", "priority:medium", "enhancement"],
        "gl_labels": ["data", "backend", "priority::2", "type::feature"],
        "body": """## 背景

籌碼資料散落於 stock_daily、外資買賣超、stock_margin、broker_branch 等來源，口徑不一，跨股票比較需在查詢端臨時 join。這是掃描功能的地基。

## User Story

作為使用者，我希望所有個股籌碼指標來自同一套口徑，這樣散布圖、表格、告警不會各自算出不同數字。

## 驗收條件

- [ ] 建立 view 或物化表 `stock_chips_daily`，主鍵 `(stock_id, trade_date)`
- [ ] 欄位：外資淨額、投信淨額、自營淨額、三大法人合計、融資餘額、融券餘額、融資增減、融券增減、成交量、收盤價
- [ ] 單位統一為「張」，欄位註解標明
- [ ] 缺漏日期以 NULL 表示，不以 0 填補（0 = 無買賣超，NULL = 無資料，語意不同）
- [ ] 建立 `(trade_date)` 與 `(stock_id, trade_date)` 索引
- [ ] 沿用既有 `tests/test_freshness.py` 模式，加入本表的完整度檢查

## 技術備註

分點資料量級與其他來源差一個數量級，不併入此表（見分點籌碼指標 issue）。先評估以 view 實作是否足夠 — Turso 上的物化表需額外的同步邏輯，能不做就不做。

**相依：** 無（阻擋籌碼異常度指標）
**Parent:** 多檔個股橫向比較 epic
""",
    },
    {
        "key": 12,
        "title": "[Feature] 籌碼異常度指標：以個股自身歷史為基準做標準化",
        "gh_labels": ["data", "backend", "priority:medium", "enhancement"],
        "gl_labels": ["data", "backend", "priority::2", "type::feature"],
        "body": """## 背景

訪談中對「籌碼轉弱」的定義是「**突然**開始大賣」。關鍵字是「突然」，基準是這檔股票自己的常態，而非全市場絕對張數。現行「外資買賣超前 15」用絕對值排序，永遠被大型股佔滿，答不了這個問題。

## 驗收條件

- [ ] 實作 `chip_zscore(stock_id, date, window_short, window_long)`：近 `window_short` 日累計淨額，相對過去 `window_long` 日同長度滾動累計的平均與標準差取 z-score
- [ ] 預設 `window_short=5`、`window_long=120`，兩者可調
- [ ] 同步輸出佔量比 `net / avg_volume` 作為交叉驗證
- [ ] 標準差趨近 0（長期無量）時回傳 NULL，不得回傳 Inf
- [ ] 樣本不足 `window_long` 者標記 `insufficient_history=true`，不參與排名
- [ ] 外資／投信／三大法人合計各算一組
- [ ] 單元測試：構造「前 100 日淨額近 0、最近 5 日大量賣超」假資料驗證 z-score 落在預期區間

## 開放問題

z-score 對厚尾分布敏感。台股籌碼資料大概率需要改用中位數與 MAD 的穩健版本，但先做 z-score，跑一週實際資料看誤報率再決定。

**相依：** stock_chips_daily
**Parent:** 多檔個股橫向比較 epic
""",
    },
    {
        "key": 13,
        "title": "[Feature] 全市場掃描散布圖：期間漲跌幅 × 籌碼異常度",
        "gh_labels": ["web", "scan", "priority:medium", "enhancement"],
        "gl_labels": ["frontend", "scan", "priority::2", "type::feature"],
        "body": """## 背景

訪談排序中此視圖為第一優先。四象限結構同時回答監控、選股、背離三個問題。

## 驗收條件

- [ ] 新增「掃描」分頁
- [ ] X 軸＝期間報酬率(%)，Y 軸＝籌碼異常度 z-score
- [ ] 象限輔助線 X=0 / Y=0，四象限文字標註（右上 量價齊揚／右下 漲但籌碼流出／左上 跌但籌碼流入／左下 量價齊跌）
- [ ] 點大小對應成交值
- [ ] 期間可選 5/20/60 日，與市場頁全域天數獨立，標題明示區間
- [ ] 可切換籌碼來源：外資／投信／三大法人合計
- [ ] 篩選器：最低成交值、排除 `insufficient_history`
- [ ] hover 顯示股號、股名、兩軸數值
- [ ] 點擊開啟該股個股分頁
- [ ] 追蹤清單個股以不同顏色高亮
- [ ] 資料點超過 1500 改用 canvas 繪製，維持拖曳縮放流暢

**相依：** 籌碼異常度指標。若「HTML 抽離」尚未完成，本 issue 會再往那個 118KB 字串裡塞程式碼，建議先做那張。
**Parent:** 多檔個股橫向比較 epic
""",
    },
    {
        "key": 14,
        "title": "[Feature] 掃描結果表格檢視（可排序 + 迷你走勢圖）",
        "gh_labels": ["web", "scan", "priority:medium", "enhancement"],
        "gl_labels": ["frontend", "scan", "priority::2", "type::feature"],
        "body": """## 驗收條件

- [ ] 與散布圖共用同一份結果集與篩選條件，切換不重新查詢
- [ ] 欄位：股號、股名、收盤、期間漲跌幅、外資淨額、投信淨額、融資增減、z-score、佔量比
- [ ] 數值欄可點擊排序，預設依 z-score 絕對值排序
- [ ] 每列附 30 日股價 sparkline
- [ ] 散布圖框選後表格只顯示框選內個股
- [ ] 匯出 CSV
- [ ] 超過 200 列採虛擬捲動

**相依：** 全市場掃描散布圖
**Parent:** 多檔個股橫向比較 epic
""",
    },
    {
        "key": 15,
        "title": "[Feature] 追蹤清單與每日籌碼轉弱提示",
        "gh_labels": ["web", "scan", "priority:medium", "enhancement"],
        "gl_labels": ["frontend", "scan", "priority::2", "type::feature"],
        "body": """## 背景

「監控持股」與「全市場發掘」是兩種不同的使用節奏，混在同一畫面會兩邊都難用。

## 驗收條件

- [ ] 可新增／移除追蹤個股，沿用回測頁既有 localStorage 機制
- [ ] JSON 匯出／匯入，與回測規則機制一致
- [ ] 顯示每檔近期籌碼 z-score 與漲跌幅
- [ ] |z-score| 超過門檻（預設 2.0，可調）置頂並標示
- [ ] 標示方向用不同顏色與符號，不只靠紅綠
- [ ] 從散布圖與表格可一鍵加入
- [ ] 空狀態提供「從掃描結果加入」入口

**相依：** 籌碼異常度指標
**Parent:** 多檔個股橫向比較 epic
""",
    },
    {
        "key": 16,
        "title": "[UX] 掃描結果跳轉個股分頁後可返回，且保留掃描狀態",
        "gh_labels": ["web", "ux", "enhancement"],
        "gl_labels": ["frontend", "ux", "priority::3", "type::improvement"],
        "body": """## 背景

現行外資排行點長條會開啟個股分頁，但條件不會保留。看完一檔就得重設，會摧毀掃描流程的可用性。

## 驗收條件

- [ ] 掃描條件序列化到 URL query string
- [ ] 個股分頁提供「返回掃描結果」，回到原條件與捲動位置
- [ ] 瀏覽器上一頁行為正確，不會退出整個應用
- [ ] 已檢視個股標記為已讀

**相依：** 散布圖、表格檢視
**Parent:** 多檔個股橫向比較 epic
""",
    },
    {
        "key": 17,
        "title": "[UX] 統一時間區間模型，消除全域天數與區塊自訂區間的混淆",
        "gh_labels": ["web", "ux", "enhancement"],
        "gl_labels": ["frontend", "ux", "priority::3", "type::improvement"],
        "body": """## 背景

市場頁有「全域天數」，外資排行卻用自己的區間，並以文字說明「與全域天數無關」。需要用文字解釋的介面通常代表模型本身有問題。掃描頁再引入第三個區間會惡化。

## 驗收條件

- [ ] 每個獨立區間的區塊在標題列顯示生效區間（例：`近 20 日｜2026-08-07 ～ 2026-09-05`）
- [ ] 覆寫全域設定的區塊加上視覺標記
- [ ] 移除現有兩處純文字免責說明，改由介面本身表達
- [ ] 掃描頁沿用同一套區間元件，不新增第四種時間控制項

**Parent:** 多檔個股橫向比較 epic
""",
    },
    {
        "key": 18,
        "title": "[Feature] 多檔股價疊圖比較（正規化為百分比變化）",
        "gh_labels": ["web", "scan", "enhancement"],
        "gl_labels": ["frontend", "scan", "priority::3", "type::feature"],
        "body": """## 驗收條件

- [ ] 支援同時疊加最多 8 檔
- [ ] 價格正規化為區間起點 = 100 的相對變化（絕對股價疊圖無法比較）
- [ ] 起始基準日可拖曳調整並重新正規化
- [ ] 可切換第二面板顯示同期籌碼累計淨額，共用 X 軸
- [ ] 圖例可點擊隱藏／顯示
- [ ] 從追蹤清單與掃描表格可勾選帶入

**相依：** 掃描結果表格
**Parent:** 多檔個股橫向比較 epic
""",
    },
    {
        "key": 19,
        "title": "[Feature] 分點籌碼指標：主力買賣超與集中度",
        "gh_labels": ["data", "scan", "enhancement"],
        "gl_labels": ["data", "scan", "priority::4", "type::feature"],
        "body": """## 背景

`market/broker_branch.py` 與 `docs/broker_branch.md` 已實作分點資料收集，`/api/broker_branch/*` 端點已存在，但未用於橫向比較。這是訊號密度最高、卻完全未開發的資產。資料量級高於其他來源，不應阻擋主線 MVP。

相關已關：#53 #54 #55 #56 #57 #61。

## 驗收條件

- [ ] 每日計算前 15 大買超／賣超分點淨額合計
- [ ] 計算集中度（買超前 15 淨額 ÷ 總成交量）
- [ ] 每日批次預先聚合，查詢時不掃原始明細
- [ ] 集中度可作為散布圖 Y 軸的可選來源
- [ ] 個股分頁新增分點進出區塊

## 備註

現行僅覆蓋熱門股（`BROKER_BRANCH_HOT_N=80`），非全市場。全市場掃描要納入分點的話需先評估 FinMind 額度與抓取成本 — 這可能決定此功能是「全市場指標」還是「熱門股專屬」。先確認再排期。

**相依：** stock_chips_daily、散布圖
**Parent:** 多檔個股橫向比較 epic
""",
    },
    {
        "key": 20,
        "title": "[Feature] 由掃描條件建立告警規則",
        "gh_labels": ["web", "alerts", "enhancement"],
        "gl_labels": ["frontend", "alerts", "priority::4", "type::feature"],
        "body": """## 背景

`alertsdb/store.py` 與 `notify/screener.py` 已有完整的告警儲存與推播管線（含 Slack、performance 追蹤、`DASHBOARD_HORIZONS`），但儀表板的告警分頁沒有建立入口。掃描條件本身就是一組完整篩選邏輯，直接轉為每日告警是最自然的路徑，不需另做告警編輯器。

## 驗收條件

- [ ] 掃描結果頁提供「設為每日告警」
- [ ] 沿用既有 `run_screener` workflow，收盤後批次執行已儲存條件
- [ ] 告警清單顯示觸發日期、股號、觸發時的指標數值
- [ ] 可從告警項目跳回當時的掃描條件
- [ ] 補上告警分頁空狀態，指向掃描頁作為建立入口

**相依：** 全市場掃描散布圖
**Parent:** 多檔個股橫向比較 epic
""",
    },
]


def run(cmd: list[str]) -> str:
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return (out.stdout or "").strip()


def ensure_gh_labels() -> None:
    wanted = {
        "backend": ("5319e7", "Backend / API / server"),
        "infra": ("0052cc", "Infra / Cloud Run / ops"),
        "security": ("b60205", "Security"),
        "scan": ("1d76db", "Market scanner"),
        "data": ("0e8a16", "Data model / ingest"),
        "ux": ("e99695", "UX"),
        "alerts": ("fbca04", "Alerts"),
        "test": ("bfd4f2", "Tests"),
        "chore": ("cccccc", "Chore"),
        "priority:low": ("c5def5", "Lower product priority"),
    }
    existing = {
        row["name"]
        for row in json.loads(run(["gh", "label", "list", "--limit", "100", "--json", "name"]))
    }
    for name, (color, desc) in wanted.items():
        if name in existing:
            continue
        subprocess.run(
            ["gh", "label", "create", name, "--color", color, "--description", desc],
            check=True,
        )


def create_github() -> None:
    ensure_gh_labels()
    created: dict[int, str] = {}
    for item in ISSUES:
        cmd = ["gh", "issue", "create", "--title", item["title"], "--body", item["body"]]
        for lab in item["gh_labels"]:
            cmd.extend(["--label", lab])
        url = run(cmd)
        created[item["key"]] = url
        print(f"#{item['key']} -> {url}")
    print("\nCreated", len(created), "issues.")


def create_gitlab() -> None:
    created: dict[int, str] = {}
    for item in ISSUES:
        cmd = ["glab", "issue", "create", "-t", item["title"], "-d", item["body"], "--no-editor", "-y"]
        for lab in item["gl_labels"]:
            cmd.extend(["-l", lab])
        url = run(cmd)
        created[item["key"]] = url
        print(f"#{item['key']} -> {url}")
    print("\nCreated", len(created), "issues.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gitlab", action="store_true", help="Use glab instead of gh")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        for item in ISSUES:
            labs = item["gl_labels"] if args.gitlab else item["gh_labels"]
            print(f"{item['key']}. {item['title']}")
            print("   labels:", ", ".join(labs))
        return 0
    if args.gitlab:
        create_gitlab()
    else:
        create_github()
    return 0


if __name__ == "__main__":
    sys.exit(main())
