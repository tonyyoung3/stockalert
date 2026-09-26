# 0050 監控名單

`python -m market.etf_members --refresh` 從元大官方 PCF/Daily 的
`FundWeights.StockWeights` 取得完整股票持股，排除現金、期貨與 ETF。
來源頁：https://www.yuantaetfs.com/product/detail/0050/ratio

必須有 50 個不重複股票代碼、有效權重與資料日期；只取得網頁前五大持股時拒收。
名單版本由資料日期及排序後的代碼產生，方便回溯每次通知使用哪個名單。

預設快取 `.cache/0050-members.json`，以原子替換更新。每個台灣日首次讀取同步一次，
`--refresh` 可強制重抓。更新失敗可使用未超過 3 個交易日的已驗證快取，並記錄 warning；
沒有有效快取、來源未更新太久或資料日期在未來時停止，不退回寫死名單。
資料日期取自官方 `PCF.trandate`，重新下載不會延長資料有效期。

`market.backfill_0050` 也使用這份名單。這是目前持股名單，不能當成歷史回測各日的成分股名單。
遇到成分股數目因公司事件暫時異動，先確認官方清單後再調整驗證規則；預設拒收部分名單。
