# 熱門股分點主力 — 掃描衍生指標（#98 / epic #76）

**狀態：query-time 契約 + 掃描工作台面板（#101）。** 只讀路徑 A 已入庫的 `broker_branch_daily`（熱門前 N）。**零 FinMind 增量**。標題鎖定「**熱門股分點動向**」，禁止寫「全市場」。掃描工作台另塊面板讀本 API，與 chip_zscore 散布圖分開；同一套 `tickers`+`asof`。

來源：DE 評估 #85（零額度衍生）。資料契約見 `docs/broker_branch.md`。

## PM merge gates

| # | Gate | 本契約 |
| --- | --- | --- |
| 1 | API + docs | `GET /api/scanner/broker_main_force`；本文件 |
| 2 | Titles must NOT say 全市場 | payload `title` = 「熱門股分點動向」；`coverage: hot_n`（空表 `empty`） |
| 3 | Tests | `tests/test_broker_main_force.py`（公式 + 多檔 + 非熱門空列） |
| 4 | Usable by scanner | 與 `chip_zscore` 同形：`tickers` 多檔、`asof`、列上數值欄、請求順序 |
| 5 | CI green — DE merge | 不改既有 `/api/broker_branch/*`、不打 FinMind、不改掃描 HTML |

---

## 公式

在 **單一 `trade_date`（= asof）**、該檔已 ingest 的分點列上算。宇宙 = 當日 `broker_branch_daily` 有列的股票（熱門前 N），**不是**全市場。

對一檔當日各分點淨額 \(n_i =\) `net_volume`（股；買 − 賣）：

1. **主力買超集中度** `buy_concentration`  
   買超分點：\(n_i > 0\)。Top K（`n` 由大到小）之和／全部買超之和。
2. **主力賣超集中度** `sell_concentration`（對稱）  
   賣超分點：\(n_i < 0\)。Top K（`|n|` 由大到小）之和／全部賣超 `|n|` 之和。
3. **龍頭分點淨額** `lead_branch_net`  
   當日 `|n|` 最大分點的 **signed** `net_volume`。平手：較大 signed net，再比 `broker_id`。

`k` 預設 **5**，範圍 1–50。買超（或賣超）分點少於 K 就用全部；該側沒有分點 → 該側集中度 `null`。`n_i = 0` 的分點不進買／賣側。

---

## API

`GET /api/scanner/broker_main_force`

與其他 `/api/*` 一樣走儀表板 Basic Auth（若有設）。**不**打 FinMind。

| Query | 必填 | 說明 |
| --- | --- | --- |
| `tickers` | 是 | 逗號分隔，最多 200 檔（`2330,2454`）。也可重複 key／`ticker`。非法代號略過 |
| `asof` | 否 | `YYYY-MM-DD`；預設為 `broker_branch_daily` 的 `MAX(trade_date)`。無效字串當沒給 |
| `k` | 否 | Top K，預設 5 |

沒有有效 `tickers` 時：`error=missing_tickers`，`data=[]`（HTTP 仍 200）。

該 `asof` 當日沒有該檔列（不在熱門 N ingest）→ 仍回一列，`in_hot_n=false`，指標皆 `null`。不要 fallback 到別日、不要假裝全市場。

### 回應

```json
{
  "kind": "broker_main_force",
  "not": "t86_foreign",
  "title": "熱門股分點動向",
  "coverage": "hot_n",
  "coverage_note": "加總範圍是已入庫的熱門前 N 檔，不是全市場。",
  "path": "A",
  "slice_decision": "hot_n",
  "k": 5,
  "asof": "2026-09-03",
  "hot_n": 80,
  "universe_count": 3,
  "fields": ["buy_concentration", "sell_concentration", "lead_branch_net"],
  "tickers": ["2330", "2454"],
  "data": [
    {
      "stock_id": "2330",
      "stock_name": "台積電",
      "trade_date": "2026-09-03",
      "asof": "2026-09-03",
      "in_hot_n": true,
      "branch_count": 3,
      "buy_side_sum": 410000,
      "sell_side_sum": 280000,
      "buy_top_k_sum": 410000,
      "sell_top_k_sum": 280000,
      "buy_concentration": 1.0,
      "sell_concentration": 1.0,
      "lead_broker_id": "1020",
      "lead_broker_name": "合庫",
      "lead_branch_net": 400000
    }
  ]
}
```

| 欄位 | 說明 |
| --- | --- |
| `title` | 固定「熱門股分點動向」。路徑 A **永不**用「全市場」 |
| `coverage` | 表空 → `empty`；有列 → `hot_n`（永不 `full_market`） |
| `universe_count` | 該 `asof` 當日表內 distinct `stock_id` |
| `in_hot_n` | 該檔當日有分點列 |
| `buy_concentration` / `sell_concentration` | 0–1，四捨五入到 6 位；該側無量則 `null` |
| `lead_branch_net` | 龍頭分點 signed 淨額（股） |
| `lead_broker_id` / `lead_broker_name` | 龍頭分點 |

`data` 順序跟請求 `tickers` 相同。

掃描工作台「熱門股分點動向」面板讀這三個 `fields`（與 `chip_zscore` 同一套 `tickers`+`asof`），不是籌碼散布圖軸。見 `web/static/index.html`。

---

## 模組

| | |
| --- | --- |
| 計算 / 查詢 | `web/broker_main_force.py` |
| 路由 | `GET /api/scanner/broker_main_force` → `broker_main_force.api_broker_main_force` |
| 資料 | 只讀 `broker_branch_daily` / `brokers` / `stocks`。不寫入、不 ingest |
| FinMind | **零呼叫**。底料仍是既有 21:00 熱門 N job |

---

## 不做

- 加大 `BROKER_BRANCH_HOT_N`、分點長歷史回補
- SponsorPro 全市場 parquet／標題寫全市場
- BSR／OpenAPI 熱門進出假裝分點
- 把主力欄位混進 chip_zscore 散布圖軸（#101 用獨立面板）
- 券商群組歸母公司（#85 可選，本票不做）
