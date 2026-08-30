# stockalert

台股篩選、績效追蹤，以及後來加上的指數／外資收集與回測工具。

## 篩選器

每個交易日抓上市股票日線，找出上影線反轉與連續反轉（程式裡叫 Inside Day），寫進 SQLite，並把 K 線圖送到 Slack。報酬用固定交易日 T+5 / T+20 / T+60 衡量，不是「滿 28 天看今天收盤」。

| 檔案 | 用途 |
| --- | --- |
| `screener.py` | 掃描 `taiwan_stocks.txt`、去重、畫圖、發 Slack |
| `performance_checker.py` | 檢查已到期的 T+5 / T+20 / T+60 |
| `interactive_bot.py` | Slack Socket Mode：輸入代碼回傳近 20 日 K 線 |
| `ptt_stock.py` | PTT 股板週報（題材／標的／盤中盤後閒聊） |
| `db.py` | SQLite（`alerts` / `performance`） |
| `prices.py` | yfinance 下載、收盤價解析、T+N 出場 |
| `taiwan_stocks.txt` | 上市代碼（`.TW`），一行一檔 |

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入 Slack token
python screener.py
python performance_checker.py
python ptt_stock.py
python -m unittest discover -s tests -v
```

資料庫預設是 `screener.db`，可用 `SCREENER_DB` 改路徑。第一次本機若沒有這個檔，會自動建空表；若要接上歷史 alert，把 `screener.seed.db` 複製成 `screener.db`。

- `SLACK_BOT_TOKEN` / `SLACK_CHANNEL`：篩選結果通知
- `SLACK_APP_TOKEN`：互動機器人（Socket Mode）
- `SHADOW_RATIO`、`UPPER_SHADOW_MIN_PCT`、`MIN_DAILY_GAIN`：型態門檻

`run_screener.yml` 每天 21:00 台灣時間跑。alert 資料庫不再 commit 回 git：從 cache / artifact 還原，沒有就用 `screener.seed.db`，跑完再存 cache 與 90 天 artifact。

- **上影線反轉**：前一日長上影線，次日收復高點、漲幅與量能過門檻，且收在 20 日均線上。
- **Inside Day**（名稱沿用舊稱）：連續兩次上影線反轉，且當日收盤是三日最高、在月線上。不是經典 inside bar。

同一檔、同一型態、同一根 K 線日期只會 alert 一次。

## 台股資料收集器

每小時抓台灣加權指數 (TAIEX)、每天抓證交所 T86 個股外資買賣超，存入 SQLite (`twse_data.db`)。

```bash
pip install requests schedule
python collector.py index             # 手動抓一次大盤指數
python collector.py foreign           # 抓最近交易日外資買賣超
python collector.py foreign 20260731  # 抓指定日期
python collector.py run               # 常駐執行(內建排程)
```

`run` 模式：交易日 09:00–14:00 每小時 :05 抓指數（14 點那次會抓到收盤值），每天 17:30 抓外資買賣超（T86 約 16:00 後公布）。

```cron
5 9-14 * * 1-5  cd /path/to/twse_collector && python3 collector.py index
30 17 * * 1-5   cd /path/to/twse_collector && python3 collector.py foreign
```

- `taiex_hourly`: 抓取時間、交易日、指數、漲跌、成交金額(億)
- `foreign_daily`: 交易日、代號、名稱、外資買進/賣出/買賣超股數(外陸資,不含外資自營商)

資料來源：`mis.twse.com.tw` 指數、`www.twse.com.tw` T86。證交所有流量限制，勿高頻請求。
