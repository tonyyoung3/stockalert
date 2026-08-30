#!/usr/bin/env python3
"""台股資料儀表板(本地網頁)
  python dashboard.py        # 啟動後自動開瀏覽器 http://localhost:8765
只用 Python 內建套件,直接讀 twse_data.db,資料永遠是最新的。
"""
import json
import sqlite3
import sys
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent))
import backtest_engine

DB = Path(__file__).parent / "twse_data.db"
PORT = 8765


def q(sql, params=()):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def api(path, qs):
    days = int(qs.get("days", ["90"])[0])
    if path == "/api/summary":
        idx = q("SELECT ts, index_value, change FROM taiex_hourly ORDER BY ts DESC LIMIT 1")
        latest = q("SELECT MAX(trade_date) FROM foreign_daily")[0][0]
        tot = q("SELECT SUM(foreign_net), COUNT(*) FROM foreign_daily WHERE trade_date=?", (latest,))
        span = q("SELECT MIN(trade_date), MAX(trade_date) FROM foreign_daily")
        return {"index": idx[0] if idx else None, "latest_date": latest,
                "foreign_net_total": tot[0][0], "stock_count": tot[0][1],
                "date_range": span[0]}
    if path == "/api/ohlc":
        if qs.get("interval", ["day"])[0] == "hour":
            rows = q("SELECT ts, open, high, low, close FROM taiex_hourly_ohlc "
                     "WHERE trade_date >= date('now', ?) ORDER BY ts", (f"-{days} day",))
        else:
            rows = q("SELECT trade_date, open, high, low, close FROM taiex_daily "
                     "WHERE trade_date >= date('now', ?) ORDER BY trade_date", (f"-{days} day",))
        return {"data": rows}
    if path == "/api/taiex":
        rows = q("SELECT ts, index_value FROM taiex_hourly "
                 "WHERE trade_date >= date('now', ?) ORDER BY ts", (f"-{days} day",))
        return {"data": rows}
    if path == "/api/foreign_total":
        rows = q("SELECT trade_date, SUM(foreign_net) FROM foreign_daily "
                 "WHERE trade_date >= date('now', ?) GROUP BY trade_date ORDER BY trade_date",
                 (f"-{days} day",))
        return {"data": rows}
    if path == "/api/top":
        latest = q("SELECT MAX(trade_date) FROM foreign_daily")[0][0]
        buy = q("SELECT stock_id, stock_name, foreign_net FROM foreign_daily "
                "WHERE trade_date=? ORDER BY foreign_net DESC LIMIT 15", (latest,))
        sell = q("SELECT stock_id, stock_name, foreign_net FROM foreign_daily "
                 "WHERE trade_date=? ORDER BY foreign_net ASC LIMIT 15", (latest,))
        return {"date": latest, "buy": buy, "sell": sell}
    if path == "/api/margin_total":
        fin = q("SELECT trade_date, balance FROM margin_total "
                "WHERE item LIKE '融資金額%' AND trade_date >= date('now', ?) "
                "ORDER BY trade_date", (f"-{days} day",))
        short = q("SELECT trade_date, balance FROM margin_total "
                  "WHERE item LIKE '融券%' AND trade_date >= date('now', ?) "
                  "ORDER BY trade_date", (f"-{days} day",))
        return {"fin": fin, "short": short}
    if path == "/api/stock_margin":
        sid = qs.get("id", [""])[0].strip()
        rows = q("SELECT trade_date, margin_balance, short_balance FROM margin_stock "
                 "WHERE stock_id=? AND trade_date >= date('now', ?) ORDER BY trade_date",
                 (sid, f"-{days} day",))
        return {"id": sid, "data": rows}
    if path == "/api/taifex_oi":
        rows = q("SELECT trade_date, investor, oi_net_lots FROM taifex_fut_oi "
                 "WHERE product='臺股期貨' AND trade_date >= date('now', ?) "
                 "ORDER BY trade_date", (f"-{days} day",))
        by_date = {}
        for d, inv, net in rows:
            by_date.setdefault(d, {})[inv] = net
        dates = sorted(by_date)
        foreign = [by_date[d].get("外資及陸資") for d in dates]
        trust = [by_date[d].get("投信") for d in dates]
        ratio = [round(f / t, 3) if (f is not None and t not in (None, 0)) else None
                 for f, t in zip(foreign, trust)]
        return {"dates": dates, "foreign": foreign, "trust": trust, "ratio": ratio}
    if path == "/api/stock":
        sid = qs.get("id", [""])[0].strip()
        rows = q("SELECT trade_date, stock_name, foreign_buy, foreign_sell, foreign_net "
                 "FROM foreign_daily WHERE stock_id=? AND trade_date >= date('now', ?) "
                 "ORDER BY trade_date", (sid, f"-{days} day"))
        return {"id": sid, "data": rows}
    return None


HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>台股資料儀表板</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-chart-financial@0.2.1"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.2.0"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif;background:#f8f9fa;color:#212529;line-height:1.5}
.wrap{max-width:1400px;margin:0 auto;padding:16px}
header{background:#1a1a2e;color:#fff;padding:18px 24px;border-radius:8px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
header h1{font-size:19px;font-weight:600}
select,input{padding:6px 10px;border:1px solid #ccc;border-radius:4px;font-size:13px}
header select{background:rgba(255,255,255,.1);color:#fff;border-color:rgba(255,255,255,.25)}
header select option{background:#1a1a2e}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:16px}
.card{background:#fff;border-radius:8px;padding:18px 22px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.kpi-label{font-size:12px;color:#6c757d;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.kpi-value{font-size:26px;font-weight:700}
.kpi-sub{font-size:13px;color:#6c757d}
.pos{color:#c0392b}.neg{color:#27ae60} /* 台股習慣:紅漲綠跌 */
.charts{display:grid;grid-template-columns:1fr;gap:16px;margin-bottom:16px}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px;margin-bottom:16px}
.card h3{font-size:14px;font-weight:600;margin-bottom:14px}
.chart-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.chart-head h3{margin:0}
.hint{font-size:12px;color:#adb5bd}
.reset-btn{padding:3px 10px;font-size:12px;border:1px solid #dee2e6;border-radius:4px;background:#fff;color:#6c757d;cursor:pointer}
.reset-btn:hover{background:#f0f0f0}
canvas{max-height:320px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 10px;border-bottom:2px solid #dee2e6;color:#6c757d;font-size:12px;white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid #f0f0f0}
tr:hover td{background:#f8f9fa}
.num{text-align:right;font-variant-numeric:tabular-nums}
.search{display:flex;gap:8px;margin-bottom:14px}
.search button{padding:6px 16px;border:none;border-radius:4px;background:#4C72B0;color:#fff;cursor:pointer}
footer{text-align:center;color:#adb5bd;font-size:12px;padding:12px}
@media(max-width:768px){.two{grid-template-columns:1fr}}
.bt-grid{display:grid;grid-template-columns:1fr;gap:14px}
.bt-box{border:1px solid #eee;border-radius:6px;padding:12px 14px}
.bt-label{font-size:12px;font-weight:600;color:#6c757d;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.bt-row{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.bt-row:last-child{margin-bottom:0}
.bt-sub{font-size:12px;color:#6c757d;margin-right:2px}
.bt-warn{margin-top:8px;padding:8px 10px;background:#fff3cd;border:1px solid #ffe08a;border-radius:4px;font-size:12.5px;color:#7a5c00}
.bt-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
.bt-kpi{background:#f8f9fa;border-radius:6px;padding:10px 14px}
.bt-kpi .l{font-size:11px;color:#6c757d;text-transform:uppercase}
.bt-kpi .v{font-size:20px;font-weight:700}
.bt-section-title{font-size:13px;font-weight:600;margin:16px 0 8px}
.bt-error{padding:12px;background:#fdecea;border:1px solid #f5c2c0;border-radius:4px;color:#a94442}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>台股資料儀表板</h1>
  <div>顯示範圍
    <select id="days" onchange="loadAll()">
      <option value="30">近 30 天</option>
      <option value="90" selected>近 90 天</option>
      <option value="365">近 1 年</option>
      <option value="730">近 2 年</option>
    </select>
  </div>
</header>

<section class="kpis">
  <div class="card"><div class="kpi-label">加權指數(最新)</div><div class="kpi-value" id="k-idx">–</div><div class="kpi-sub" id="k-chg"></div></div>
  <div class="card"><div class="kpi-label">外資合計買賣超</div><div class="kpi-value" id="k-net">–</div><div class="kpi-sub" id="k-netdate"></div></div>
  <div class="card"><div class="kpi-label">收錄個股數</div><div class="kpi-value" id="k-cnt">–</div><div class="kpi-sub">最新交易日</div></div>
  <div class="card"><div class="kpi-label">資料期間</div><div class="kpi-value" id="k-span" style="font-size:16px">–</div><div class="kpi-sub">foreign_daily</div></div>
</section>

<section class="charts">
  <div class="card">
    <div class="chart-head"><h3>加權指數 K 線(開高低收)</h3>
      <span>
        <select id="kint" onchange="loadKline()" style="margin-right:8px">
          <option value="day" selected>日 K</option>
          <option value="hour">小時 K</option>
        </select>
        <span class="hint">拖曳平移 · 滾輪縮放　</span><button class="reset-btn" onclick="resetZoom('c-kline')">重置</button></span></div>
    <canvas id="c-kline"></canvas></div>
  <div class="card">
    <div class="chart-head"><h3>加權指數走勢(每小時)</h3>
      <span>
        <select id="overlay" onchange="loadTaiex()" style="margin-right:8px">
          <option value="none" selected>不疊圖</option>
          <option value="taifex_ratio">疊圖:外資÷投信 台指期未平倉比</option>
          <option value="foreign_net">疊圖:外資每日買賣超</option>
          <option value="margin_fin">疊圖:融資餘額</option>
          <option value="margin_short">疊圖:融券餘額</option>
        </select>
        <span class="hint">拖曳平移 · 滾輪縮放　</span><button class="reset-btn" onclick="resetZoom('c-taiex')">重置</button></span></div>
    <canvas id="c-taiex"></canvas></div>
  <div class="card">
    <div class="chart-head"><h3>台指期未平倉:外資 / 投信 淨額口數 與比值</h3>
      <span><span class="hint">虛線為外資÷投信比值(右軸,恆為負)　拖曳平移 · 滾輪縮放　</span><button class="reset-btn" onclick="resetZoom('c-taifex')">重置</button></span></div>
    <canvas id="c-taifex"></canvas></div>
  <div class="card">
    <div class="chart-head"><h3>整體融資融券餘額</h3>
      <span><span class="hint">拖曳平移 · 滾輪縮放　</span><button class="reset-btn" onclick="resetZoom('c-margin')">重置</button></span></div>
    <canvas id="c-margin"></canvas></div>
  <div class="card">
    <div class="chart-head"><h3>外資每日合計買賣超(張)</h3>
      <span><span class="hint">拖曳平移 · 滾輪縮放　</span><button class="reset-btn" onclick="resetZoom('c-net')">重置</button></span></div>
    <canvas id="c-net"></canvas></div>
</section>

<section class="two">
  <div class="card"><h3 id="t-buy">外資買超前 15</h3><canvas id="c-buy"></canvas></div>
  <div class="card"><h3 id="t-sell">外資賣超前 15</h3><canvas id="c-sell"></canvas></div>
</section>

<section class="card" style="margin-bottom:16px">
  <div class="chart-head"><h3>個股外資買賣超查詢</h3>
    <span><span class="hint">拖曳平移 · 滾輪縮放　</span><button class="reset-btn" onclick="resetZoom('c-stock')">重置</button></span></div>
  <div class="search">
    <input id="sid" placeholder="輸入股票代號,例如 2330" onkeydown="if(event.key==='Enter')loadStock()">
    <button onclick="loadStock()">查詢</button>
  </div>
  <canvas id="c-stock" style="display:none"></canvas>
  <canvas id="c-stock-margin" style="display:none;margin-top:16px"></canvas>
  <div id="stock-table"></div>
</section>

<section class="card" style="margin-bottom:16px">
  <h3 style="margin-bottom:14px">策略回測</h3>

  <div class="bt-grid">
    <div class="bt-box">
      <div class="bt-label">資料集</div>
      <select id="bt-dataset" onchange="btOnDatasetChange()">
        <option value="2y_hourly" selected>2年小時K(支援日內事件)</option>
        <option value="15y_daily">15年日K(只能整天賭注,見下方警告)</option>
      </select>
      <div id="bt-stale-warning" class="bt-warn" style="display:none">
        ⚠️ 官方日K開盤價 99.7% 等於前一天收盤價(陳舊開盤價陷阱),用它模擬「日內事件觸發」是幻覺。
        這個資料集只開放「隔夜模式」(前一天收盤進場、隔日出場),日內模式已停用。
      </div>
    </div>

    <div class="bt-box">
      <div class="bt-label">篩選條件(用「前一交易日已知」的資訊,不含未來資訊)</div>
      <div class="bt-row">
        <span class="bt-sub">星期</span>
        <label><input type="checkbox" class="bt-dow" value="0" checked>一</label>
        <label><input type="checkbox" class="bt-dow" value="1" checked>二</label>
        <label><input type="checkbox" class="bt-dow" value="2" checked>三</label>
        <label><input type="checkbox" class="bt-dow" value="3" checked>四</label>
        <label><input type="checkbox" class="bt-dow" value="4" checked>五</label>
      </div>
      <div class="bt-row">
        <span class="bt-sub">趨勢濾網</span>
        <select id="bt-trend">
          <option value="none" selected>不篩</option>
          <option value="above_ma20">前收 > MA20</option>
          <option value="below_ma20">前收 < MA20</option>
          <option value="above_ma60">前收 > MA60(季線)</option>
          <option value="below_ma60">前收 < MA60(季線)</option>
          <option value="above_ma20_today">今收 > MA20(今日)</option>
          <option value="below_ma20_today">今收 < MA20(今日)</option>
          <option value="above_ma60_today">今收 > MA60/季線(今日)</option>
          <option value="below_ma60_today">今收 < MA60/季線(今日)</option>
        </select>
        <span class="bt-sub">前一日漲跌</span>
        <select id="bt-prevday">
          <option value="none" selected>不篩</option>
          <option value="up">前一日上漲</option>
          <option value="down">前一日下跌</option>
        </select>
      </div>
      <div class="bt-row">
        <span class="bt-sub">當日跳空方向</span>
        <select id="bt-gapdir">
          <option value="any" selected>不篩</option>
          <option value="up">跳空漲</option>
          <option value="down">跳空跌</option>
        </select>
        <span class="bt-sub">最小 |跳空| %</span>
        <input type="number" id="bt-gapmin" value="0" step="0.1" style="width:70px">
      </div>
      <div class="bt-row">
        <span class="bt-sub">當日漲跌方向(收盤才確定,適合收盤進場規則)</span>
        <select id="bt-dayretdir">
          <option value="any" selected>不篩</option>
          <option value="up">當日上漲</option>
          <option value="down">當日下跌</option>
        </select>
        <span class="bt-sub">最小 |當日漲跌| %</span>
        <input type="number" id="bt-dayretmin" value="0" step="0.1" style="width:70px">
      </div>
      <div class="bt-row">
        <span class="bt-sub">均線交叉(MA20 vs MA60,收盤才確定)</span>
        <select id="bt-macross">
          <option value="none" selected>不篩</option>
          <option value="golden">黃金交叉(今天)</option>
          <option value="death">死亡交叉(今天)</option>
        </select>
        <span class="bt-sub">N日新高/新低突破(收盤才確定)</span>
        <select id="bt-breakout">
          <option value="none" selected>不篩</option>
          <option value="n_day_high">創N日新高</option>
          <option value="n_day_low">破N日新低</option>
        </select>
        <span class="bt-sub">N=</span>
        <input type="number" id="bt-breakoutwindow" value="20" step="1" min="2" style="width:60px">
      </div>
      <div class="bt-row">
        <span class="bt-sub">外資/投信台指期未平倉比(前一交易日值,只有近~3年TAIFEX資料)</span>
        <select id="bt-oiratio">
          <option value="none" selected>不篩</option>
          <option value="below_pctile">低於分位門檻(比值相對更負)</option>
          <option value="above_pctile">高於分位門檻(比值相對較不負)</option>
        </select>
        <span class="bt-sub">分位門檻(0-100)</span>
        <input type="number" id="bt-oipctile" value="25" step="1" min="0" max="100" style="width:60px">
        <span class="bt-sub">回看天數</span>
        <input type="number" id="bt-oiwindow" value="60" step="5" min="10" style="width:60px">
      </div>
      <p class="hint" id="bt-close-decided-hint" style="display:none">⚠️ 均線交叉 / N日新高新低突破 / 當日漲跌 / 今日均線 這幾種濾網要等收盤才能確定,不能用在「日內模式」(會偷看未來資訊)。切到隔夜或波段模式才能套用。</p>
    </div>

    <div class="bt-box">
      <div class="bt-label">模式</div>
      <label><input type="radio" name="bt-mode" value="intraday" checked onchange="btOnModeChange()"> 日內(當天進出)</label>
      <label style="margin-left:14px"><input type="radio" name="bt-mode" value="overnight" onchange="btOnModeChange()"> 隔夜(收盤進、隔日出)</label>
      <label style="margin-left:14px"><input type="radio" name="bt-mode" value="swing" onchange="btOnModeChange()"> 波段(收盤進、固定%停損、可多日持有)</label>
    </div>

    <div class="bt-box" id="bt-intraday-box">
      <div class="bt-label">日內規則</div>
      <div class="bt-row">
        <span class="bt-sub">進場參考價</span>
        <select id="bt-ref">
          <option value="first_hour_high">第一小時高點</option>
          <option value="first_hour_low">第一小時低點</option>
          <option value="day_open">當日開盤價</option>
          <option value="prev_close">前一日收盤價</option>
        </select>
        <span class="bt-sub">偏移 %(可負,例如回檔0.5%填-0.5)</span>
        <input type="number" id="bt-offset" value="0" step="0.1" style="width:80px">
      </div>
      <div class="bt-row">
        <span class="bt-sub">觸發方式</span>
        <select id="bt-trigger">
          <option value="touch_from_below">價格向下觸及(跌破/回檔)</option>
          <option value="touch_from_above">價格向上觸及(突破)</option>
        </select>
        <span class="bt-sub">方向</span>
        <select id="bt-direction">
          <option value="long">做多</option>
          <option value="short">做空</option>
        </select>
      </div>
      <div class="bt-row">
        <span class="bt-sub">最早檢查時間</span>
        <select id="bt-earliest">
          <option value="9">09:00</option>
          <option value="10" selected>10:00</option>
          <option value="11">11:00</option>
          <option value="12">12:00</option>
        </select>
        <span class="bt-sub">出場時間</span>
        <select id="bt-exithour">
          <option value="10">10:00收盤</option>
          <option value="11">11:00收盤</option>
          <option value="12">12:00收盤</option>
          <option value="13" selected>當日收盤(13:30)</option>
        </select>
      </div>
      <div class="bt-row">
        <label><input type="checkbox" id="bt-stop-on" onchange="btOnStopToggle()"> 啟用停損</label>
      </div>
      <div class="bt-row" id="bt-stop-box" style="display:none">
        <span class="bt-sub">停損參考價</span>
        <select id="bt-stopref">
          <option value="day_open">當日開盤價</option>
          <option value="entry_price">進場價</option>
          <option value="first_hour_high">第一小時高點</option>
          <option value="first_hour_low">第一小時低點</option>
        </select>
        <span class="bt-sub">偏移 %</span>
        <input type="number" id="bt-stopoffset" value="0" step="0.1" style="width:80px">
      </div>
    </div>

    <div class="bt-box" id="bt-overnight-box" style="display:none">
      <div class="bt-label">隔夜規則</div>
      <div class="bt-row">
        <span class="bt-sub">方向</span>
        <select id="bt-on-direction">
          <option value="long">做多</option>
          <option value="short">做空</option>
        </select>
        <span class="bt-sub">出場時機</span>
        <select id="bt-holdto">
          <option value="next_open">隔日開盤</option>
          <option value="next_close">隔日收盤</option>
          <option value="next_hour">隔日某小時收盤(僅2年小時K)</option>
        </select>
        <span class="bt-sub" id="bt-holdhour-label" style="display:none">小時</span>
        <select id="bt-holdhour" style="display:none">
          <option value="10" selected>10:00</option>
          <option value="11">11:00</option>
          <option value="12">12:00</option>
        </select>
      </div>
      <div class="bt-row">
        <label><input type="checkbox" id="bt-skipweekend" checked> 跳過週末(週五收盤不留倉)</label>
      </div>
    </div>

    <div class="bt-box" id="bt-swing-box" style="display:none">
      <div class="bt-label">波段規則(收盤進場,固定 % 停損,兩個資料集都可用)</div>
      <div class="bt-row">
        <span class="bt-sub">方向</span>
        <select id="bt-swing-direction">
          <option value="long">做多</option>
          <option value="short">做空</option>
        </select>
        <span class="bt-sub">停損 %(相對進場價)</span>
        <input type="number" id="bt-swing-stoppct" value="2" step="0.1" min="0.1" style="width:70px">
        <span class="bt-sub">最長持有天數</span>
        <input type="number" id="bt-swing-maxhold" value="60" step="1" min="1" style="width:70px">
      </div>
      <div class="bt-row">
        <label><input type="checkbox" id="bt-swing-tpon" onchange="btOnSwingTpToggle()"> 啟用停利</label>
        <span class="bt-sub" id="bt-swing-tp-label" style="display:none">停利 %(相對進場價)</span>
        <input type="number" id="bt-swing-tppct" value="5" step="0.1" min="0.1" style="width:70px; display:none">
      </div>
      <p class="hint">同一天停損停利都可能觸發時,保守假設先停損。訊號出現在資料尾端、還沒等到出場資料就用完的交易會被排除(不計入統計),不會用未知結果硬猜。</p>
    </div>

    <div class="bt-box">
      <div class="bt-row">
        <span class="bt-sub">來回成本 %</span>
        <input type="number" id="bt-cost" value="0.03" step="0.01" style="width:80px">
        <button onclick="runBacktest()" style="margin-left:16px;padding:8px 24px;background:#4C72B0;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:14px">執行回測</button>
      </div>
    </div>
  </div>

  <div id="bt-results" style="margin-top:20px"></div>
</section>

<footer>資料來源:台灣證券交易所 · 買賣超單位:張(千股)</footer>
</div>

<script>
const charts = {};
const ZOOM = {pan:{enabled:true, mode:'x'},
              zoom:{wheel:{enabled:true}, pinch:{enabled:true}, mode:'x'},
              limits:{x:{minRange:5}}};
function resetZoom(id){ if(charts[id]) charts[id].resetZoom(); }
const fmt = n => n==null ? '–' : n.toLocaleString('zh-TW');
const zhang = n => Math.round(n/1000);  // 股 -> 張

function mk(id, cfg){ if(charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), cfg); return charts[id]; }

async function j(u){ return (await fetch(u)).json(); }
const days = () => document.getElementById('days').value;

async function loadSummary(){
  const s = await j('/api/summary');
  if(s.index){
    document.getElementById('k-idx').textContent = fmt(s.index[1]);
    const c = s.index[2];
    const el = document.getElementById('k-chg');
    if(c!=null){ el.textContent = (c>=0?'+':'')+fmt(c); el.className = 'kpi-sub '+(c>=0?'pos':'neg'); }
  }
  const net = s.foreign_net_total;
  const kn = document.getElementById('k-net');
  kn.textContent = (net>=0?'+':'')+fmt(zhang(net))+' 張';
  kn.className = 'kpi-value '+(net>=0?'pos':'neg');
  document.getElementById('k-netdate').textContent = s.latest_date;
  document.getElementById('k-cnt').textContent = fmt(s.stock_count);
  document.getElementById('k-span').textContent = s.date_range[0]+' ~ '+s.date_range[1];
}

async function loadKline(){
  const itv = document.getElementById('kint').value;
  const r = await j('/api/ohlc?days='+days()+'&interval='+itv);
  if(!r.data.length){
    if(itv==='hour') document.getElementById('c-kline').parentElement.querySelector('h3')
      .textContent = '加權指數 K 線(尚無小時K資料,請先執行 backfill.py index)';
    return;
  }
  mk('c-kline', {type:'candlestick',
    data:{datasets:[{data:r.data.map(d=>({x:new Date(d[0]).getTime(), o:d[1], h:d[2], l:d[3], c:d[4]})),
      color:{up:'#c0392b', down:'#27ae60', unchanged:'#6c757d'},
      borderColor:{up:'#c0392b', down:'#27ae60', unchanged:'#6c757d'}}]},
    options:{animation:false, plugins:{legend:{display:false}, zoom:ZOOM},
      scales:{x:{type:'timeseries', time:{unit: itv==='hour' ? 'day' : 'month'},
                 ticks:{maxTicksLimit:14}},
              y:{grace:'2%'}}}});
}

const OVERLAY_LABELS = {
  taifex_ratio: '外資÷投信 台指期未平倉比(恆為負)',
  foreign_net: '外資每日買賣超(張)',
  margin_fin: '融資餘額(億元)',
  margin_short: '融券餘額(千張)',
};

// 疊圖資料都是日頻,指數走勢是小時頻 —— 用「當日值」對齊到當天每個小時點上,
// 畫出來像階梯狀,一天內同一條水平線,換日才跳到新值,方便跟指數同框比較。
async function buildOverlayMap(kind){
  const map = new Map();
  if(kind==='taifex_ratio'){
    const r = await j('/api/taifex_oi?days='+days());
    r.dates.forEach((dt,i)=>{ if(r.ratio[i]!=null) map.set(dt, r.ratio[i]); });
  } else if(kind==='foreign_net'){
    const r = await j('/api/foreign_total?days='+days());
    r.data.forEach(d=>map.set(d[0], zhang(d[1])));
  } else if(kind==='margin_fin'){
    const r = await j('/api/margin_total?days='+days());
    r.fin.forEach(d=>map.set(d[0], Math.round(d[1]/100000)));
  } else if(kind==='margin_short'){
    const r = await j('/api/margin_total?days='+days());
    r.short.forEach(d=>map.set(d[0], Math.round(d[1]/1000)));
  }
  return map;
}

async function loadTaiex(){
  const r = await j('/api/taiex?days='+days());
  const labels = r.data.map(d=>d[0].slice(0,16).replace('T',' '));
  const datasets = [{label:'加權指數', data:r.data.map(d=>d[1]), borderColor:'#4C72B0',
    borderWidth:1.5, pointRadius:0, tension:.2}];

  const ov = document.getElementById('overlay').value;
  const scales = {x:{ticks:{maxTicksLimit:12}}, y:{grace:'2%'}};
  if(ov !== 'none'){
    const map = await buildOverlayMap(ov);
    const vals = r.data.map(d => { const dt=d[0].slice(0,10); return map.has(dt) ? map.get(dt) : null; });
    datasets.push({label:OVERLAY_LABELS[ov], data:vals, borderColor:'#c0392b',
      borderDash:[5,4], borderWidth:2, pointRadius:0, stepped:true, spanGaps:true, yAxisID:'y2'});
    // 比值恆為負,鎖定右軸範圍避免自動刻度出現容易誤讀的 0 / 正值刻度
    if(ov==='taifex_ratio'){
      const vv = vals.filter(v=>v!=null);
      if(vv.length){
        const lo=Math.min(...vv), hi=Math.max(...vv), pad=(hi-lo)*0.15||0.1;
        scales.y2 = {position:'right', grid:{display:false}, min:lo-pad, max:hi+pad};
      }
    } else {
      scales.y2 = {position:'right', grid:{display:false}};
    }
  }

  mk('c-taiex', {type:'line', data:{labels, datasets},
    options:{animation:false, plugins:{legend:{display: ov!=='none'}, zoom:ZOOM},
      interaction:{mode:'index',intersect:false},
      scales}});
}

async function loadNet(){
  const r = await j('/api/foreign_total?days='+days());
  mk('c-net', {type:'bar', data:{labels:r.data.map(d=>d[0]),
    datasets:[{data:r.data.map(d=>zhang(d[1])),
      backgroundColor:r.data.map(d=>d[1]>=0?'#c0392bcc':'#27ae60cc')}]},
    options:{animation:false, plugins:{legend:{display:false}, zoom:ZOOM},
      scales:{x:{ticks:{maxTicksLimit:15}}}}});
}

async function loadMargin(){
  const r = await j('/api/margin_total?days='+days());
  if(!r.fin.length && !r.short.length) return;
  mk('c-margin', {type:'line', data:{labels:(r.fin.length?r.fin:r.short).map(d=>d[0]),
    datasets:[
      {label:'融資餘額(億元)', data:r.fin.map(d=>Math.round(d[1]/100000)),
       borderColor:'#DD8452', backgroundColor:'#DD845220', borderWidth:2, pointRadius:0, tension:.2, fill:true},
      {label:'融券餘額(千張)', data:r.short.map(d=>Math.round(d[1]/1000)),
       borderColor:'#8172B3', borderWidth:2, pointRadius:0, tension:.2, yAxisID:'y2'}]},
    options:{animation:false, interaction:{mode:'index',intersect:false},
      plugins:{zoom:ZOOM},
      scales:{x:{ticks:{maxTicksLimit:12}},
              y2:{position:'right', grid:{display:false}}}}});
}

async function loadTaifexOi(){
  const r = await j('/api/taifex_oi?days='+days());
  const card = document.getElementById('c-taifex').closest('.card');
  if(!r.dates.length){
    card.querySelector('h3').textContent = '台指期未平倉(尚無資料,請先執行 taifex_collector.py recent)';
    return;
  }
  // 比值恆為負;顯式鎖定右軸範圍,避免自動刻度把 0 / 正值也畫進刻度列表造成誤讀
  const rv = r.ratio.filter(v=>v!=null);
  const rMin = Math.min(...rv), rMax = Math.max(...rv);
  const pad = (rMax-rMin)*0.15 || 0.1;
  mk('c-taifex', {type:'line', data:{labels:r.dates,
    datasets:[
      {label:'外資淨額(口)', data:r.foreign, borderColor:'#c0392b',
       borderWidth:1.5, pointRadius:0, tension:.2},
      {label:'投信淨額(口)', data:r.trust, borderColor:'#27ae60',
       borderWidth:1.5, pointRadius:0, tension:.2},
      {label:'外資÷投信比(恆為負)', data:r.ratio, borderColor:'#4C72B0', borderDash:[5,4],
       borderWidth:2, pointRadius:0, tension:.2, yAxisID:'y2'}]},
    options:{animation:false, interaction:{mode:'index',intersect:false},
      plugins:{zoom:ZOOM},
      scales:{x:{ticks:{maxTicksLimit:12}},
              y2:{position:'right', grid:{display:false},
                  min: rMin-pad, max: rMax+pad}}}});
}

async function loadTop(){
  const r = await j('/api/top');
  document.getElementById('t-buy').textContent = r.date+' 外資買超前 15(張)';
  document.getElementById('t-sell').textContent = r.date+' 外資賣超前 15(張)';
  const cfg = (rows, color) => ({type:'bar',
    data:{labels:rows.map(d=>d[0]+' '+d[1]),
      datasets:[{data:rows.map(d=>Math.abs(zhang(d[2]))), backgroundColor:color}]},
    options:{indexAxis:'y', animation:false, plugins:{legend:{display:false}},
      scales:{y:{ticks:{font:{size:11}}}}}});
  mk('c-buy', cfg(r.buy, '#c0392bcc'));
  mk('c-sell', cfg(r.sell, '#27ae60cc'));
}

async function loadStock(){
  const sid = document.getElementById('sid').value.trim();
  if(!sid) return;
  const r = await j('/api/stock?id='+encodeURIComponent(sid)+'&days='+days());
  const cv = document.getElementById('c-stock');
  const tb = document.getElementById('stock-table');
  if(!r.data.length){ cv.style.display='none'; tb.innerHTML='<p style="color:#6c757d">查無 '+sid+' 的資料</p>'; return; }
  cv.style.display='block';
  let cum = 0; const cumData = r.data.map(d=>cum += zhang(d[4]));
  mk('c-stock', {type:'bar', data:{labels:r.data.map(d=>d[0]),
    datasets:[
      {label:'每日買賣超(張)', data:r.data.map(d=>zhang(d[4])),
       backgroundColor:r.data.map(d=>d[4]>=0?'#c0392bcc':'#27ae60cc'), order:2},
      {label:'累計(張)', data:cumData, type:'line', borderColor:'#4C72B0',
       pointRadius:0, borderWidth:2, tension:.2, yAxisID:'y2', order:1}]},
    options:{animation:false, interaction:{mode:'index',intersect:false},
      plugins:{zoom:ZOOM},
      scales:{x:{ticks:{maxTicksLimit:15}}, y2:{position:'right', grid:{display:false}}}}});
  // 個股融資融券餘額
  const m = await j('/api/stock_margin?id='+encodeURIComponent(sid)+'&days='+days());
  const cvm = document.getElementById('c-stock-margin');
  if(m.data.length){
    cvm.style.display='block';
    mk('c-stock-margin', {type:'line', data:{labels:m.data.map(d=>d[0]),
      datasets:[
        {label:'融資餘額(張)', data:m.data.map(d=>d[1]), borderColor:'#DD8452',
         borderWidth:2, pointRadius:0, tension:.2},
        {label:'融券餘額(張)', data:m.data.map(d=>d[2]), borderColor:'#8172B3',
         borderWidth:2, pointRadius:0, tension:.2, yAxisID:'y2'}]},
      options:{animation:false, interaction:{mode:'index',intersect:false},
        plugins:{zoom:ZOOM},
        scales:{x:{ticks:{maxTicksLimit:12}}, y2:{position:'right', grid:{display:false}}}}});
  } else { cvm.style.display='none'; }
  const last = r.data.slice(-15).reverse();
  tb.innerHTML = '<table><thead><tr><th>日期</th><th>名稱</th><th class="num">買進(張)</th><th class="num">賣出(張)</th><th class="num">買賣超(張)</th></tr></thead><tbody>'
    + last.map(d=>'<tr><td>'+d[0]+'</td><td>'+d[1]+'</td><td class="num">'+fmt(zhang(d[2]))
      +'</td><td class="num">'+fmt(zhang(d[3]))+'</td><td class="num '+(d[4]>=0?'pos':'neg')+'">'
      +(d[4]>=0?'+':'')+fmt(zhang(d[4]))+'</td></tr>').join('')
    + '</tbody></table>';
}

// ---------------------------------------------------------------- 策略回測

const DOW_NAME = ['週一','週二','週三','週四','週五'];

function btOnDatasetChange(){
  const ds = document.getElementById('bt-dataset').value;
  const warn = document.getElementById('bt-stale-warning');
  const intradayRadio = document.querySelector('input[name="bt-mode"][value="intraday"]');
  const overnightRadio = document.querySelector('input[name="bt-mode"][value="overnight"]');
  const holdHourOpt = document.querySelector('#bt-holdto option[value="next_hour"]');
  if(ds === '15y_daily'){
    warn.style.display = 'block';
    intradayRadio.disabled = true;
    if(intradayRadio.checked) overnightRadio.checked = true;
    holdHourOpt.disabled = true;
    btOnModeChange();
  } else {
    warn.style.display = 'none';
    intradayRadio.disabled = false;
    holdHourOpt.disabled = false;
  }
}

function btOnModeChange(){
  const mode = document.querySelector('input[name="bt-mode"]:checked').value;
  document.getElementById('bt-intraday-box').style.display = mode==='intraday' ? 'block' : 'none';
  document.getElementById('bt-overnight-box').style.display = mode==='overnight' ? 'block' : 'none';
  document.getElementById('bt-swing-box').style.display = mode==='swing' ? 'block' : 'none';

  // 「收盤才確定」的濾網(均線交叉/N日突破/當日漲跌/今日均線)在日內模式下會偷看未來
  // 資訊,直接停用對應控制項並提示,而不是等送出去才被後端擋掉
  const closeDecided = mode === 'intraday';
  document.getElementById('bt-close-decided-hint').style.display = closeDecided ? 'block' : 'none';
  ['bt-macross','bt-breakout','bt-breakoutwindow','bt-dayretdir','bt-dayretmin'].forEach(id=>{
    document.getElementById(id).disabled = closeDecided;
  });
  document.querySelectorAll('#bt-trend option[value$="_today"]').forEach(opt=>{ opt.disabled = closeDecided; });
  if(closeDecided){
    document.getElementById('bt-macross').value = 'none';
    document.getElementById('bt-breakout').value = 'none';
    document.getElementById('bt-dayretdir').value = 'any';
    document.getElementById('bt-dayretmin').value = '0';
    const trendSel = document.getElementById('bt-trend');
    if(trendSel.value.endsWith('_today')) trendSel.value = 'none';
  }
}

function btOnStopToggle(){
  document.getElementById('bt-stop-box').style.display =
    document.getElementById('bt-stop-on').checked ? 'flex' : 'none';
}

function btOnSwingTpToggle(){
  const show = document.getElementById('bt-swing-tpon').checked;
  document.getElementById('bt-swing-tp-label').style.display = show ? 'inline' : 'none';
  document.getElementById('bt-swing-tppct').style.display = show ? 'inline-block' : 'none';
}

function btOnHoldToChange(){
  const show = document.getElementById('bt-holdto').value === 'next_hour';
  document.getElementById('bt-holdhour-label').style.display = show ? 'inline' : 'none';
  document.getElementById('bt-holdhour').style.display = show ? 'inline-block' : 'none';
}
document.getElementById('bt-holdto').addEventListener('change', btOnHoldToChange);

function btBuildRule(){
  const dow = [...document.querySelectorAll('.bt-dow:checked')].map(el=>parseInt(el.value));
  const mode = document.querySelector('input[name="bt-mode"]:checked').value;
  const rule = {
    dataset: document.getElementById('bt-dataset').value,
    mode: mode,
    filters: {
      weekdays: dow,
      trend: document.getElementById('bt-trend').value,
      prev_day: document.getElementById('bt-prevday').value,
      gap_dir: document.getElementById('bt-gapdir').value,
      gap_abs_min_pct: parseFloat(document.getElementById('bt-gapmin').value)||0,
      day_ret_dir: document.getElementById('bt-dayretdir').value,
      day_ret_min_pct: parseFloat(document.getElementById('bt-dayretmin').value)||0,
      ma_cross: document.getElementById('bt-macross').value,
      breakout: document.getElementById('bt-breakout').value,
      breakout_window: parseInt(document.getElementById('bt-breakoutwindow').value)||20,
      oi_ratio_mode: document.getElementById('bt-oiratio').value,
      oi_ratio_pctile: parseFloat(document.getElementById('bt-oipctile').value)||25,
      oi_ratio_window: parseInt(document.getElementById('bt-oiwindow').value)||60,
    },
    cost_pct: parseFloat(document.getElementById('bt-cost').value)||0,
  };
  if(mode==='intraday'){
    rule.entry = {
      reference: document.getElementById('bt-ref').value,
      offset_pct: parseFloat(document.getElementById('bt-offset').value)||0,
      trigger: document.getElementById('bt-trigger').value,
      direction: document.getElementById('bt-direction').value,
      earliest_hour: parseInt(document.getElementById('bt-earliest').value),
    };
    rule.exit_hour = parseInt(document.getElementById('bt-exithour').value);
    rule.stop = {
      enabled: document.getElementById('bt-stop-on').checked,
      reference: document.getElementById('bt-stopref').value,
      offset_pct: parseFloat(document.getElementById('bt-stopoffset').value)||0,
    };
  } else if(mode==='overnight'){
    rule.direction = document.getElementById('bt-on-direction').value;
    rule.hold_to = document.getElementById('bt-holdto').value;
    rule.hold_to_hour = parseInt(document.getElementById('bt-holdhour').value);
    rule.skip_weekend = document.getElementById('bt-skipweekend').checked;
  } else {
    rule.direction = document.getElementById('bt-swing-direction').value;
    rule.stop_pct = parseFloat(document.getElementById('bt-swing-stoppct').value)||2;
    rule.max_hold_days = parseInt(document.getElementById('bt-swing-maxhold').value)||60;
    rule.take_profit_on = document.getElementById('bt-swing-tpon').checked;
    rule.take_profit_pct = parseFloat(document.getElementById('bt-swing-tppct').value)||0;
  }
  return rule;
}

async function runBacktest(){
  const box = document.getElementById('bt-results');
  box.innerHTML = '<p class="hint">回測中…</p>';
  let data;
  try{
    const resp = await fetch('/api/backtest', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(btBuildRule())});
    data = await resp.json();
  }catch(e){ box.innerHTML = '<div class="bt-error">請求失敗:'+e+'</div>'; return; }
  renderBacktestResult(data);
}

function renderBacktestResult(d){
  const box = document.getElementById('bt-results');
  if(d.error){ box.innerHTML = '<div class="bt-error">'+d.error+'</div>'; return; }
  if(!d.n){ box.innerHTML = '<div class="bt-error">沒有任何交易被觸發(篩選後 '+d.days_passed_filter+' / '+d.total_days_in_dataset+' 天符合條件,但沒有一天觸發進場)。試試放寬篩選條件或調整進場規則。</div>'; return; }

  let html = '';
  if(d.stale_open_warning){
    html += '<div class="bt-warn">⚠️ 這個資料集用的是官方開盤價,90%以上時間等於前一天收盤價,任何用到「開盤價」的日內判斷都不可信,只看隔夜(收盤→收盤/開盤)的結果。</div>';
  }
  const evClass = d.ev_pct>=0 ? 'pos' : 'neg';
  html += '<div class="bt-kpis">'
    + '<div class="bt-kpi"><div class="l">交易數</div><div class="v">'+d.n+'</div></div>'
    + '<div class="bt-kpi"><div class="l">每筆淨益</div><div class="v '+evClass+'">'+(d.ev_pct>=0?'+':'')+d.ev_pct+'%</div></div>'
    + '<div class="bt-kpi"><div class="l">勝率</div><div class="v">'+d.win_rate+'%</div></div>'
    + '<div class="bt-kpi"><div class="l">t 值</div><div class="v">'+(d.t_stat??'–')+'</div></div>'
    + '<div class="bt-kpi"><div class="l">p 值</div><div class="v">'+(d.p_value??'–')+'</div></div>'
    + '<div class="bt-kpi"><div class="l">Sharpe(粗估年化)</div><div class="v">'+(d.sharpe??'–')+'</div></div>'
    + '<div class="bt-kpi"><div class="l">最慘 / 最好</div><div class="v" style="font-size:15px">'+d.worst_pct+'% / +'+d.best_pct+'%</div></div>'
    + (d.stopped_rate!=null ? '<div class="bt-kpi"><div class="l">停損觸發率</div><div class="v">'+d.stopped_rate+'%</div></div>' : '')
    + (d.avg_hold_days!=null ? '<div class="bt-kpi"><div class="l">平均持有天數</div><div class="v">'+d.avg_hold_days+'</div></div>' : '')
    + (d.max_drawdown&&d.max_drawdown.mdd_pct!=null ? '<div class="bt-kpi"><div class="l">最大回撤(MDD)</div><div class="v neg">'+d.max_drawdown.mdd_pct+'%</div></div>' : '')
    + (d.profit_factor!=null ? '<div class="bt-kpi"><div class="l">獲利因子</div><div class="v '+(d.profit_factor>=1?'pos':'neg')+'">'+d.profit_factor+'</div></div>' : '')
    + (d.payoff_ratio!=null ? '<div class="bt-kpi"><div class="l">賺賠比(均賺/均賠)</div><div class="v">'+d.payoff_ratio+'</div></div>' : '')
    + '<div class="bt-kpi"><div class="l">最大連續虧損</div><div class="v">'+(d.max_consec_losses??'–')+' 筆</div></div>'
    + '</div>';

  if(d.exit_reason_pct){
    const RN = {stop:'停損', take_profit:'停利', max_hold:'到期(未觸發停損/停利)'};
    html += '<div class="bt-section-title">出場原因分布</div>';
    html += '<p>' + Object.entries(d.exit_reason_pct).map(([k,v])=>(RN[k]||k)+': <b>'+v+'%</b>').join('　') + '</p>';
  }
  if(d.overlap_pct!=null){
    const warnCls = d.overlap_pct > 20 ? 'bt-warn' : '';
    html += '<div class="'+warnCls+'" style="margin:8px 0">'
      + (d.overlap_pct > 20 ? '⚠️ ' : '')
      + '重疊部位比例:<b>'+d.overlap_pct+'%</b> 的交易在進場時,上一筆同規則的倉位理論上還沒出場'
      + (d.overlap_pct > 20 ? '(比例偏高,樣本之間並不獨立,顯著性檢定的參考價值會被高估,區塊拔靴的結論比 t/p 值更可信)' : '')
      + '。</div>';
  }
  if(d.unresolved_trades){
    html += '<p class="hint">另有 '+d.unresolved_trades+' 個訊號出現在資料尾端,出場前資料就用完了(結果未知),已從統計中排除。</p>';
  }

  html += '<div class="bt-section-title">前後半穩定性(判斷是否為單一時段拖動整體結果)</div>';
  html += '<p>前半 EV = <b>'+(d.front_half_ev_pct??'–')+'%</b>　後半 EV = <b>'+(d.back_half_ev_pct??'–')+'%</b></p>';

  if(d.block_bootstrap_ci){
    const b = d.block_bootstrap_ci;
    const sig = (b.lo_pct*b.hi_pct>0);
    html += '<div class="bt-section-title">區塊拔靴 95% 信賴區間(以 '+b.block_days+' 天為區塊,處理事件群聚/重疊問題)</div>';
    html += '<p>[' + b.lo_pct + '%, ' + b.hi_pct + '%]　' + (sig ? '<b class="pos">不含0,統計上站得住</b>' : '<b class="neg">含0,不顯著,建議保守看待</b>') + '　(區塊數='+b.n_blocks+')</p>';
  } else {
    html += '<div class="bt-section-title">區塊拔靴</div><p class="hint">樣本數太少(&lt;30),略過區塊拔靴檢定。</p>';
  }

  html += '<div class="bt-section-title">成本敏感度(來回成本從0倍到3倍現有設定,EV怎麼變)</div>';
  html += '<table><thead><tr><th>成本%</th>' + d.cost_sensitivity.map(c=>'<th class="num">'+c.cost_pct+'%</th>').join('') + '</tr></thead>'
    + '<tbody><tr><td>每筆EV</td>' + d.cost_sensitivity.map(c=>'<td class="num '+(c.ev_pct>=0?'pos':'neg')+'">'+(c.ev_pct>=0?'+':'')+c.ev_pct+'%</td>').join('') + '</tr></tbody></table>';

  html += '<div class="bt-section-title">依星期拆解</div>';
  html += '<table><thead><tr><th>星期</th><th class="num">n</th><th class="num">EV</th><th class="num">勝率</th></tr></thead><tbody>'
    + d.by_weekday.map(w=>'<tr><td>'+DOW_NAME[w.dow]+'</td><td class="num">'+w.n+'</td><td class="num '+(w.ev_pct>=0?'pos':'neg')+'">'+(w.ev_pct>=0?'+':'')+w.ev_pct+'%</td><td class="num">'+w.win_rate+'%</td></tr>').join('')
    + '</tbody></table>';

  if(d.max_drawdown && d.max_drawdown.mdd_pct!=null){
    const m = d.max_drawdown;
    html += '<div class="bt-section-title">回撤分析</div>';
    html += '<p>最大回撤 <b class="neg">'+m.mdd_pct+'%</b>(第 '+(m.peak_idx+1)+' 筆的高點跌到第 '+(m.trough_idx+1)+' 筆)　'
      + '最長未創新高:<b>'+m.longest_dd_trades+'</b> 筆交易　'
      + '回撤修復:'+(m.trades_to_recover!=null ? '<b>'+m.trades_to_recover+'</b> 筆後回到前高' : '<b class="neg">至今尚未回到前高</b>')
      + '</p>';
    html += '<p class="hint">這是「每筆交易等權複利」的回撤,不是真實帳戶回撤(沒有部位大小、保證金、閒置資金的概念)。用途是看策略連續虧損能有多久多深,不能直接當「我會賠多少」。</p>';
  }

  if(d.excursion && d.excursion.all){
    const e = d.excursion;
    const row = (nm,o)=> o ? '<tr><td>'+nm+'</td><td class="num">'+o.n+'</td><td class="num neg">'+o.mae_avg_pct+'%</td><td class="num neg">'+o.mae_p90_pct+'%</td><td class="num pos">+'+o.mfe_avg_pct+'%</td></tr>' : '';
    html += '<div class="bt-section-title">MAE / MFE(最大不利 / 有利偏移 —— 調停損停利的直接依據)</div>';
    html += '<table><thead><tr><th></th><th class="num">n</th><th class="num">平均MAE</th><th class="num">最差10%的MAE</th><th class="num">平均MFE</th></tr></thead><tbody>'
      + row('全部', e.all) + row('賺錢的單', e.winners) + row('賠錢的單', e.losers)
      + '</tbody></table>';
    if(e.winners && e.losers){
      html += '<p class="hint">看法:賺錢的單平均最深被套 <b>'+e.winners.mae_avg_pct+'%</b>(最差10%到 '+e.winners.mae_p90_pct+'%)。'
        + '停損若設得比這還窄,會把本來會賺的單先洗掉。反過來,賺錢單的平均MFE是 +'+e.winners.mfe_avg_pct+'%,'
        + '若實際獲利遠低於它,代表出場太晚或缺停利。</p>';
    }
    if(e.coverage) html += '<p class="hint">MAE/MFE 涵蓋率 '+e.coverage+'(隔夜模式持倉多在休市時段,無盤中路徑者不計算)。</p>';
  }

  if(d.monthly && d.monthly.length){
    html += '<div class="bt-section-title">逐月報酬(該月所有交易複利,共 '+d.monthly.length+' 個月)'
      + '<button id="bt-monthly-toggle" style="margin-left:12px;padding:3px 12px;font-size:12px;cursor:pointer">展開/收合</button></div>';
    html += '<div id="bt-monthly-wrap" style="display:none;max-height:320px;overflow:auto;margin-top:8px">';
    html += '<table><thead><tr><th>月份</th><th class="num">交易數</th><th class="num">報酬</th><th class="num">勝率</th></tr></thead><tbody>'
      + d.monthly.map(m=>'<tr><td>'+m.month+'</td><td class="num">'+m.n+'</td>'
        + '<td class="num '+(m.ret_pct>=0?'pos':'neg')+'">'+(m.ret_pct>=0?'+':'')+m.ret_pct+'%</td>'
        + '<td class="num">'+m.win_rate+'%</td></tr>').join('')
      + '</tbody></table></div>';
    const pos = d.monthly.filter(m=>m.ret_pct>0).length;
    html += '<p>正報酬月份:<b>'+pos+' / '+d.monthly.length+'</b>('+(pos/d.monthly.length*100).toFixed(1)+'%)</p>';
  }

  if(d.price_series && d.price_series.length){
    html += '<div class="bt-section-title">進出場位置(標在指數走勢上)</div>';
    html += '<p class="hint">▲ 進場　▼ 出場。日內模式進出場同一天,兩個標記會重疊在同一個X位置。</p>';
    html += '<canvas id="bt-marker-chart" style="max-height:320px"></canvas>';
  }

  html += '<div class="bt-section-title">權益曲線(每筆交易複利,不代表資金曲線,只看形狀是否平穩)</div>';
  html += '<canvas id="bt-equity-chart" style="max-height:280px"></canvas>';

  if(d.trades && d.trades.length){
    const RN = {stop:'停損', take_profit:'停利', max_hold:'到期', exit_hour:'出場時間到', hold_to:'持有到期'};
    html += '<div class="bt-section-title">交易明細(每筆進出場時間與價位,共 '+d.trades.length+' 筆)'
      + '<button id="bt-trades-toggle" style="margin-left:12px;padding:3px 12px;font-size:12px;cursor:pointer">展開/收合</button>'
      + '<button id="bt-trades-csv" style="margin-left:6px;padding:3px 12px;font-size:12px;cursor:pointer">下載CSV</button>'
      + '</div>';
    html += '<p class="hint">觸價進場/停損只能定位到那一根K棒之內(日內為小時K、波段為日K),故標為區間或「盤中觸價」,不是精確到分秒的成交時點。</p>';
    html += '<div id="bt-trades-wrap" style="display:none;max-height:420px;overflow:auto;margin-top:8px">';
    html += '<table><thead><tr><th>#</th><th>星期</th><th>進場時間</th><th class="num">進場價</th>'
      + '<th>出場時間</th><th class="num">出場價</th>'
      + (d.mode==='swing' ? '<th class="num">持有天數</th>' : '')
      + '<th>出場原因</th><th class="num">MAE</th><th class="num">MFE</th><th class="num">淨報酬</th></tr></thead><tbody>';
    html += d.trades.map((t,i)=>'<tr>'
      + '<td>'+(i+1)+'</td>'
      + '<td>'+(DOW_NAME[t.dow]||'')+'</td>'
      + '<td>'+(t.entry_time||'–')+'</td>'
      + '<td class="num">'+t.entry_price+'</td>'
      + '<td>'+(t.exit_time||'–')+'</td>'
      + '<td class="num">'+t.exit_price+'</td>'
      + (d.mode==='swing' ? '<td class="num">'+(t.hold_days??'–')+'</td>' : '')
      + '<td>'+(RN[t.exit_reason]||t.exit_reason||'–')+'</td>'
      + '<td class="num neg">'+(t.mae_pct!=null ? t.mae_pct+'%' : '–')+'</td>'
      + '<td class="num pos">'+(t.mfe_pct!=null ? '+'+t.mfe_pct+'%' : '–')+'</td>'
      + '<td class="num '+(t.ret_net_pct>=0?'pos':'neg')+'">'+(t.ret_net_pct>=0?'+':'')+t.ret_net_pct+'%</td>'
      + '</tr>').join('');
    html += '</tbody></table></div>';
  }

  box.innerHTML = html;

  const tw = document.getElementById('bt-trades-wrap');
  if(tw){
    document.getElementById('bt-trades-toggle').addEventListener('click', ()=>{
      tw.style.display = tw.style.display==='none' ? 'block' : 'none';
    });
    document.getElementById('bt-trades-csv').addEventListener('click', ()=>{
      const hdr = ['#','日期','星期','進場時間','進場價','出場時間','出場價','持有天數','出場原因','MAE%','MFE%','淨報酬%'];
      const lines = [hdr.join(',')].concat(d.trades.map((t,i)=>[
        i+1, t.date, DOW_NAME[t.dow]||'', '"'+(t.entry_time||'')+'"', t.entry_price,
        '"'+(t.exit_time||'')+'"', t.exit_price, t.hold_days??'', t.exit_reason||'',
        t.mae_pct??'', t.mfe_pct??'', t.ret_net_pct
      ].join(',')));
      const blob = new Blob(['\\ufeff'+lines.join('\\n')], {type:'text/csv;charset=utf-8'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'backtest_trades.csv';
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }

  const mwrap = document.getElementById('bt-monthly-wrap');
  if(mwrap){
    document.getElementById('bt-monthly-toggle').addEventListener('click', ()=>{
      mwrap.style.display = mwrap.style.display==='none' ? 'block' : 'none';
    });
  }

  if(d.price_series && d.price_series.length){
    const labels = d.price_series.map(p=>p.date);
    const idxOf = new Map(labels.map((dt,i)=>[dt,i]));
    const entryArr = new Array(labels.length).fill(null);
    const exitArr  = new Array(labels.length).fill(null);
    d.trades.forEach(t=>{
      const ei = idxOf.get(t.date);
      if(ei!=null) entryArr[ei] = t.entry_price;
      // 出場日期從 exit_time 前10碼取(格式固定為 YYYY-MM-DD ...)
      const xd = (t.exit_time||'').slice(0,10);
      const xi = idxOf.get(xd);
      if(xi!=null) exitArr[xi] = t.exit_price;
    });
    const isLong = d.direction !== 'short';
    mk('bt-marker-chart', {type:'line', data:{labels, datasets:[
      {label:'加權指數', data:d.price_series.map(p=>p.close), borderColor:'#adb5bd',
       borderWidth:1, pointRadius:0, tension:.1, order:3},
      {label:'進場', data:entryArr, showLine:false, pointStyle:'triangle', pointRadius:6,
       pointRotation:0, backgroundColor: isLong?'#c0392b':'#27ae60',
       borderColor: isLong?'#c0392b':'#27ae60', order:1},
      {label:'出場', data:exitArr, showLine:false, pointStyle:'triangle', pointRadius:6,
       pointRotation:180, backgroundColor:'#4C72B0', borderColor:'#4C72B0', order:2},
    ]},
      options:{animation:false, plugins:{zoom:ZOOM, legend:{display:true}},
        interaction:{mode:'nearest', intersect:true},
        spanGaps:false,
        scales:{x:{ticks:{maxTicksLimit:12}}, y:{grace:'2%'}}}});
  }

  mk('bt-equity-chart', {type:'line', data:{labels:d.equity_curve.map(p=>p.date),
    datasets:[{data:d.equity_curve.map(p=>p.equity), borderColor:'#4C72B0', borderWidth:1.5,
      pointRadius:0, tension:.1}]},
    options:{animation:false, plugins:{legend:{display:false}, zoom:ZOOM},
      interaction:{mode:'index',intersect:false},
      scales:{x:{ticks:{maxTicksLimit:12}}, y:{grace:'5%'}}}});
}

btOnHoldToChange();
btOnModeChange();

function loadAll(){ loadSummary(); loadKline(); loadTaiex(); loadMargin(); loadNet(); loadTaifexOi(); loadTop();
  if(document.getElementById('sid').value.trim()) loadStock(); }
loadAll();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            result = api(u.path, parse_qs(u.query))
            if result is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/backtest":
            self.send_response(404); self.end_headers(); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            rule = json.loads(self.rfile.read(length) or b"{}")
            conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            try:
                result = backtest_engine.run_backtest(conn, rule)
            finally:
                conn.close()
        except Exception as e:
            result = {"error": f"回測執行失敗: {e}"}
        body = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not DB.exists():
        raise SystemExit(f"找不到 {DB},請先執行 collector.py 或 backfill.py")

    server = None
    port = PORT
    for attempt in range(10):
        try:
            server = HTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as e:
            if e.errno == 48 or "Address already in use" in str(e):
                port += 1
                continue
            raise
    if server is None:
        raise SystemExit(
            f"連續 10 個埠({PORT}~{port-1})都被占用。多半是之前開的 dashboard.py 沒關掉——"
            f"可以在終端機執行 `lsof -ti:{PORT} | xargs kill -9` 把舊的殺掉,再重新啟動這支程式。"
        )
    if port != PORT:
        print(f"預設埠 {PORT} 已被占用(通常是還有一個舊的 dashboard.py 在跑),改用 {port}。")

    url = f"http://localhost:{port}"
    print(f"儀表板啟動:{url}(Ctrl+C 結束)")
    webbrowser.open(url)
    server.serve_forever()
