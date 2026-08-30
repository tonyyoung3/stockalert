# stockalert

台股型態篩選、固定持有期績效、PTT 週報，以及指數／外資收集。

## 篩選器與 harness

每天用 yfinance 掃 `taiwan_stocks.txt`，抓上影線反轉 / Inside Day，寫進 SQLite，再視需要打 Slack。報酬用 T+5 / T+20 / T+60 交易日衡量。

```bash
pip install -r requirements.txt
cp .env.example .env
python screener.py
python performance_checker.py
python ptt_stock.py
python interactive_bot.py   # 需要長期主機
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

## 台股資料收集器

正式環境用 GitHub Actions 排程，每個台股交易日 **18:00 台灣時間** 跑 `update_market_data.py`，補最近 14 天缺的資料：

- 大盤日 K / 小時 K / 開盤 5 秒
- 外資買賣超、融資融券、全市場個股日 K
- 期交所三大法人未平倉
- 美股 SPY / QQQ / IWM

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
python update_market_data.py              # 預設近 14 天
python update_market_data.py --days 730   # 歷史回補
python update_market_data.py --dry-run
python cloud_db.py status
python cloud_db.py push --days 14
python cloud_db.py push-alerts
```

本機或 VPS 也用同一支腳本（台灣 18:00）：

```cron
0 18 * * 1-5  cd /path/to/stockalert && python3 update_market_data.py --days 14
```

`collector.py run` 是長駐每小時抓即時指數，GitHub Actions runner 結束就死，不適合作正式排程。研究用的小時 K 在收盤後由 5 秒資料彙整，比盤中快照完整。證交所有流量限制，勿把間隔調低。

手動單次：

```bash
python collector.py index
python collector.py foreign
python backfill.py 90
python taifex_collector.py recent 30
python us_collector.py hourly
```
