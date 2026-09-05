# Chip z-score — 掃描用可配置窗格（#78 / epic #76）

**狀態：query-time 契約，可 merge。** 不實作掃描散布圖 UI（#79）。DE merge（本票不自動合）。

在 `stock_chips_daily`（#77 VIEW）上算三大法人買賣超的 **z-score**，給多檔掃描排序，之後當散布圖軸（#79）。**不是**新表、也不是 VIEW：每次查詢用 SQL 取窗格列，Python 算樣本標準差。

儀表板既有讀徑（`/api/summary`、`/api/top`、`/api/stock`…）繼續打底表，**不要**改走 VIEW 或本 API。

## PM merge gates

| # | Gate | 本契約 |
| --- | --- | --- |
| 1 | Configurable window | 預設 **20 個交易日**；`window` 可調 2–252。`min_periods` 預設 = `window` |
| 2 | Z-scores for nets (+ buy/sell) | `foreign_net` / `trust_net` / `dealer_net`，以及同窗的 buy/sell |
| 3 | Multi-ticker query/API | `GET /api/scanner/chip_zscore?tickers=2330,2454&window=20&asof=YYYY-MM-DD` |
| 4 | Insufficient-sample flag | 列上 `insufficient_sample`：窗格列數 `< window` 或 `< min_periods` |
| 5 | Tests cover the math | `tests/test_chip_zscore.py`（`zscore` 公式 + VIEW 整合） |
| 6 | Documented fields / nulls | 本文件 |
| 7 | CI green; dashboard paths intact | 既有 `/api/*` 不改 SQL；`dashboard.py` 不寫 `stock_chips_daily` |

---

## 公式

對每一檔、每一欄 \(x\)（例如 `foreign_net`），取 `trade_date <= asof`、依日期排序的**最後 `window` 列**（含 asof 當日；partition = `stock_id`）：

\[
z = \frac{x_{\mathrm{asof}} - \overline{x}}{s},\quad
s = \sqrt{\frac{1}{n-1}\sum_i (x_i - \overline{x})^2}
\]

- **樣本標準差**（`ddof=1`），與 `statistics.stdev` / pandas `std()` 預設相同。
- \(n\) 是窗格內**該欄非 NULL** 的個數。AVG/STD 都跳過 NULL。
- `nullif(s, 0)`：常數序列 \(s=0\) 時 z 為 `null`（避免除零）。
- 當日 \(x_{\mathrm{asof}}\) 為 NULL（上櫃無 T86、或缺法人列）時 z 為 `null`。

等價 SQL 形狀（SQLite 沒有內建 `stddev`，API 用 Python 算 \(s\)）：

```sql
-- 概念：per stock_id, last `window` rows, then
(x - avg(x) over w) / nullif(stddev_samp(x) over w, 0)
```

---

## 窗格與樣本不足

| 參數 | 預設 | 範圍 | 意義 |
| --- | --- | --- | --- |
| `window` | **20** 個交易日 | 2–252 | 回看列數（`stock_chips_daily` 列 = 有日 K 的交易日） |
| `min_periods` | = `window` | 2–`window` | 算 z 所需的**非 NULL** 個數下限 |
| `asof` | 查詢標的的 `MAX(trade_date)` | `YYYY-MM-DD` | 窗格右端；無效字串當沒給 |
| `ddof` | 1 | （固定） | 樣本標準差 |

`insufficient_sample`（**一檔一旗標**，不是每欄一個）：

- `true` 當該檔 `trade_date <= asof` 的列數（最多取 `window` 列）**小於 `window` 或小於 `min_periods`**。
- 新上市、資料缺口、或 `asof` 之前沒有日 K → `true`。
- 上市滿窗、但法人欄很多 NULL（上櫃、T86 缺日）：旗標仍看**列數**；該欄 `*_z` 為 `null`，`*_n` 是非 NULL 個數。

`*_z` 另為 `null` 當：當日值 NULL、非 NULL 個數 `< min_periods`、或 stddev = 0。

---

## API

`GET /api/scanner/chip_zscore`

與其他 `/api/*` 一樣走儀表板 Basic Auth（若有設）。

| Query | 必填 | 說明 |
| --- | --- | --- |
| `tickers` | 是 | 逗號分隔，最多 200 檔（`2330,2454`）。也可重複 key。非法代號略過 |
| `window` | 否 | 預設 20 |
| `min_periods` | 否 | 預設 = `window` |
| `asof` | 否 | `YYYY-MM-DD`；預設為這些代號在 VIEW 裡的最新日 |

沒有有效 `tickers` 時：`error=missing_tickers`，`data=[]`（HTTP 仍 200，與多數儀表板 API 相同）。

### 回應

```json
{
  "window": 20,
  "min_periods": 20,
  "asof": "2026-09-04",
  "ddof": 1,
  "fields": ["foreign_net", "trust_net", "dealer_net",
             "foreign_buy", "foreign_sell", "trust_buy", "trust_sell",
             "dealer_buy", "dealer_sell"],
  "tickers": ["2330", "2454"],
  "data": [
    {
      "stock_id": "2330",
      "stock_name": "台積電",
      "trade_date": "2026-09-04",
      "asof": "2026-09-04",
      "close": 1460.0,
      "volume": 11000000,
      "turnover": 16000000000,
      "sample_count": 20,
      "insufficient_sample": false,
      "foreign_net": 150,
      "foreign_net_n": 20,
      "foreign_net_z": 1.264911,
      "trust_net": 10,
      "trust_net_n": 20,
      "trust_net_z": 0.45,
      "dealer_net": 2,
      "dealer_net_n": 20,
      "dealer_net_z": -0.8
    }
  ]
}
```

| 欄位 | 說明 |
| --- | --- |
| `trade_date` | 該檔實際用來當 \(x_{\mathrm{asof}}\) 的日（`<= asof` 的最後一列） |
| `asof` | 請求／解析後的窗格右端 |
| `close` / `volume` / `turnover` | 當日價量（單位與 VIEW 相同：元／股／元），給之後散布圖；本票不畫圖 |
| `sample_count` | 窗格列數（≤ `window`） |
| `insufficient_sample` | 見上 |
| `{field}` | 當日原始值（股；無 T86 則 `null`） |
| `{field}_n` | 窗格內該欄非 NULL 個數 |
| `{field}_z` | z-score，四捨五入到 6 位；見 NULL 規則 |

`data` 順序跟請求 `tickers` 相同。查無列的代號仍回一列（`insufficient_sample=true`，z 皆 `null`）。

---

## 模組

| | |
| --- | --- |
| 計算 / 查詢 | `web/chip_zscore.py`（`zscore`、`query_chip_zscore`） |
| 路由 | `GET /api/scanner/chip_zscore` → `chip_zscore.api_chip_zscore` |
| 資料 | 只讀 `stock_chips_daily`。不寫入、不刷新 VIEW |
| Turso | 無需新 DDL；VIEW 已由 #77 `ensure_schema` 同步 |

---

## 不做

- 掃描散布圖／表格 UI（#79 / #80）
- 改儀表板既有 SQL 或 `KEY_TABLES`
- 物化 z-score 表
- 上櫃三大法人（VIEW 裡本來就是 NULL；z 亦為 NULL）
