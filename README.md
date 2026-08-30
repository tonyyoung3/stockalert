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

每小時抓台灣加權指數 (TAIEX)、每天抓證交所 T86 個股外資買賣超，存入 SQLite (`twse_data.db`)。

```bash
python collector.py index
python collector.py foreign
python collector.py run
```

`run` 模式：交易日 09:00–14:00 每小時 :05 抓指數，每天 17:30 抓外資買賣超。證交所有流量限制，勿高頻請求。
