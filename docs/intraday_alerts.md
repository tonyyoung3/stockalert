# 0050 盤中五分鐘急拉通知

## 規則

只監控 `market.etf_members` 驗證通過的元大 0050 股票持股；不監控 ETF 本身或其他股票。
元大名單日期、版本會與每筆事件一併保存。每日 08:45–09:00（台灣時間）先同步名單；
若 worker 開盤後才啟動，也會在抓行情前同步。

worker 預設每 60 秒透過 yfinance 下載 `period=1d, interval=1m`、未還原分鐘資料，
最多 4 個並行下載。這會向 Yahoo 發出多筆請求，並非單一全市場 API；遇整批錯誤或全數缺少有效行情時退避 5 分鐘。

只使用**已完成**的正常盤 1 分鐘 K：最新完成分鐘的收盤價，對過去 5 分鐘內最低價上漲
**≥ 2%** 觸發。視窗包含最新完成分鐘，共 5 根連續且有成交量的分鐘 K。
例如 10:00–10:05 五根 K 的最低價為 100，10:04 那根 K 在 10:05 完成、收盤 102，即觸發。
最低價的時間只能精確到分鐘；不使用 high-low 振幅，因此先漲後跌不會僅因振幅大而通知。

- 09:00–13:30 成交資料；監控至 13:32，讓最後完成的一分鐘線有時間到達。
- 最新完成分鐘結束至偵測時間不得超過 120 秒。此數值是來源時間差，不是保證延遲。
- 缺分鐘、零成交量、NaN、無效價格、跨日或未完成分鐘不觸發。
- 啟動、資料中斷或重連後先連續觀察 5 分鐘有效視窗；暖機期只計健康狀態，不補發舊訊號。
- 同一波只通知一次；條件必須先降到 2% 以下，且上次事件後已過 15 分鐘，才能重新觸發。
- 在冷卻期間出現的新一波也會被抑制，必須再次解除條件才重新啟用。
- 使用既有台股日曆。未知年度暫停監控；颱風等臨時休市未列入靜態表，但當日無新鮮報價時不通知。

Yahoo / yfinance 不是有延遲保證的逐筆行情。偵測可能漏掉分鐘內上漲後回落，
或因延遲與缺漏而略過；服務會在每輪 JSON log 記錄 `counts`、`max_delay_seconds`、`events`、`sent`。

## 執行與驗收

```bash
# 本機診斷；預設不發 Slack。休市回 off_hours，不下載行情。
python -m notify.intraday_alert --once

# 至少跑一個完整交易日，檢查 missing/stale/incomplete/warming 與行情時間差。
python -m notify.intraday_alert

# 確認資料延遲與覆蓋率可接受後，明確啟用正式 Slack 發送。
# SLACK_BOT_TOKEN / SLACK_CHANNEL 沿用既有設定；可放在未入版控的 .env。
python -m notify.intraday_alert --send
```

`--interval` 可設 30–60 秒，預設 60 秒。`--once` 只跑一輪，啟動暖機中不產生新訊號；若同時指定 `--send`，仍可能重試尚未過期的待送事件。
`--members-cache` 可指定名單檔；`--state` 可指定 SQLite 狀態檔。
預設 dry-run 使用 `.cache/intraday-dry-run.db`，正式使用 `.cache/intraday.db`；
檔案內記錄模式，不允許將 dry-run 狀態直接拿來正式發送，避免測試紀錄壓掉通知。

通知含股票、視窗、最低價／收盤價、漲幅、來源時間與名單日期。
可設定 `DASHBOARD_PUBLIC_URL=https://your-dashboard.example`，加入 `?stock=代號#stock` 連結。

## 常駐部署

使用獨立常駐 worker 與持久磁碟；不要放在網站 HTTP request 或五分鐘 GitHub Actions 排程。
已有 Dockerfile 可以直接重用，`compose.intraday.yml` 不開網站埠，只跑 worker：

```bash
# 預設 dry-run，狀態存入具名 volume。
docker compose -f compose.intraday.yml up -d --build
docker compose -f compose.intraday.yml logs -f intraday
```

完成交易日驗收後，將 compose 的 command 改為：
`["python", "-m", "notify.intraday_alert", "--send"]`，再執行 `up -d`。
主機需保持運作、連網與時間同步，部署只設 1 個副本；Cloud Run 網站本身不會啟動監控。
本機檔案鎖阻止相同狀態路徑的重複程序；不同主機沒有共享鎖，因此不可橫向擴容。

## 儲存與發送可靠性

同一交易日的最後處理分鐘、啟用狀態與冷卻時間持久化，重啟不會清掉去重狀態。
事件與冷卻狀態在同一 SQLite transaction 寫入，之後才由 outbox 發 Slack。
成功回應後記錄 Slack ts；失敗最多 5 次，遵守 Retry-After，事件超過 120 秒即過期，
避免斷線恢復後補發一串舊急拉。名單已移除的股票及跨日事件也不補發。

傳送使用固定 `client_msg_id` 降低重複；若 Slack 已收到訊息、程序卻在記錄成功前中止，
仍有重複的可能，無法承諾端到端 exactly-once。錯誤只記類型與事件 ID，不記 token。
狀態保留 7 日、事件保留 30 日；dry-run 只記事件，不進待送佇列。

先完成 mock 邊界測試與官方／Yahoo 讀取測試，再用 dry-run 的實際交易日記錄驗收。
週末測試不能證明盤中延遲，不能據此直接宣稱已具備即時通知能力。
