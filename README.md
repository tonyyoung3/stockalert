# 台股資料收集器

每小時抓台灣加權指數 (TAIEX)、每天抓證交所 T86 個股外資買賣超,存入 SQLite (`twse_data.db`)。

## 安裝

```bash
pip install requests schedule
```

## 使用

```bash
python collector.py index             # 手動抓一次大盤指數
python collector.py foreign           # 抓最近交易日外資買賣超
python collector.py foreign 20260731  # 抓指定日期
python collector.py run               # 常駐執行(內建排程)
```

`run` 模式:交易日 09:00–14:00 每小時 :05 抓指數(14 點那次會抓到收盤值),每天 17:30 抓外資買賣超(T86 約 16:00 後公布)。

## 用 cron 排程(替代 run 模式)

```cron
5 9-14 * * 1-5  cd /path/to/twse_collector && python3 collector.py index
30 17 * * 1-5   cd /path/to/twse_collector && python3 collector.py foreign
```

## 資料表

- `taiex_hourly`: 抓取時間、交易日、指數、漲跌、成交金額(億)
- `foreign_daily`: 交易日、代號、名稱、外資買進/賣出/買賣超股數(外陸資,不含外資自營商)

查詢範例:

```sql
-- 某股外資近 20 日買賣超
SELECT trade_date, foreign_net FROM foreign_daily
WHERE stock_id='2330' ORDER BY trade_date DESC LIMIT 20;

-- 當日外資買超前 10 名
SELECT * FROM foreign_daily WHERE trade_date=(SELECT MAX(trade_date) FROM foreign_daily)
ORDER BY foreign_net DESC LIMIT 10;
```

## 資料來源

- 指數:`mis.twse.com.tw/stock/api/getStockInfo.jsp` (tse_t00 即時行情)
- 外資:`www.twse.com.tw/rwd/zh/fund/T86` (三大法人買賣超日報)

注意:證交所有流量限制,勿高頻請求;非交易日 T86 會自動往前找最近交易日。
