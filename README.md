# stockalert

台股 K 線型態篩選器：每個交易日抓上市股票日線，找出上影線反轉與連續反轉（程式裡叫 Inside Day），寫進 SQLite，並把 K 線圖送到 Slack。滿 28 天後再回看報酬。

## 元件

| 檔案 | 用途 |
| --- | --- |
| `screener.py` | 掃描 `taiwan_stocks.txt`、去重、畫圖、發 Slack |
| `performance_checker.py` | 檢查滿 28 天且尚未評估的 alert |
| `interactive_bot.py` | Slack Socket Mode：輸入代碼回傳近 20 日 K 線 |
| `db.py` | SQLite（`alerts` / `performance`） |
| `prices.py` | yfinance 下載與各種欄位格式的收盤價解析 |
| `taiwan_stocks.txt` | 上市代碼（`.TW`），一行一檔 |

## 本機執行

Python 3.11+。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入 Slack token
```

```bash
python screener.py
python performance_checker.py
python interactive_bot.py
```

測試：

```bash
python -m unittest discover -s tests -v
```

資料庫預設是專案目錄下的 `screener.db`。可用環境變數 `SCREENER_DB` 改路徑。第一次本機跑若沒有這個檔，會自動建空表；若要接上目前的歷史 alert，把 `screener.seed.db` 複製成 `screener.db`。

## 環境變數

見 `.env.example`。

- `SLACK_BOT_TOKEN` / `SLACK_CHANNEL`：篩選結果通知（沒設就只印在 terminal）
- `SLACK_APP_TOKEN`：互動機器人（Socket Mode）另外需要
- `SHADOW_RATIO`、`UPPER_SHADOW_MIN_PCT`、`MIN_DAILY_GAIN`：型態門檻，可選

互動機器人請在**長期開著的機器**上跑，不要當 GitHub Actions 常駐服務。Actions runner 一結束，Socket Mode 連線就斷。`run_interactive_bot.yml` 只適合手動短測。

## GitHub Actions

`run_screener.yml` 每天 21:00 台灣時間跑（UTC 13:00），也可手動 `workflow_dispatch`。

alert 資料庫**不再 commit 回 git**。流程是：

1. 還原上次 run 的 cache；沒有再試前一次的 artifact
2. 都沒有就用 repo 裡的 `screener.seed.db` 當起點
3. 跑完把 `screener.db` 存進 cache，並上傳 90 天 artifact（名稱 `screener-db`）

需要的 secrets：`SLACK_BOT_TOKEN`、`SLACK_CHANNEL`。互動機器人另外要 `SLACK_APP_TOKEN`。

## 型態（簡述）

- **上影線反轉**：前一日長上影線，次日收復高點、漲幅與量能過門檻，且收在 20 日均線上。
- **Inside Day**（名稱沿用舊稱）：連續兩次上影線反轉，且當日收盤是三日最高、在月線上。不是經典 inside bar（高低點包在前一根裡面）。

同一檔、同一型態、同一根 K 線日期只會 alert 一次。
