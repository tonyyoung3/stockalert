# 掃描條件 → 每日告警（#86 / epic #76）

**狀態：MVP，一組條件。** 复用既有 chip_zscore、`alerts` 列、Slack screener／`notify_job`。不是 DSL，也不是下單。

把掃描工作台的標的清單＋一組欄位上下限存成排程。每日／盤後對清單跑 `query_chip_zscore`，命中寫入儀表板告警列，並（有 secrets 時）打 Slack。失敗寫 `scanner_alert_runs`、Actions log，以及 `notify.notify_job`。

## PM merge gates

| # | Gate | 本契約 |
| --- | --- | --- |
| 1 | 可存一組條件＋清單 | `POST /api/scanner/alert_profile` 或 `python -m notify.scanner_alert save`；repo 預設 `data/scanner_alert_profile.json` |
| 2 | 排程跑完有 Slack **或** 告警列 | 命中 `save_alert`（`pattern_type=scanner_{field}`）；Slack 走 `notify.screener.send_to_slack`。沒命中也發空通知（除非當日已跑過） |
| 3 | 失敗可觀測 | `scanner_alert_runs`（status/error）、stdout、Actions 紅燈、`notify_job` 失敗通知 |
| 4 | 非 DSL／非下單 | 只接受 `tickers` / `window` / `field` / `min` / `max`。`expr`／`sql`／`where` 回 `unsupported_condition` |

## 設定檔形狀

```json
{
  "tickers": ["2330", "2454", "2317"],
  "window": 20,
  "min_periods": 20,
  "field": "foreign_net_z",
  "min": 1.5,
  "max": null,
  "x": "foreign_net_z",
  "y": "close",
  "enabled": true
}
```

`field` 只能是掃描軸已有的欄（籌碼 z / 三大法人淨額 / 價量）。命中：該欄非 NULL、列非 `insufficient_sample`，且 `min ≤ value ≤ max`（缺的那一端不檢查）。沒給 min/max 時預設 `min=1.5`。

## 存在哪、誰讀

| 來源 | 誰寫 | 排程怎麼讀 |
| --- | --- | --- |
| `data/scanner_alert_profile.json` | 改檔 commit，或 `scanner_alert save` | 沒有 DB 列時的預設 |
| `screener.db` 表 `scanner_alert_profile`（一列 `id=1`） | 儀表板「存成每日告警」、CLI save | 本機／Actions cache |
| 同一張表在 Turso | Cloud Run 儀表板 POST | 設了 `TURSO_*` 時優先於 JSON |

載入順序：`--profile`／`$SCANNER_ALERT_PROFILE` → DB（alerts 連線，必要時 Turso／市場連線）→ repo JSON。

## 排程怎麼跑

`python -m notify.scanner_alert` 掛在既有 `.github/workflows/run_screener.yml`（與 K 線篩選同一條 cron：台股 13:30／21:00 備援 + `workflow_dispatch`）。

1. 還原 `screener.db` 與 `twse_data.db` cache（沒有本機市場檔時改打 Turso）
2. `query_chip_zscore`（#78，query-time，不物化）
3. 命中 `save_alert`；重複 `(ticker, pattern_type, asof)` 略過
4. Slack 或略過（沒 secrets）
5. 寫 `scanner_alert_runs`；失敗再跑 `python -m notify.notify_job`
6. 既有 `push-alerts` 把新表一併推上 Turso

本機：

```bash
python -m notify.scanner_alert save --tickers 2330,2454,2317 --field foreign_net_z --min 1.5
python -m notify.scanner_alert --dry-run
python -m notify.scanner_alert
```

`--dry-run` 算命中、寫 run 列，不寫 `alerts`、不打 Slack。

## API

| 路徑 | 說明 |
| --- | --- |
| `GET /api/scanner/alert_profile` | `{profile, source, last_run}`。`source` 為 `db` / `file` / `empty` |
| `POST /api/scanner/alert_profile` | body 同上設定檔。成功 `{saved: true, profile, source: "db", last_run}` |

仍走儀表板 Basic Auth（若有設）。**不**另打 chip_zscore。

## 不做

- 任意 DSL / SQL / 表達式
- 實盤下單或券商 API
- 多組 profile UI（#85 / #98 分點主力另票）
- 改既有 `/api/summary` 等儀表板 SQL；`dashboard.py` 不寫 `stock_chips_daily`
