# stockalert

台股型態篩選、固定持有期績效、PTT / Reddit 週報，以及指數／外資收集。

對外兩張臉是 **Slack 通知** 跟 **本地網站**。共用層是行情、規則、績效；SQLite 檔仍在 repo 根目錄。

```
notify/     Slack：篩選、績效、互動 bot、失敗通知
web/        本地儀表板與指數回測
market/     證交所／期交所／美股收集與每日補資料
data/       價格解析、Turso 同步、repo 路徑
signals/    型態規則（上影線 / Inside Day）
alertsdb/   screener.db
ptt/        股板週報
reddit/     Reddit 投資想法週報
harness/    唯讀查詢迴圈
```

啟動用 `python -m <套件>.<模組>`。

## 篩選器與 harness

每天用 yfinance 掃 `taiwan_stocks.txt`，抓上影線反轉 / Inside Day，寫進 SQLite，再視需要打 Slack。報酬用 T+5 / T+20 / T+60 交易日衡量。

```bash
pip install -r requirements.txt
cp .env.example .env
python -m notify.screener
python -m notify.performance_checker
python -m ptt.ptt_stock
python -m reddit.ideas             # Reddit 投資想法週報
python -m notify.daily_digest      # PTT + Reddit 今日重點打 Slack（--dry-run 只印）
python -m notify.interactive_bot   # 需要長期主機
python -m web.dashboard            # 本地 http://localhost:8765；Cloud Run 見下方
```

`harness/` 包在 model 外面：工具、迴圈、權限、trace。Model 只負責想；harness 負責查資料並停在唯讀範圍。

```bash
python -m harness --list-tools
python -m harness --tool summarize_performance
python -m harness --tool lookup_alert_history --arg ticker=2330
python -m harness "最近訊號的績效如何？"
```

Slack bot 預設只認 ticker / help。設 `HARNESS_ENABLED=1` 且有 API key，沒寫代碼的問句才會進 harness。

```bash
python -m unittest discover -s tests -v
```

