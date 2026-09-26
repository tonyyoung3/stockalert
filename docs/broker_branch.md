# 熱門股分點動向：既有資料唯讀查詢

**分點自動更新已停用。** 不再提供排程或 CLI 正式匯入，也不需要供應商 token。
網站只讀 sqlite／Turso 中既有的 `broker_branch_daily`、`brokers`、`broker_branch_meta`。
資料庫既有列保留；空表回空列表，不會在請求時補抓。熱門股分點**不是全市場**。

`data_mode` 依儲存的來源標記判斷：`live` 表示歷史正式資料，並不表示仍有更新；
`dev_fixture` 表示測試資料，`empty` 表示沒有資料。無來源標記的列保守視為測試資料。
`BROKER_BRANCH_HOT_N` 僅供既有切片資訊顯示（預設 80），不會觸發下載。

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
| `ingest_configured` | 固定 `false`，正式匯入已停用 |
| `writes` / `reads` | 固定 `"disabled"` / `"db"` |
| `data_mode` | `empty` / `live` / `dev_fixture`（空表一律 `empty`） |
| `blocker` | 自動更新已停用的說明 |
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

`automatic_updates: false`。21:00 僅保留作為歷史資料過期判定的相容欄位，不代表有更新排程。
獨立於 `/api/freshness`（16:00／T86）。

---

## 掃描衍生指標

`GET /api/scanner/broker_main_force` 只讀既有分點列，計算買／賣超集中度與龍頭分點淨額。公式見 `docs/broker_main_force.md`。

## 本機測試資料

`python -m market.broker_branch status` 回報唯讀狀態。
`python -m market.broker_branch load-fixture --dev` 只供 TEST/DEV，必須明確加 `--dev`；
示範列不可當成正式行情推到 Turso。fixture 檔在 `tests/fixtures/broker_branch_sample.json`。
