# 待辦

## 券商分點當日買賣超

儀表板外資買賣超來自證交所 T86（三大法人），**不是**券商分點。分點買賣超 = 各券商分店買進股數 − 賣出股數。證交所沒有「全市場各分點」現成 JSON。

### 資料源

| 來源 | 能拿到什麼 | 限制 |
| --- | --- | --- |
| 證交所 BSR [bsr.twse.com.tw](https://bsr.twse.com.tw/bshtm/) | 上市個股各分點買/賣 | 驗證碼、多半只有當日、要逐檔 |
| 櫃買 [券商買賣日報](https://www.tpex.org.tw/zh-tw/mainboard/trading/info/brokerBS.html) | 上櫃同上 | 一樣有驗證碼、當日、逐檔 |
| FinMind `TaiwanStockTradingDailyReport` | 個股或券商、價位明細 | Sponsor 付費；單日約 300 萬筆 |
| FinMind `TaiwanStockTradingDailyReportSecIdAgg` | 個股區間、各分點買/賣量 | Sponsor；買賣超 = buy − sell |
| FinMind SponsorPro 整日 parquet | 全市場一次下載（約 22 MB） | 更貴；盤後約 21:00 更新 |
| TWSE / TPEX OpenAPI | 熱門股進出排行、券商營業金額 | **沒有**全部分點明細 |

輔助：`TaiwanSecuritiesTraderInfo`、證交所券商名冊 Excel（分點代號對名稱）。

資料區間約 2021-06-30 起（FinMind）。官方 BSR 下午 3–4 點才出當日。

### 建議做法

1. 有 FinMind token：接 `TaiwanStockTradingDailyReportSecIdAgg`（指定個股分點排行），或 SponsorPro 整日 parquet 再 `GROUP BY` 分點算買賣超。
2. 沒有 token：不要先爬 BSR（驗證碼 + 2000 檔）。除非只做少數指定個股。
3. 存庫用 `(trade_date, stock_id, broker_id)` 日彙總，不要存價位明細。

### 不做（除非上面走不通）

- 自己解 BSR / 櫃買驗證碼全市場爬蟲
- 用 OpenAPI 熱門排行假裝是全市場分點
