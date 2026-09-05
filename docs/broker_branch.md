# 分點買賣超 — 資料契約（#54 / epic #53）

**狀態：契約已對齊（2026-09-05）。切片鎖定路徑 A。Live ingest 未實作。無 token 不可當已上線。**

儀表板現有「外資買賣超」來自證交所 **T86（三大法人）**，**不是**券商分點。
分點買賣超 = 各券商分店買進股數 − 賣出股數。

程式裡只有空表、讀取契約、以及標成 TEST/DEV 的 fixture。**沒有 live FinMind client。**

---

## 已鎖定：路徑 A（預設）

**熱門前 N 驅動市場 tab Top**；同一套 `broker_branch_daily` / `brokers` 也提供個股讀取（#57 v1.1，不擋大盤）。

| | |
| --- | --- |
| 市場 Top | 該日、已 ingest 的熱門 N 檔內，依分點 `SUM(net_volume)` 買超／賣超 Top K |
| 個股 | 同表 `stock_id` + `trade_date` → 分點排行（有列才顯示；沒有該檔就空） |
| 下鑽 | `broker_id` + `trade_date` → 貢獻標的（同樣只看已入庫的熱門 N） |
| 標題 | **「熱門股分點動向」**。README／UI **禁止**寫「全市場」 |
| Ingest（有 token 之後才寫） | 週一至週五 ~21:00 後，用 `stock_daily` 最新日成交額取前 N，只拉這 N 檔 SecIdAgg |

**無 token → 不打 FinMind、不 merge live 功能。** 空狀態或「請接 token」。Fixture 不可當 production。

---

## 路徑 B（備案，不要當預設實作）

**僅當主人之後改選 B：** 單檔 on-demand（查詢時才拉該 `stock_id`），市場 tab **不排行**（入口／跳個股）。

本契約與程式預設都是路徑 A。不要把 on-demand 做成預設 ingest，也不要為 B 先寫 live client。

---

## 阻塞（blocker）— 只剩 token

| 項目 | 現況 |
| --- | --- |
| 切片 | **已鎖定路徑 A**（熱門前 N → 市場 Top；同表個股讀取） |
| `FINMIND_TOKEN` | GitHub secrets、本機 `.env` **皆無**。`.env.example` 只有空佔位。 |
| Live ingest | **未寫。** 無 token 時 skip／明確錯誤。**禁止**無 token 就 merge live FinMind。 |

Merge 真功能到 `main` 的條件：

- 有 token 且有實作的 live FinMind 路徑（路徑 A：熱門前 N 日更），**或**
- 誠實的空狀態／「請接 token」狀態。

**禁止**把 fixture 資料當成正式行情 merge 進 production。

---

## 資料源

| 來源 | 用途 | 何時用 |
| --- | --- | --- |
| FinMind `TaiwanStockTradingDailyReportSecIdAgg` | 個股區間、各分點 buy/sell 量（日彙總） | **路徑 A 有 token 之後的主路徑**（對熱門 N 檔逐檔拉） |
| FinMind SponsorPro 整日 parquet | 全市場一次下載（約 22 MB） | **非 v1。** 只有真全市場日更才考慮；v1 標題仍不可寫「全市場」 |
| `TaiwanSecuritiesTraderInfo` | 分點代號 → 名稱 | 填 `brokers` |
| 證交所 BSR / 櫃買券商日報 | 有驗證碼、當日、逐檔 | **不做** |
| TWSE / TPEX OpenAPI 熱門進出 | 沒有全部分點明細 | **禁止當成分點排行** |

官方欄位（FinMind Chip 文件，Sponsor）：

- Endpoint：`https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report_secid_agg`
- 參數：`data_id`（stock_id）、可選 `securities_trader_id`、`start_date`、`end_date`
- Token：`Authorization: Bearer …`（不要把 token 放進 query）
- 列：`date`, `stock_id`, `securities_trader_id`, `securities_trader`, `buy_volume`, `sell_volume`, `buy_price`, `sell_price`
- 區間約 2021-06-30 起；文件寫盤後約 **21:00（Asia/Taipei）** 更新
- **不存** `buy_price` / `sell_price`（價位／均價明細，非目標）

