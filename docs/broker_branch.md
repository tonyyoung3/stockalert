# 熱門股分點動向：路徑 A 排程 ingest + 網站只讀

**Actions 寫、服務讀。** FinMind 只由 GitHub Actions（`.github/workflows/update_broker_branch.yml`，平日約 **21:00** 台灣時間）或 CLI `python -m market.broker_branch ingest` 寫入 sqlite／Turso。Dashboard／API **請求時不打 FinMind**。空表回空列表 + freshness，不會在請求時補抓。熱門股分點**不是全市場**。

`ingest_configured: true` 只表示**這個行程**有 `FINMIND_TOKEN`，可以跑排程 ingest，**不是**網站 live fetch。Cloud Run 讀庫通常沒有 token。

資料集是 FinMind **`TaiwanStockTradingDailyReport`**（Sponsor）。每次請求 `data_id`（股票）+ `date`（`YYYY-MM-DD`）。`securities_trader_id` 是「依券商查」的替代參數，跟股票一起查時**不要傳**。回傳是價位列（`price` / `buy` / `sell`）；入庫前依分點 `SUM(buy)`、`SUM(sell)`，**不存價格**。`net_volume = buy - sell`（股）。

單檔 HTTP 400（或其它 FinMind 錯誤）：**skip + log**（status／detail，不含 token），繼續其餘熱門 N。401／403 中止整次（token／Sponsor 權限）。整次 0 列，或失敗檔數超過一半，job 失敗（Slack）。不要把 fixture 當 production。

## API / SQL 契約（給 #55 / #56 / #57）

Base：與現有儀表板相同的 sqlite／Turso 讀取。
**這不是 T86。** 不要跟 `/api/top` 共用一張表或同一段文案。

共用欄位：

| 欄位 | 含義 |
| --- | --- |
| `kind` | 固定 `"broker_branch"` |
| `not` | 固定 `"t86_foreign"` |
| `title` | 市場／空狀態 → **「熱門股分點動向」**。路徑 A **永不**用「全市場」。 |
| `coverage` | 路徑 A：`empty` / `hot_n`；個股讀取另用 `single_stock` |
| `path` | 固定 `"A"` |
| `slice_decision` | 固定 `"hot_n"` |
| `slice_trade_date` | 選熱門 N 所用的 `stock_daily` 日（可能比 ingest 日舊） |
| `dataset` | `TaiwanStockTradingDailyReport` |
| `ingest_configured` | 這個行程有 `FINMIND_TOKEN` 才是 `true`。網站通常是 `false` |
| `writes` / `reads` | `"actions_or_cli"` / `"db"` |
| `data_mode` | `empty` / `live` / `dev_fixture`（空表一律 `empty`） |
| `blocker` | 沒有 token 時說明排程 ingest 需要 secret；有 token 則 `null` |
| `freshness` | `last_date`, `expected_trade_date`（21:00）, `stale`, `empty` |

### 市場：某日熱門股分點買／賣超 Top K（路徑 A）

`GET /api/broker_branch/top?date=YYYY-MM-DD&k=15&days=1`

- 未給 `date`：用表內最大 `trade_date`（空則 `null`）。
- 可選 `days`：與外資排行相同，取該截止日往前 N 個有列的交易日加總。未給或 `1` = 當日。
- **定義：** 該區間、已入庫股票上，`GROUP BY broker_id` 加總 `net_volume`。買超 = DESC；賣超 = ASC。
- 空表：空列表 + `coverage: "empty"` + `title: "熱門股分點動向"`。**不要**假造全市場。

```sql
SELECT b.broker_id, COALESCE(MAX(br.broker_name), b.broker_id),
       SUM(b.net_volume) AS net
FROM broker_branch_daily b
LEFT JOIN brokers br ON br.broker_id = b.broker_id
WHERE b.trade_date = ?
GROUP BY b.broker_id
ORDER BY net DESC   -- 賣超改 ASC
LIMIT ?;
```

### 下鑽（#56）：某分點當日貢獻標的