PTT 股板週報跟 Reddit 投資想法週報都是本機整理、印到 stdout（可加 `--json`）。`python -m notify.daily_digest` 抓近 1 天重點（PTT 題材／標的／盤後閒聊，Reddit DD）打到同一個 Slack channel。GitHub Actions 每天 **22:00 台灣時間** 跑（排程只在 `main`；沒 merge 不會送）。Reddit 預設看 r/SecurityAnalysis、r/ValueInvesting、r/investing、r/stocks、r/wallstreetbets；WSB 只收 DD / 研究類 flair。雲端 IP 常被 Reddit 擋（403），會改走 [Arctic Shift](https://arctic-shift.photon-reddit.com) 封存。分點買賣超契約在 `docs/broker_branch.md`（#54）。v1 是**熱門股**分點動向（成交額前 N 驅動市場 Top，同表可讀個股），**不是**全市場、也**不是** T86 外資。空表已建；**無 FinMind token、尚未 live ingest**（無 token 不 merge live）。#55 可用 stub API 或本機 `python -m market.broker_branch load-fixture --dev`（TEST/DEV，不是 production）。

## 台股資料收集器

正式環境用 GitHub Actions 排程，每個台股交易日 **18:00 台灣時間（Asia/Taipei）** 跑 `python -m market.update_market_data`，補最近 14 天缺的資料：

- 大盤日 K / 小時 K / 開盤 5 秒
- 外資買賣超、融資融券、全市場個股日 K（**上市**證交所 MI_INDEX + **上櫃**櫃買收盤行情，寫進同一張 `stock_daily`）
- 期交所三大法人未平倉
- 美股 SPY / QQQ / IWM

這就是每日台股價格下載，不是另開一條 Yahoo 全市場 pipeline。篩選器（21:00 台灣時間）仍用 yfinance + `taiwan_stocks.txt`；Yahoo Close=NaN 時回填同一組官方上市/上櫃收盤價（PR #22 / #23）。上櫃抓空或失敗會讓這輪 job 變紅，既有 Slack 失敗通知會帶 log。

資料先寫進 `twse_data.db`、`us_data.db`，用 Actions cache 接下一次、artifact 留 90 天，**不要 commit**。設了 `TURSO_DATABASE_URL` 跟 `TURSO_AUTH_TOKEN` 時，同一輪會把最近 N 天的列推到 Turso（一個 DB 裡同時放台股表跟美股表）。篩選排程另外把 `screener.db` 的 alerts / performance 全量推上去（表很小，且 performance 有 FK）。沒設 secrets 就只留本機檔，排程不會失敗。增量欄位依序認 `trade_date` / `alert_date` / `check_date`。

排程失敗時，若 repo 已有 `SLACK_BOT_TOKEN` 跟 `SLACK_CHANNEL`，會把最後約 80 行 log 跟完整 `market-update.log` 打到同一個頻道，並附 Actions 連結。沒設 Slack secrets 就只紅在 Actions。

第一次或 cache 被清掉時，到 Actions → *Update market data* → Run workflow，把 `days` 設成 `730` 回補約兩年。

開 Turso：

```bash
turso auth login
turso db create stockalert
turso db show stockalert --url
turso db tokens create stockalert
```

把 URL 跟 token 加到 GitHub repo secrets（`TURSO_DATABASE_URL`、`TURSO_AUTH_TOKEN`），以及本機 `.env`。

```bash
python -m market.update_market_data              # 預設近 14 天
python -m market.update_market_data --days 730   # 歷史回補
python -m market.update_market_data --dry-run
python -m data.cloud_db status
python -m data.cloud_db push --days 14
python -m data.cloud_db push-alerts
```

本機或 VPS 也用同一支腳本（台灣 18:00）：

```cron
0 18 * * 1-5  cd /path/to/stockalert && python3 -m market.update_market_data --days 14
```

`python -m market.collector run` 是長駐每小時抓即時指數，GitHub Actions runner 結束就死，不適合作正式排程。研究用的小時 K 在收盤後由 5 秒資料彙整，比盤中快照完整。證交所有流量限制，勿把間隔調低。

手動單次：

```bash
python -m market.collector index
python -m market.collector foreign
python -m market.backfill 90
python -m market.taifex_collector recent 30
python -m market.us_collector hourly
```

## 公開網站（Cloud Run）

儀表板可以丟上 Cloud Run：閒置不計費，有人打開才跑。區域選 `us-central1`（或 `us-east1` / `us-west1`）才算免費額度。

### 存取控制（必設再開公網）

這是個人研究工具。`DASHBOARD_USER` 與 `DASHBOARD_PASSWORD` **兩個都有非空值**時，啟用 HTTP Basic Auth：

- 受保護：HTML（`/`、`/index.html`）與所有 `/api/*`（含 `POST /api/backtest`）
- 永遠開放：`GET /health`（Cloud Run 探活；HTTP 200 + freshness JSON，與今天相同）
- 未帶帳密或密碼錯誤：HTTP **401**，`WWW-Authenticate: Basic realm="stockalert"`，內容只有通用 `{"error":"unauthorized"}`（不回內部路徑、DB、stack）
- 瀏覽器對同網域的 `/api/*` 與回測 `fetch` 會自動帶 Basic（前端用 `credentials: 'same-origin'`，**不要把密碼寫進 JS**）
- Auth 開啟時，`POST /api/backtest` 另有簡易記憶體限流（每 IP 每分鐘約 10 次）

**任一變數未設：不做驗證。** 只適合本機；**不要在沒設這兩個變數的情況下公開部署**。Cloud Run IAM 維持 `--allow-unauthenticated` 沒問題——那只表示任何人打得到服務，擋人的是應用層 Basic。IAM 全開 + 應用層 Basic 是個人部署的預期組合；沒設 Basic 環境變數時，網站是全開的。

本機（無驗證，僅本機）：

```bash
python -m web.dashboard
```

本機也要登入時：

```bash
DASHBOARD_USER=me DASHBOARD_PASSWORD=change-me python -m web.dashboard
```

先在本機確認 Turso 已有資料（`python -m data.cloud_db status`），再建 Secret（或直接用 env，較不建議）：

```bash
printf '%s' "$DASHBOARD_USER" | gcloud secrets create DASHBOARD_USER --data-file=-
printf '%s' "$DASHBOARD_PASSWORD" | gcloud secrets create DASHBOARD_PASSWORD --data-file=-
```

已存在的 secret 用 `gcloud secrets versions add … --data-file=-`。Cloud Run 預設運算服務帳號要能讀 secret（`roles/secretmanager.secretAccessor`）。

```bash
gcloud run deploy stockalert \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars TURSO_DATABASE_URL="$TURSO_DATABASE_URL",TURSO_AUTH_TOKEN="$TURSO_AUTH_TOKEN" \
  --set-secrets DASHBOARD_USER=DASHBOARD_USER:latest,DASHBOARD_PASSWORD=DASHBOARD_PASSWORD:latest
```

或把帳密一併寫進環境變數（會出現在服務設定裡，只建議暫時用）：

```bash
gcloud run deploy stockalert \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars TURSO_DATABASE_URL="$TURSO_DATABASE_URL",TURSO_AUTH_TOKEN="$TURSO_AUTH_TOKEN",DASHBOARD_USER="$DASHBOARD_USER",DASHBOARD_PASSWORD="$DASHBOARD_PASSWORD"
```

會給一個 `*.run.app` 網址。設了那兩個 Turso 變數就讀雲端，不帶本機 `twse_data.db`。回測會把需要的表快照進暫存 sqlite，pandas 不用改。既有服務在 merge 後也要把 `DASHBOARD_USER` / `DASHBOARD_PASSWORD` 設成 secret 或 env，否則站點仍全開。

### 資料新鮮度與 `/health`

儀表板載入後會顯示 `foreign_daily`、`stock_daily`、`taifex`（`taifex_fut_oi`）、`alerts` 的最後日期與距今天數。最新日若早於「上一個台股交易日」，header 下方會有醒目警告（不是灰色 hint）；圖表空陣列則顯示「請跑 `python -m market.update_market_data`」，不會留空白 canvas。

交易日是**平日且非證交所休市**（週末、國定假日、春節前結算日都不計過期）。靜態表在 `web/tw_calendar.py` 的 `is_tw_trading_day`，覆蓋 **2025–2026**；來源是證交所[市場開休市日期](https://www.twse.com.tw/zh/trading/holiday.html)／[holidaySchedule JSON](https://www.twse.com.tw/holidaySchedule/holidaySchedule?response=json)（2026 為 115 年表；2025 為 114 年修正版，含年中新增的教師節／光復節／行憲紀念日）。**2027 尚未公告**，那些年份先當平日，等證交所公布再補表。不爬即時公告，也不含颱風等臨時休市。平日 **16:00 台灣時間**之後才把當日視為應有資料（與 `market.update_market_data.include_today` 同一截止；收集器回補也走同一個 helper）。

| 路徑 | 內容 |
| --- | --- |
| `GET /api/freshness` | 各表 `{table, last_date, days_ago, stale, empty}`，以及整體 `stale` / `empty` |
| `GET /api/summary` | 原有 KPI，另含同一個 `freshness` 物件 |
| `GET /health` | JSON：`status`/`ok` 表示**行程活著**（HTTP **一律 200**）。資料過期是 payload 的 `freshness.stale`／`empty`，**不會因此回 503**，方便之後 Cloud Run 探活依欄位延伸。 |

### 儀表板分區

頂部分頁：`市場`（預設）｜`個股`｜`告警`｜`回測`。用顯示／隱藏切換，不必捲完整頁。新鮮度橫幅與各表最後日期留在分頁上方。

網址 hash：`#market` / `#stock` / `#alerts` / `#backtest`（也接受 `#section-stock` 這種寫法），可與既有 `?stock=2330` 並用。沒有 hash、但有 `?stock=` 時開個股分頁；純首次進入落在市場總覽，回測表單與結果不佔首屏。

窄螢幕（約 375px）控制列改直向堆疊、input 滿寬、表格在區內橫滑；回測的資料集／濾網／進場／出場用 `data-bt-fold` 折疊（手機預設收合），濾網積木是列表、參數為第二層。圖表手勢文案是拖曳／雙指縮放（pinch 原本就開著）。外資排行日期欄在小螢幕改直向、高度至少 44px，避免 iOS 裁切。

Header **顯示範圍**（全域 `days`）只影響加權 K 線／走勢、外資合計、融資融券、台指期未平倉、個股圖。外資買賣超排行用自己的當日／近 N 日／自訂區間，不受全域天數控制。

市場 tab 另有獨立卡 **「熱門股分點動向」**（不是「全市場」、也不是 T86 外資排行）：買超／賣超分點 Top 讀 `GET /api/broker_branch/top`，新鮮度讀 `GET /api/broker_branch/freshness`（21:00 截止，不併入 `/api/freshness`）。無 token／空表顯示「尚未接上 FinMind token」，不是空白圖。手機兩塊列表直向堆疊、各自捲動。本機可用 `python -m market.broker_branch load-fixture --dev` 看示範列，**不要**把 fixture 當 production。

個股 tab 在外資圖旁有獨立卡 **「券商分點買賣超」**：選股後（含 `?stock=` / `selectStock`）讀 `GET /api/broker_branch/stock?id=`（可帶 `date`）。未選股先引導選股；無 token 用與市場卡相同的「尚未接上 FinMind token」文案；該檔不在熱門前 N 則明示空狀態，不是空白圖。同樣標 FinMind 分點約 21:00、不是 T86，**禁止**寫「全市場」。

### 告警與績效區塊

儀表板「今日／近期告警」與「績效摘要」讀 `alerts`、`performance`（T+5／T+20／T+60 結算列）：

| 環境 | 資料在哪 |
| --- | --- |
| 本機 | `screener.db`（`python -m notify.screener`、`python -m notify.performance_checker` 寫入） |
| Cloud Run | 同一個 Turso DB（排程 `python -m data.cloud_db push-alerts` 把兩張表推上去；不必再開第二個 Turso） |

名稱來自市場資料的 `stocks` 表（本機 `twse_data.db` 或 Turso）。題材（theme）目前沒存在表裡，畫面上是空的，也不會在每次開頁去打 yfinance。沒有列時顯示「尚無告警／尚未結算」。近期告警預設近 30 日（可選 7／90，上限 365）。績效是全樣本，並依 `pattern_type` 拆一列，不是分頁。

### 回測規則積木（v1）

回測頁用積木組「若…（濾網 AND）→ 則進場／出場」，能力對齊現有 `web/backtest_engine.py`，**不是**新 DSL。

`POST /api/backtest` 仍走既有 **HTTP Basic Auth** 與（auth 開啟時）每 IP 每分鐘約 10 次限流。Body 可為：

1. **v1 積木 JSON**（儀表板現在送這個；伺服器 `blocks_to_rule` 編成扁平 rule）
2. **舊扁平 rule**（`filters` 為物件）— 相容，行為與以前相同

積木文件形狀（詳見 `web/strategy_blocks.py`）：

```json
{
  "version": 1,
  "dataset": "2y_hourly",
  "mode": "intraday",
  "filters": [{"type": "weekdays", "params": {"days": [0, 1, 2, 3]}}],
  "entry": {"direction": "long", "reference": "first_hour_high", "offset_pct": 0,
            "trigger": "touch_from_below", "earliest_hour": 10},
  "exit": {"exit_hour": 13, "stop_enabled": false},
  "cost_pct": 0.03
}
```

| 濾網 `type` | 引擎欄位 | 收盤才確定？ |
| --- | --- | --- |
| `weekdays` | `weekdays` | 否 |
| `trend` | `trend`（含 `*_today`） | 僅 `*_today` |
| `prev_day` | `prev_day` | 否 |
| `gap` | `gap_dir`, `gap_abs_min_pct` | 否 |
| `day_return` | `day_ret_dir`, `day_ret_min_pct` | 是 |
| `ma_cross` | `ma_cross` | 是 |
| `breakout` | `breakout`, `breakout_window` | 是 |
| `oi_ratio` | `oi_ratio_mode`, `oi_ratio_pctile`, `oi_ratio_window` | 否 |

結構：`dataset` ∈ {`2y_hourly`, `15y_daily`}；`mode` ∈ {`intraday`, `overnight`, `swing`}。日內 `entry`/`exit` 對應參考價＋偏移、觸發、方向、最早時間、出場時刻、停損。隔夜／波段 `exit` 對應 `hold_to`／停損%／停利%／最大持有天數（引擎既有優先序：停損＞停利＞時間／持有）。

v1 **不做**：OR／巢狀群組、個股回測、告警 K 線進場、移動停損、任意腳本。日內模式若帶收盤才確定的濾網，編譯與引擎都會拒絕（偷看未來）。

**本機預設**（回測頁「本機預設」）：具名規則存在瀏覽器 `localStorage` 鍵 `stockalert.bt.presets.v1`（最多 20 筆）。可載入／覆蓋／刪除；也可匯出或貼上 v1 積木 JSON。壞掉的 JSON 會顯示繁中錯誤，不會白屏。沒有雲端同步或多帳號分享。載入後 `blocks_to_rule` 編譯結果與儲存時相同。

本機模擬 PaaS：

```bash
PORT=8080 DASHBOARD_NO_BROWSER=1 python -m web.dashboard
```