`net_volume = buy_volume - sell_volume`（股）。

若日後發現該 dataset 對此帳號不可用：退回 `TaiwanStockTradingDailyReport`（價位列再 GROUP BY，更貴）或 SponsorPro parquet。先向 FinMind 確認權限，再寫 ingest。

---

## 熱門前 N（路徑 A 切片）

> **UX 紅線：** 標題必須是「**熱門股分點動向**」（或同等「熱門／成交額前 N」）。
> **禁止**寫「全市場」「全市場分點」「大盤全部分點」。
> OpenAPI 熱門進出排行也**禁止**拿來假裝分點。

| 項目 | 契約 |
| --- | --- |
| N | 可設定，預設 **80**（建議 50–100）。環境變數 `BROKER_BRANCH_HOT_N`。 |
| 選股 | 取 `stock_daily` **最新一個 `trade_date`**，依 `turnover`（成交金額，元）降序取前 N。上市+上櫃同一張表。 |
| 為何是「最新 stock_daily 日」 | 市場 catch-up 約 **18:00** 台灣時間寫入當日 `stock_daily`。分點約 **21:00** 才有。同一曆日：18:00 後已有當日成交額，21:00 再只拉這 N 檔。 |
| 若 18:00 尚未寫入當日 | 用**上一交易日**成交額當切片（文件／UI 須標「切片日」）。不要 silently 假裝是當日全市場。 |
| 刷新 | 週一至週五、FinMind 約 21:00 之後。每次重算前 N，只 ingest 這 N 檔當日（或指定日）SecIdAgg。 |
| 市場排行定義 | 該 `trade_date`、**已 ingest 的熱門 N 檔內**，依 `SUM(net_volume)` 依分點加總，再取買超 Top K／賣超 Top K。 |
| 個股讀取 | 同一張表。熱門 N 以外的股票當日通常沒有列（空狀態，不是錯誤）。 |
| 不是什麼 | **不是**全市場加總。N 以外的股票當日沒進表（或只剩舊列）。掉出前 N 的股票當天不會更新。 |
| 覆蓋率 | payload 帶 `coverage: "hot_n"`、`hot_n`、`slice_trade_date`、`universe_count`。 |

SponsorPro 真全市場是後續 milestone，**不是路徑 A**。未做之前標題一律「熱門股」。

---

## 降級條件

| 條件 | 行為 |
| --- | --- |
| 無 `FINMIND_TOKEN` | 不打 FinMind。API 回空列表 + `token_present: false` + blocker。UI：「尚未接上 FinMind token」。標題仍是「熱門股分點動向」。 |
| 有 token（路徑 A，預設） | 熱門前 N 日更 + 市場 Top + 同表個股／下鑽。標題「熱門股分點動向」。 |
| 主人之後改選路徑 B | 才改成個股 on-demand；市場 tab 不排行。**現在不要實作。** |
| FinMind 21:00 尚未出當日 | `freshness.stale`／空；預期日用 **21:00** 切，不是 T86 的 16:00。 |
| API／配額失敗 | 沿用市場資料 job 的 Slack 失敗通知（若已接 Slack secrets）。不因分點缺資料讓 `/health` 變 503。 |
| 無 token 又要做分點 | **不做** BSR 驗證碼全爬、**不做** OpenAPI 假分點。 |

---

## Schema（空表，與其他行情表同一處建立）

寫在 `market/collector.py` `init_db`。既有 `twse_data.db` 下次 `get_conn()` 會 `CREATE IF NOT EXISTS`。
**不存價位明細。** 沒有 live 列，直到有 token 後才寫 ingest。

