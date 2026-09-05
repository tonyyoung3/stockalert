# stock_chips_daily — 掃描用日頻籌碼＋價量契約（#77 / epic #76）

**狀態：VIEW 契約，可 merge。** 不實作 z-score（#78）或掃描散布圖（#79）。DE merge（本票不自動合）。

多檔掃描要一張穩定的日頻寬表：價量來自 `stock_daily`，三大法人買賣超來自 `foreign_daily`／`trust_daily`／`dealer_daily`（#64 / `f09da98` 已在 main，與外資同日覆蓋）。本物件是 **SQL VIEW**，不是實體表。

## PM merge gates

| # | Gate | 本契約 |
| --- | --- | --- |
| 1 | Documented columns / PK | 下表欄位；**邏輯 PK = `(trade_date, stock_id)`**（承 `stock_daily` 實體 PK；VIEW 本身沒有 SQLite PRIMARY KEY） |
| 2 | Queryable recent N days × many tickers | 見下方 SQL；測試 `test_recent_n_days_many_tickers` |
| 3 | Clear incremental strategy | VIEW **不刷新**。底表仍由 18:00 `update_market_data` 寫入；Turso 只推 table 列 + VIEW DDL |
| 4 | Do not break existing dashboard read paths | 儀表板／`KEY_TABLES` 繼續打底表，不改走 VIEW |
| 5 | CI green then merge | CI 綠後由 **DE merge** |

---

## 物件

| | |
| --- | --- |
| 名稱 | `stock_chips_daily` |
| 類型 | `VIEW`（`collector.init_db` 建立；`DROP VIEW IF EXISTS` 後重建，定義可演進） |
| 左表 | `stock_daily`（上市 MI_INDEX + 上櫃櫃買，同一張日 K） |
| 右表 | `foreign_daily`、`trust_daily`、`dealer_daily`（證交所 T86；**只有上市**） |
| Join 鍵 / 邏輯 PK | `(trade_date, stock_id)`。三個 LEFT JOIN 都用這對鍵；一檔一日一列 |
| 列來源 | 左表有日 K 才出現。只有法人、沒有日 K 的列**不會**進 VIEW（SQLite 無 FULL OUTER JOIN） |

上櫃列會出現（`stock_daily` 有），法人欄是 `NULL`。T86 本來就不收 OTC，與 README／#64 一致。

既有儀表板讀徑繼續打底表（`foreign_daily`、`stock_daily` 等），**不要**改成走這個 VIEW。新鮮度 `KEY_TABLES` 也不加它。

---

## 欄位

**PK（邏輯）：** `(trade_date, stock_id)`。`trade_date` 為 `YYYY-MM-DD`。一檔一日一列。底表 `stock_daily` / T86 三表的實體 `PRIMARY KEY` 也是這對。

| 欄位 | 來源 | 單位／備註 |
| --- | --- | --- |
| `trade_date` | `stock_daily` | 交易日 |
| `stock_id` | `stock_daily` | 股票代號 |
| `stock_name` | `stock_daily` | 以日 K 名稱為準 |
| `open` / `high` / `low` / `close` | `stock_daily` | 元 |
| `volume` | `stock_daily` | **成交股數**（與今日 `stock_daily` 相同） |
| `turnover` | `stock_daily` | **成交金額（元）**（與今日 `stock_daily` 相同） |
| `foreign_buy` / `foreign_sell` / `foreign_net` | `foreign_daily` | **股**；無 T86 列則 NULL |
| `trust_buy` / `trust_sell` / `trust_net` | `trust_daily` | **股**；無列則 NULL |
| `dealer_buy` / `dealer_sell` / `dealer_net` | `dealer_daily` | **股**；自營商合計含避險（#64） |

法人淨額單位是股，不要換成張。價量單位不要自行換算。

---

## 查詢（近 N 日 × 多檔）

```sql
SELECT *
FROM stock_chips_daily
WHERE trade_date >= date('now', '-14 days')
  AND stock_id IN ('2330', '2317', '2454')
ORDER BY trade_date, stock_id;
```

單檔區間：

```sql
SELECT trade_date, close, foreign_net, trust_net, dealer_net
FROM stock_chips_daily
WHERE stock_id = '2330' AND trade_date >= '2026-01-01'
ORDER BY trade_date;
```

底表已有 PK `(trade_date, stock_id)` 與 `(stock_id, trade_date)` covering index，VIEW 走這些索引，不必再物化。

---

## 增量更新（DE）

**VIEW 不用刷新、也不要 upsert。** 定義永遠讀當下底表。

| 步驟 | 誰做 | 何時 |
| --- | --- | --- |
| 寫入／補 `stock_daily` 與 T86 三表 | `python -m market.update_market_data`（`backfill` 同一套） | 每個台股交易日 **18:00 台灣時間**（Actions `update_market_data.yml`）；本機同一支 |
| 只補投信／自營商缺日 | `--institutional-gaps-only` / `python -m market.backfill institutional` | 歷史 `foreign_daily` 已有、另外兩張空的時候 |
| 推 Turso | 同一輪 `cloud_db.push`（未設 secrets 則只留本機） | catch-up 之後；`ensure_schema` 會 `DROP VIEW` + `CREATE VIEW`，列只推 **table** |

不要在 `update_market_data` 另開 job 重建這張物件。若日後查詢真的慢到要物化，再改成實體表，且只 upsert 新的 `trade_date`（收盤 catch-up 之後），並在本文件改契約。

---

## Turso / libSQL

libSQL 支援 `CREATE VIEW`。本機 `init_db` 建 VIEW 後，`data.cloud_db.ensure_schema` 在 push 時把 VIEW DDL 同步到遠端（先 DROP 再 CREATE，定義更新才會過去）。`push_file` 只 `INSERT` `type='table'`，**不會**把 VIEW 當資料表灌列。

既有遠端庫：這次 merge 後的下一次 `update_market_data`（或手動 `python -m data.cloud_db push`）就會有 VIEW。不必另跑 migration。

---

## 不做

- 籌碼 z-score（#78）— 見 `docs/chip_zscore.md`（query-time API，不改本 VIEW）
- 掃描散布圖／表格 UI（#79 / #80）
- 改儀表板既有 SQL
- 把上櫃三大法人塞進 T86 三表