市場 tab「熱門股分點動向」點買超／賣超分點列，讀此端點顯示該分點在**已入庫熱門前 N** 的當日貢獻標的（買／賣／淨）。近 N 日累計排行可看，下鑽列表顯示「此切片未支援」（本 API 只有單日，不另做區間加總、也不假裝全市場）。空列與錯誤要明示，不可假造標的。

`GET /api/broker_branch/broker?broker_id=&date=`

```sql
SELECT b.stock_id, COALESCE(s.stock_name, b.stock_id),
       b.buy_volume, b.sell_volume, b.net_volume
FROM broker_branch_daily b
LEFT JOIN stocks s ON s.stock_id = b.stock_id
WHERE b.broker_id = ? AND b.trade_date = ?
ORDER BY b.net_volume DESC;
```

只看已 ingest 的熱門 N，不是該分點全市場帳本。

### 個股（#57，v1.1，同表讀取，不擋大盤）

`GET /api/broker_branch/stock?stock_id=&date=`

```sql
SELECT b.broker_id, COALESCE(br.broker_name, b.broker_id),
       b.buy_volume, b.sell_volume, b.net_volume
FROM broker_branch_daily b
LEFT JOIN brokers br ON br.broker_id = b.broker_id
WHERE b.stock_id = ? AND b.trade_date = ?
ORDER BY b.net_volume DESC;
```

路徑 A 不另開 on-demand fetch；沒有該檔列就空。
個股 tab UI（#57）讀此端點，標題「券商分點買賣超」；未選股／無 token／該檔無列要誠實空狀態，不可假裝行情。不實作 #56 全市場下鑽。

### 新鮮度

`GET /api/broker_branch/freshness`

`automatic_updates: true` 表示平日 Actions 排程存在。21:00 是分點 freshness 截止，獨立於 `/api/freshness`（16:00／T86）。這支 API 仍然只讀資料庫。

---

## 排程 ingest（#113）

```bash
python -m market.broker_branch ingest --date YYYY-MM-DD --n 80 --days 1
```

| 項目 | 契約 |
| --- | --- |
| Endpoint | `GET https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report` |
| 參數 | `data_id` + `date`。不傳 `securities_trader_id`，不把 token 放進 query |
| Token | `Authorization: Bearer`。Actions secret `FINMIND_TOKEN`（Sponsor）。`.env.example` 只有空佔位 |
| 切片 | `stock_daily` 該日（或之前最近日）`ORDER BY turnover DESC LIMIT N`（預設 80，`BROKER_BRANCH_HOT_N`） |
| 彙總 | 同一 `(date, stock_id, securities_trader_id)` 的價位列加總 `buy`／`sell`，寫 `broker_branch_daily`；名稱寫 `brokers` |
| 壞檔 | HTTP ≥400（除 401／403 與可重試的 429／5xx）skip + log，不讓整次 hot-N 停在第一檔 |
| 整次失敗 | 0 列，或失敗 stock-day 超過一半。401／403 立刻停 |
| Turso | 沿用 `cloud_db.push_market_files`。Actions **沒有** `TURSO_DATABASE_URL`／`TURSO_AUTH_TOKEN` 會失敗，避免只寫本機 cache |
| 驗證 | Actions → Update broker-branch → Run workflow。`n`、`days`、`date` 可空白（`date` 空白＝21:00 截止日，`days` 預設 1） |

文件寫 TradingDailyReport 約 18:00 更新。Job 仍排 21:00，等 18:00 的 `stock_daily` 先寫好成交額。

## 掃描衍生指標

`GET /api/scanner/broker_main_force` 只讀既有分點列，計算買／賣超集中度與龍頭分點淨額。公式見 `docs/broker_main_force.md`。

## 本機測試資料

`python -m market.broker_branch status` 回報 token 是否可供排程 ingest（不印 token、不打 API）。
`python -m market.broker_branch load-fixture --dev` 只供 TEST/DEV，必須明確加 `--dev`；
示範列不可當成正式行情推到 Turso。fixture 檔在 `tests/fixtures/broker_branch_sample.json`。