```sql
CREATE TABLE IF NOT EXISTS broker_branch_daily (
    trade_date  TEXT,
    stock_id    TEXT,
    broker_id   TEXT,
    buy_volume  INTEGER,
    sell_volume INTEGER,
    net_volume  INTEGER,  -- buy_volume - sell_volume（股）
    PRIMARY KEY (trade_date, stock_id, broker_id)
);
CREATE TABLE IF NOT EXISTS brokers (
    broker_id   TEXT PRIMARY KEY,
    broker_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_broker_branch_date_net
    ON broker_branch_daily(trade_date, net_volume);
CREATE INDEX IF NOT EXISTS idx_broker_branch_stock_date
    ON broker_branch_daily(stock_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_broker_branch_broker_date
    ON broker_branch_daily(broker_id, trade_date);
```

對齊 FinMind：`broker_id` = `securities_trader_id`，`broker_name` = `securities_trader`。

Turso：表有 `trade_date`，之後 ingest 可用既有 `cloud_db.push_market_files` 增量推。
本 PR **不**推 fixture、不為分點開新的排程 push。空表可能在下次 `update_market_data` 的 `init_db` + push 出現在遠端，0 列，沒問題。

---

## 排程契約（尚未啟用）

| | |
| --- | --- |
| 何時 | 週一至週五，FinMind 文件約 **21:00 Asia/Taipei**（13:00 UTC）。 |
| 與 18:00 市場 job 的關係 | `stock_daily` 先到，熱門前 N 才選得出來。分點 job 必須在 21:00 後，不可掛在 18:00 那輪裡打 FinMind。 |
| 失敗 | 與 `update_market_data.yml` 相同：`python -m notify.notify_job` + Slack secrets。 |
| Freshness | 分點**自己的**欄位／API（21:00 切）。**不要**併進現有 `/api/freshness` 四張關鍵表。 |
| 本 PR | 只在 workflow 裡**註解**說明。沒有會打 FinMind 的 cron（無 token）。 |

---

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
| `token_present` | 環境有沒有非空 `FINMIND_TOKEN` |
| `live_ingest` | 本 PR 永遠 `false` |
| `data_mode` | `empty_awaiting_token` 或 `dev_fixture`（僅本機／測試） |
| `blocker` | 無 token 時的說明 |
| `freshness` | `last_date`, `expected_trade_date`（21:00）, `stale`, `empty` |

### 市場：某日熱門股分點買／賣超 Top K（路徑 A）

`GET /api/broker_branch/top?date=YYYY-MM-DD&k=15`

- 未給 `date`：用表內最大 `trade_date`（空則 `null`）。
- **定義：** 該日、已入庫股票上，`GROUP BY broker_id` 加總 `net_volume`。買超 = DESC；賣超 = ASC。
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

### 新鮮度

`GET /api/broker_branch/freshness`

預期日：週一至週五 **21:00** 台灣時間之後才把當日算「應有資料」。
獨立於 `/api/freshness`（16:00／T86）。

---

## Fixture（#55 可平行做 UI；不是 production ingest）

- 檔案：`tests/fixtures/broker_branch_sample.json`
- 載入：`python -m market.broker_branch load-fixture --dev`（沒有 `--dev` 會拒絕）
- 標示：`data_mode: "dev_fixture"`，`live_ingest: false`
- **不要** commit 進 `twse_data.db`、**不要**當正式行情 push Turso、**不要**在 UI 寫成「已接上 FinMind」

#55 現在可以：

1. 對空 DB 打 stub API → 空列表 + 「請接 token」；標題「熱門股分點動向」。
2. 本機 `--dev` 載入 fixture → 同一支 API 有假資料，排版／空狀態可先做。
3. Merge 到 `main` 仍須 live 路徑（且有 token）或誠實空狀態，不能靠 fixture 假裝可用。

---

## 明確不做

- BSR／櫃買驗證碼全爬
- 用 OpenAPI 熱門排行假裝分點
- 存價位／tick 明細
- 無 token 卻開打 FinMind 的排程或 merge live ingest
- 熱門前 N 卻標題／README 寫「全市場」
- 把路徑 B（on-demand only）做成預設
