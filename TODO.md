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

### 契約（#54）＋ live ingest（#61，2026-09-05，路徑 A）

書面定案與 API／SQL 在 **`docs/broker_branch.md`**。空表在 `collector.init_db`。

- **路徑 A（預設）：** 熱門前 N（`stock_daily` 最新日成交額，`BROKER_BRANCH_HOT_N` 預設 80）驅動市場 Top；同一套表給個股讀取。標題「**熱門股分點動向**」，**不是**全市場。
- **Live ingest：** 有 `FINMIND_TOKEN` 時打 FinMind `TaiwanStockTradingDailyReportSecIdAgg`，寫入 `broker_branch_daily` / `brokers`。週一至週五約 21:00 台灣時間（`update_broker_branch.yml`）。無 token：不打 FinMind，API 維持 empty／connect-token。
- **路徑 B（備案）：** 單檔 on-demand、市場不排行。主人之後才可能改選；**不要當預設實作**。

Fixture 僅 TEST/DEV（`python -m market.broker_branch load-fixture --dev`），不可當 production merge，也不可當 Turso 正式行情。

#57 個股 tab UI 殼：選股後讀 `/api/broker_branch/stock`，未選股／token／該檔無列都要誠實空狀態。
#56 市場卡下鑽：點當日分點列 → `GET /api/broker_branch/broker` 熱門前 N 貢獻標的；近 N 日累計「此切片未支援」。不另開 ingest。

### 不做（除非上面走不通）

- 自己解 BSR / 櫃買驗證碼全市場爬蟲
- 用 OpenAPI 熱門排行假裝是全市場分點
