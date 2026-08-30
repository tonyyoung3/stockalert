# stockalert

台股型態篩選器：每天用 yfinance 掃 `taiwan_stocks.txt`，抓上影線反轉 / Inside Day，寫進 SQLite，再視需要打 Slack。滿 28 天後可用 `performance_checker.py` 回看報酬。

## 日常指令

```bash
pip install -r requirements.txt
cp .env.example .env

python screener.py
python performance_checker.py
python interactive_bot.py   # 需要長期主機，不要指望 GitHub Actions 一直活著
```

## Agent harness

`harness/` 是包在 model 外面的那一層：工具、迴圈、權限、trace。Model 只負責想；harness 負責查資料並停在唯讀範圍。

可用工具：

- `list_recent_alerts` — 最近訊號
- `lookup_alert_history` — 單一 ticker 的 alerts + 28 天報酬
- `summarize_performance` — 勝率與平均報酬
- `list_pending_checks` — 滿 28 天但還沒寫進 performance 的列
- `check_ticker_pattern` — 抓價並套現有型態判斷

不需要 API key 也可以直接呼叫工具：

```bash
python -m harness --list-tools
python -m harness --tool summarize_performance
python -m harness --tool lookup_alert_history --arg ticker=2330
python -m harness --tool list_recent_alerts --arg days=14
```

有 `OPENAI_API_KEY` 時才走完整的 think → tool → observe 迴圈：

```bash
python -m harness "最近訊號的 28 天績效如何？"
python -m harness --trace "2330 有沒有出過上影線反轉？"
```

Slack bot 預設行為不變（只認 ticker / help）。設 `HARNESS_ENABLED=1` 且有 API key，沒寫代碼的問句才會進 harness。

測試（含 scripted model，不打外網）：

```bash
python -m unittest discover -s tests -v
```
