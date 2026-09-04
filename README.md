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

PTT 股板週報跟 Reddit 投資想法週報都是本機整理、印到 stdout（可加 `--json`）。`python -m notify.daily_digest` 抓近 1 天重點（PTT 題材／標的／盤後閒聊，Reddit DD）打到同一個 Slack channel。GitHub Actions 每天 **22:00 台灣時間** 跑（排程只在 `main`；沒 merge 不會送）。Reddit 預設看 r/SecurityAnalysis、r/ValueInvesting、r/investing、r/stocks、r/wallstreetbets；WSB 只收 DD / 研究類 flair。雲端 IP 常被 Reddit 擋（403），會改走 [Arctic Shift](https://arctic-shift.photon-reddit.com) 封存。分點買賣超資料源記在 `TODO.md`，還沒做。

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

先在本機確認 Turso 已有資料（`python -m data.cloud_db status`），再：

```bash
gcloud run deploy stockalert \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars TURSO_DATABASE_URL="$TURSO_DATABASE_URL",TURSO_AUTH_TOKEN="$TURSO_AUTH_TOKEN"
```

會給一個 `*.run.app` 網址。設了那兩個 Turso 變數就讀雲端，不帶本機 `twse_data.db`。回測會把需要的表快照進暫存 sqlite，pandas 不用改。

### 資料新鮮度與 `/health`

儀表板載入後會顯示 `foreign_daily`、`stock_daily`、`taifex`（`taifex_fut_oi`）、`alerts` 的最後日期與距今天數。最新日若早於「上一個台股交易日」，header 下方會有醒目警告（不是灰色 hint）；圖表空陣列則顯示「請跑 `python -m market.update_market_data`」，不會留空白 canvas。

交易日假設是**平日、未內建國定假日**；平日 **16:00 台灣時間**之後才把當日視為應有資料（與 `market.update_market_data` 的收盤後判斷同一截止）。

| 路徑 | 內容 |
| --- | --- |
| `GET /api/freshness` | 各表 `{table, last_date, days_ago, stale, empty}`，以及整體 `stale` / `empty` |
| `GET /api/summary` | 原有 KPI，另含同一個 `freshness` 物件 |
| `GET /health` | JSON：`status`/`ok` 表示**行程活著**（HTTP **一律 200**）。資料過期是 payload 的 `freshness.stale`／`empty`，**不會因此回 503**，方便之後 Cloud Run 探活依欄位延伸。 |

### 告警與績效區塊

儀表板「今日／近期告警」與「績效摘要」讀 `alerts`、`performance`（T+5／T+20／T+60 結算列）：

| 環境 | 資料在哪 |
| --- | --- |
| 本機 | `screener.db`（`python -m notify.screener`、`python -m notify.performance_checker` 寫入） |
| Cloud Run | 同一個 Turso DB（排程 `python -m data.cloud_db push-alerts` 把兩張表推上去；不必再開第二個 Turso） |

名稱來自市場資料的 `stocks` 表（本機 `twse_data.db` 或 Turso）。題材（theme）目前沒存在表裡，畫面上是空的，也不會在每次開頁去打 yfinance。沒有列時顯示「尚無告警／尚未結算」。近期告警預設近 30 日（可選 7／90，上限 365）。績效是全樣本，並依 `pattern_type` 拆一列，不是分頁。

本機模擬 PaaS：

```bash
PORT=8080 DASHBOARD_NO_BROWSER=1 python -m web.dashboard
```

