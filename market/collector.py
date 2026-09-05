#!/usr/bin/env python3
"""台股資料收集器
- 每小時:抓取台灣加權指數 (TAIEX) 即時值
- 每天:抓取證交所 T86 個股三大法人(外資／投信／自營商)買賣超

用法:
  python -m market.collector index    # 抓一次大盤指數
  python -m market.collector foreign  # 抓一次 T86 (外資+投信+自營商, 預設最近交易日)
  python -m market.collector foreign 20260731  # 抓指定日期
  python -m market.collector run      # 常駐執行,自動排程 (需 pip install schedule)
"""
import sys
import time
import sqlite3
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import NamedTuple

import requests

from data.paths import repo_file
from data.sqlite_util import configure_local
from web.tw_calendar import is_tw_trading_day, taiwan_today

DB_PATH = repo_file("twse_data.db")
# MI_INDEX listed-only is ~1,300–1,400 names; listed+OTC is ~2,300.
# Below this we treat a day as missing 上櫃 and fetch TPEX again.
MIN_COMBINED_STOCK_DAILY = 1800

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(repo_file("collector.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (data-collector; personal use)"}


# ---------- DB ----------

def init_db(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS taiex_hourly (
        ts          TEXT PRIMARY KEY,   -- 抓取時間 ISO8601
        trade_date  TEXT,               -- 交易日 YYYY-MM-DD
        index_value REAL,               -- 指數
        change      REAL,               -- 漲跌點數
        volume_100m REAL                -- 成交金額(億元)
    );
    CREATE TABLE IF NOT EXISTS foreign_daily (
        trade_date  TEXT,               -- 交易日 YYYY-MM-DD
        stock_id    TEXT,               -- 股票代號
        stock_name  TEXT,
        foreign_buy   INTEGER,          -- 外資買進股數(不含自營)
        foreign_sell  INTEGER,          -- 外資賣出股數
        foreign_net   INTEGER,          -- 外資買賣超股數
        PRIMARY KEY (trade_date, stock_id)
    );
    CREATE INDEX IF NOT EXISTS idx_foreign_stock ON foreign_daily(stock_id);
    CREATE TABLE IF NOT EXISTS trust_daily (
        trade_date  TEXT,               -- 交易日 YYYY-MM-DD
        stock_id    TEXT,
        stock_name  TEXT,
        trust_buy   INTEGER,            -- 投信買進股數
        trust_sell  INTEGER,            -- 投信賣出股數
        trust_net   INTEGER,            -- 投信買賣超股數
        PRIMARY KEY (trade_date, stock_id)
    );
    CREATE INDEX IF NOT EXISTS idx_trust_stock ON trust_daily(stock_id);
    -- 自營商 = T86「自營商買賣超股數」(合計含避險 = 自行買賣 + 避險)。
    -- 這是三大法人合計的自營商分量,也是籌碼分析慣用的「自營商」淨額。
    -- 不是「自行買賣」單欄。上市 T86 only;上櫃不在 foreign_daily,本表也不收 OTC。
    CREATE TABLE IF NOT EXISTS dealer_daily (
        trade_date  TEXT,               -- 交易日 YYYY-MM-DD
        stock_id    TEXT,
        stock_name  TEXT,
        dealer_buy  INTEGER,            -- 自營商買進股數(自行買賣+避險)
        dealer_sell INTEGER,            -- 自營商賣出股數(自行買賣+避險)
        dealer_net  INTEGER,            -- 自營商買賣超股數(合計含避險)
        PRIMARY KEY (trade_date, stock_id)
    );
    CREATE INDEX IF NOT EXISTS idx_dealer_stock ON dealer_daily(stock_id);
    CREATE TABLE IF NOT EXISTS taiex_daily (
        trade_date TEXT PRIMARY KEY,    -- YYYY-MM-DD
        open  REAL,
        high  REAL,
        low   REAL,
        close REAL
    );
    CREATE TABLE IF NOT EXISTS taiex_hourly_ohlc (
        ts    TEXT PRIMARY KEY,         -- 該小時起點 YYYY-MM-DDTHH:00:00
        trade_date TEXT,
        open  REAL,
        high  REAL,
        low   REAL,
        close REAL
    );
    CREATE TABLE IF NOT EXISTS margin_total (      -- 整體信用交易統計
        trade_date   TEXT,
        item         TEXT,   -- 融資(交易單位) / 融券(交易單位) / 融資金額(仟元)
        buy          INTEGER,
        sell         INTEGER,
        redeem       INTEGER, -- 現金(券)償還
        prev_balance INTEGER,
        balance      INTEGER, -- 今日餘額
        PRIMARY KEY (trade_date, item)
    );
    CREATE TABLE IF NOT EXISTS margin_stock (      -- 個股融資融券(單位:張)
        trade_date     TEXT,
        stock_id       TEXT,
        stock_name     TEXT,
        margin_buy     INTEGER,  -- 融資買進
        margin_sell    INTEGER,  -- 融資賣出
        margin_balance INTEGER,  -- 融資今日餘額
        short_buy      INTEGER,  -- 融券買進
        short_sell     INTEGER,  -- 融券賣出
        short_balance  INTEGER,  -- 融券今日餘額
        PRIMARY KEY (trade_date, stock_id)
    );
    CREATE INDEX IF NOT EXISTS idx_margin_stock ON margin_stock(stock_id);
    CREATE TABLE IF NOT EXISTS stock_daily (       -- 個股日K
        trade_date TEXT,
        stock_id   TEXT,
        stock_name TEXT,
        open   REAL,
        high   REAL,
        low    REAL,
        close  REAL,
        volume INTEGER,   -- 成交股數
        turnover INTEGER, -- 成交金額(元)
        PRIMARY KEY (trade_date, stock_id)
    );
    CREATE INDEX IF NOT EXISTS idx_stock_daily ON stock_daily(stock_id);
    CREATE TABLE IF NOT EXISTS stocks (            -- 個股主檔
        stock_id   TEXT PRIMARY KEY,
        stock_name TEXT,
        last_seen  TEXT   -- 最後出現在資料中的交易日
    );
    -- 分點買賣超日彙總（#54 / #61）。無 FINMIND_TOKEN 不寫 live 列。
    -- 不存價位明細。見 docs/broker_branch.md。
    CREATE TABLE IF NOT EXISTS broker_branch_daily (
        trade_date  TEXT,
        stock_id    TEXT,
        broker_id   TEXT,
        buy_volume  INTEGER,
        sell_volume INTEGER,
        net_volume  INTEGER,
        PRIMARY KEY (trade_date, stock_id, broker_id)
    );
    CREATE TABLE IF NOT EXISTS brokers (
        broker_id   TEXT PRIMARY KEY,
        broker_name TEXT
    );
    CREATE TABLE IF NOT EXISTS broker_branch_meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_broker_branch_date_net
        ON broker_branch_daily(trade_date, net_volume);
    CREATE INDEX IF NOT EXISTS idx_broker_branch_stock_date
        ON broker_branch_daily(stock_id, trade_date);
    CREATE INDEX IF NOT EXISTS idx_broker_branch_broker_date
        ON broker_branch_daily(broker_id, trade_date);
    CREATE TABLE IF NOT EXISTS taiex_5sec_open (   -- 開盤時段每5秒指數 (09:00-10:05)
        trade_date TEXT,
        t          TEXT,   -- HH:MM:SS
        index_value REAL,
        PRIMARY KEY (trade_date, t)
    );
    -- Covering (id, date) indexes for dashboard per-stock range scans.
    -- New names so Turso CREATE INDEX IF NOT EXISTS can add them beside the old ones.
    CREATE INDEX IF NOT EXISTS idx_foreign_stock_date ON foreign_daily(stock_id, trade_date);
    CREATE INDEX IF NOT EXISTS idx_trust_stock_date ON trust_daily(stock_id, trade_date);
    CREATE INDEX IF NOT EXISTS idx_dealer_stock_date ON dealer_daily(stock_id, trade_date);
    CREATE INDEX IF NOT EXISTS idx_margin_stock_date ON margin_stock(stock_id, trade_date);
    CREATE INDEX IF NOT EXISTS idx_stock_daily_id_date ON stock_daily(stock_id, trade_date);
    CREATE INDEX IF NOT EXISTS idx_taiex_hourly_trade_date ON taiex_hourly(trade_date);
    CREATE INDEX IF NOT EXISTS idx_taiex_hourly_ohlc_trade_date ON taiex_hourly_ohlc(trade_date);
    -- Scanner wide view (#77): price/volume from stock_daily LEFT JOIN T86 chips.
    -- Always current; no refresh. Contract: docs/stock_chips_daily.md
    DROP VIEW IF EXISTS stock_chips_daily;
    CREATE VIEW stock_chips_daily AS
    SELECT
        s.trade_date,
        s.stock_id,
        s.stock_name,
        s.open,
        s.high,
        s.low,
        s.close,
        s.volume,
        s.turnover,
        f.foreign_buy,
        f.foreign_sell,
        f.foreign_net,
        t.trust_buy,
        t.trust_sell,
        t.trust_net,
        d.dealer_buy,
        d.dealer_sell,
        d.dealer_net
    FROM stock_daily AS s
    LEFT JOIN foreign_daily AS f
        ON f.trade_date = s.trade_date AND f.stock_id = s.stock_id
    LEFT JOIN trust_daily AS t
        ON t.trade_date = s.trade_date AND t.stock_id = s.stock_id
    LEFT JOIN dealer_daily AS d
        ON d.trade_date = s.trade_date AND d.stock_id = s.stock_id;
    """)
    conn.commit()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    configure_local(conn)
    init_db(conn)
    return conn


# ---------- 大盤指數 (每小時) ----------

def fetch_taiex() -> dict | None:
    """從證交所即時行情 API 抓加權指數 (tse_t00)。"""
    url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
    params = {"ex_ch": "tse_t00.tw", "json": "1", "delay": "0"}
    r = requests.get(url, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    arr = data.get("msgArray") or []
    if not arr:
        return None
    m = arr[0]
    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    value = f(m.get("z")) or f(m.get("y"))  # z=最新, 盤後可能為 '-' 改用 y=昨收
    prev = f(m.get("y"))
    return {
        "index_value": value,
        "change": round(value - prev, 2) if value and prev else None,
        "volume_100m": f(m.get("v")),
        "trade_date": m.get("d"),  # YYYYMMDD
    }


def save_taiex():
    info = fetch_taiex()
    if not info or info["index_value"] is None:
        log.warning("抓不到大盤指數 (可能非交易時間)")
        return
    d = info["trade_date"]
    trade_date = f"{d[:4]}-{d[4:6]}-{d[6:]}" if d and len(d) == 8 else None
    # ts 對齊整點,與 backfill 寫入的格式一致,避免同一小時出現兩筆
    ts = datetime.now().replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO taiex_hourly VALUES (?,?,?,?,?)",
            (ts, trade_date, info["index_value"], info["change"], info["volume_100m"]),
        )
    log.info("TAIEX %s 漲跌 %s", info["index_value"], info["change"])


# ---------- 個股主檔 ----------

def update_stock_master(conn: sqlite3.Connection, pairs, trade_date: str):
    """用 (stock_id, stock_name) 更新主檔;只在資料較新時覆蓋。"""
    conn.executemany("""
        INSERT INTO stocks (stock_id, stock_name, last_seen) VALUES (?,?,?)
        ON CONFLICT(stock_id) DO UPDATE SET
            stock_name = excluded.stock_name,
            last_seen  = excluded.last_seen
        WHERE excluded.last_seen >= stocks.last_seen
    """, [(sid, name, trade_date) for sid, name in pairs])


def sync_stock_master():
    """從既有資料表重建個股主檔(名稱取最近交易日的版本)。"""
    with get_conn() as conn:
        n0 = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        for table in ("foreign_daily", "margin_stock", "stock_daily"):
            conn.execute(f"""
                INSERT INTO stocks (stock_id, stock_name, last_seen)
                SELECT stock_id, stock_name, MAX(trade_date) FROM {table}
                GROUP BY stock_id
                ON CONFLICT(stock_id) DO UPDATE SET
                    stock_name = excluded.stock_name,
                    last_seen  = excluded.last_seen
                WHERE excluded.last_seen >= stocks.last_seen
            """)
        n1 = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
    log.info("個股主檔同步完成:%d 檔(原 %d 檔)", n1, n0)


# ---------- 大盤日 K (開高低收) ----------

def fetch_taiex_ohlc_month(day: date) -> list[tuple]:
    """MI_5MINS_HIST:回傳該月份每日開/高/低/收(日期為民國年)。"""
    url = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
    r = requests.get(url, params={"date": day.strftime("%Y%m%d"), "response": "json"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("stat") != "OK":
        return []
    rows = []
    for row in data.get("data", []):
        # row: [民國日期, 開盤, 最高, 最低, 收盤]
        try:
            y, m, d = row[0].split("/")
            trade_date = f"{int(y) + 1911}-{m}-{d}"
            vals = [float(str(v).replace(",", "")) for v in row[1:5]]
        except (ValueError, IndexError):
            continue
        rows.append((trade_date, *vals))
    return rows


def save_taiex_ohlc(day: date | None = None):
    """抓當月(或指定月份)的每日開高低收並寫入。"""
    rows = fetch_taiex_ohlc_month(day or taiwan_today())
    if not rows:
        log.warning("抓不到日 K 資料")
        return
    with get_conn() as conn:
        conn.executemany("INSERT OR REPLACE INTO taiex_daily VALUES (?,?,?,?,?)", rows)
    log.info("日 K %s ~ %s 共 %d 筆已存入", rows[0][0], rows[-1][0], len(rows))


# ---------- 大盤小時 K (由每5秒資料彙整) ----------

def fetch_index_5sec(day: date) -> list[tuple]:
    """MI_5MINS_INDEX:該日盤中每5秒的加權指數,回傳 [(時間, 指數), ...]。"""
    url = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_INDEX"
    r = requests.get(url, params={"date": day.strftime("%Y%m%d"), "response": "json"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("stat") != "OK" or not data.get("data"):
        return []
    out = []
    for row in data["data"]:
        try:
            out.append((row[0], float(str(row[1]).replace(",", ""))))
        except (ValueError, IndexError):
            continue
    return out


def hourly_ohlc_from_5sec(day: date, rows: list[tuple]) -> list[tuple]:
    """把每5秒資料彙整成每小時開高低收(13:00 那根含收盤 13:30)。

    注意:證交所 09:00:00 那筆是「昨日收盤指數」(9 點整個股尚未成交,
    指數還沒更新),必須丟棄,否則第一根 K 的 open/high/low 會被污染。
    """
    rows = [(t, v) for t, v in rows if t > "09:00:00"]
    buckets: dict[str, list[float]] = {}
    for t, v in rows:
        h = min(t[:2], "13")  # 13:30 收盤資料歸入 13:00 那根
        buckets.setdefault(h, []).append(v)
    d = day.isoformat()
    return [(f"{d}T{h}:00:00", d, vals[0], max(vals), min(vals), vals[-1])
            for h, vals in sorted(buckets.items())]


def save_open_5sec(conn: sqlite3.Connection, day: date, raw: list[tuple]):
    """把 09:00-10:05 的每5秒原始值存入(供開盤動態分析;09:00:00 昨收那筆一併保留,分析時自行排除)。"""
    d = day.isoformat()
    rows = [(d, t, v) for t, v in raw if t <= "10:05:00"]
    if rows:
        conn.executemany("INSERT OR REPLACE INTO taiex_5sec_open VALUES (?,?,?)", rows)


def save_hourly_ohlc(day: date | None = None):
    day = day or taiwan_today()
    raw = fetch_index_5sec(day)
    rows = hourly_ohlc_from_5sec(day, raw)
    if not rows:
        log.warning("抓不到小時 K 資料(可能非交易日)")
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO taiex_hourly_ohlc VALUES (?,?,?,?,?,?)", rows)
        save_open_5sec(conn, day, raw)
    log.info("小時 K %s 共 %d 根已存入(含開盤5秒資料)", day, len(rows))


# ---------- 三大法人買賣超 (每天,一次 T86) ----------
#
# T86 欄位(2024+ / 2026-09-04 實測 19 欄):
#   0 證券代號
#   1 證券名稱
#   2-4  外陸資買/賣/超(不含外資自營商)  ← foreign_daily
#   5-7  外資自營商買/賣/超              ← 不另存;三大法人合計已含,且 foreign 沿用舊定義
#   8-10 投信買/賣/超                    ← trust_daily
#   11   自營商買賣超股數                ← dealer_daily.net (合計含避險)
#   12-14 自營商自行買賣 買/賣/超
#   15-17 自營商避險 買/賣/超
#   18   三大法人買賣超股數
#
# 自營商定義:合計含避險。T86 把「自營商買賣超股數」獨立成第 11 欄,
# 且三大法人合計 = 外資(不含自營) + 外資自營商 + 投信 + 此欄。
# 籌碼分析慣用的「自營商」就是這個合計,不是「自行買賣」單欄。
# dealer_buy / dealer_sell = 自行買賣 + 避險,使 buy - sell = net。
#
# 範圍:上市 T86 only。foreign_daily 本來就不含上櫃;櫃買雖有
# 3itrade_hedge JSON,這裡不鏡射,避免跟外資覆蓋範圍不一致。

class T86Tables(NamedTuple):
    foreign: list[tuple]
    trust: list[tuple]
    dealer: list[tuple]


EMPTY_T86 = T86Tables([], [], [])

# Fallback indexes when the payload has no fields[] (or a short test fixture).
_T86_IDX = {
    "foreign_buy": 2, "foreign_sell": 3, "foreign_net": 4,
    "trust_buy": 8, "trust_sell": 9, "trust_net": 10,
    "dealer_net": 11,
    "dealer_prop_buy": 12, "dealer_prop_sell": 13,
    "dealer_hedge_buy": 15, "dealer_hedge_sell": 16,
}


def _t86_int(v) -> int:
    try:
        return int(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return 0


def _t86_col(row: list, idx: int | None) -> int:
    if idx is None or idx < 0 or idx >= len(row):
        return 0
    return _t86_int(row[idx])


def _t86_colmap(fields: list | None) -> dict[str, int]:
    """Map T86 logical columns. Prefer fields[] labels; else the 19-col layout."""
    if not fields:
        return dict(_T86_IDX)
    labels = [str(f).strip() for f in fields]

    def find(*needles: str, exclude: tuple[str, ...] = ()) -> int | None:
        for i, label in enumerate(labels):
            if any(x in label for x in exclude):
                continue
            if all(n in label for n in needles):
                return i
        return None

    mapped = {
        "foreign_buy": find("外陸資買進") or find("外資買進"),
        "foreign_sell": find("外陸資賣出") or find("外資賣出"),
        "foreign_net": find("外陸資買賣超") or find("外資買賣超"),
        "trust_buy": find("投信買進"),
        "trust_sell": find("投信賣出"),
        "trust_net": find("投信買賣超"),
        # 「自營商買賣超股數」合計欄. Must not pick 外資自營商 or 自行買賣/避險.
        "dealer_net": find("自營商買賣超", exclude=("自行", "避險", "外資", "外陸")),
        "dealer_prop_buy": find("自營商買進", "自行"),
        "dealer_prop_sell": find("自營商賣出", "自行"),
        "dealer_hedge_buy": find("自營商買進", "避險"),
        "dealer_hedge_sell": find("自營商賣出", "避險"),
    }
    out = dict(_T86_IDX)
    for key, idx in mapped.items():
        if idx is not None:
            out[key] = idx
    return out


def parse_t86(data: dict, day: date) -> T86Tables:
    """Split one T86 JSON payload into foreign / trust / dealer row lists."""
    if data.get("stat") != "OK":
        return EMPTY_T86
    cmap = _t86_colmap(data.get("fields"))
    trade_date = day.isoformat()
    foreign, trust, dealer = [], [], []
    for row in data.get("data") or []:
        if len(row) < 5:
            continue
        sid = str(row[0]).strip()
        name = str(row[1]).strip()
        foreign.append((
            trade_date, sid, name,
            _t86_col(row, cmap["foreign_buy"]),
            _t86_col(row, cmap["foreign_sell"]),
            _t86_col(row, cmap["foreign_net"]),
        ))
        trust.append((
            trade_date, sid, name,
            _t86_col(row, cmap["trust_buy"]),
            _t86_col(row, cmap["trust_sell"]),
            _t86_col(row, cmap["trust_net"]),
        ))
        prop_buy = _t86_col(row, cmap["dealer_prop_buy"])
        prop_sell = _t86_col(row, cmap["dealer_prop_sell"])
        hedge_buy = _t86_col(row, cmap["dealer_hedge_buy"])
        hedge_sell = _t86_col(row, cmap["dealer_hedge_sell"])
        dealer.append((
            trade_date, sid, name,
            prop_buy + hedge_buy,
            prop_sell + hedge_sell,
            _t86_col(row, cmap["dealer_net"]),
        ))
    return T86Tables(foreign, trust, dealer)


def fetch_t86(day: date) -> T86Tables:
    """One HTTP call: TWSE T86 → foreign + trust + dealer rows."""
    url = "https://www.twse.com.tw/rwd/zh/fund/T86"
    params = {
        "date": day.strftime("%Y%m%d"),
        "selectType": "ALLBUT0999",  # 全部(不含權證等)
        "response": "json",
    }
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return parse_t86(r.json(), day)


def fetch_foreign(day: date) -> list[tuple]:
    """證交所 T86:個股三大法人買賣超,取外資(不含外資自營商)欄位。"""
    return fetch_t86(day).foreign


def persist_t86(conn: sqlite3.Connection, tables: T86Tables) -> int:
    """Write all three T86 tables from one parsed day. Returns foreign row count."""
    if tables.foreign:
        conn.executemany(
            "INSERT OR REPLACE INTO foreign_daily VALUES (?,?,?,?,?,?)", tables.foreign)
        update_stock_master(conn, [(r[1], r[2]) for r in tables.foreign], tables.foreign[0][0])
    if tables.trust:
        conn.executemany(
            "INSERT OR REPLACE INTO trust_daily VALUES (?,?,?,?,?,?)", tables.trust)
    if tables.dealer:
        conn.executemany(
            "INSERT OR REPLACE INTO dealer_daily VALUES (?,?,?,?,?,?)", tables.dealer)
    return len(tables.foreign)


def save_foreign(day: date | None = None):
    """抓指定日 T86(外資+投信+自營商);無資料則往前找最近交易日(最多 7 天)。"""
    day = day or taiwan_today()
    for _ in range(7):
        tables = fetch_t86(day)
        if tables.foreign:
            with get_conn() as conn:
                persist_t86(conn, tables)
            log.info(
                "T86 %s:外資 %d 檔、投信 %d 檔、自營商 %d 檔已存入",
                day, len(tables.foreign), len(tables.trust), len(tables.dealer),
            )
            return
        log.info("%s 無資料,往前一天找", day)
        day -= timedelta(days=1)
        time.sleep(3)  # 對證交所禮貌性間隔
    log.warning("最近 7 天都抓不到 T86 資料")


# ---------- 融資融券 (每天) ----------

def _n(v):
    try:
        return int(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def fetch_margin(day: date) -> tuple[list, list]:
    """MI_MARGN:回傳 (整體統計 rows, 個股 rows)。"""
    url = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
    params = {"date": day.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"}
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("stat") != "OK":
        return [], []
    # 新版 rwd 回傳 tables:[{fields,data},...];舊版是 creditList / data
    total_rows, stock_rows = [], []
    if "tables" in data:
        for t in data["tables"]:
            fields = "".join(t.get("fields", []))
            if "代號" in fields or "證券" in fields:
                stock_rows = t.get("data", [])
            elif "項目" in fields or any("融資" in str(r0[0]) for r0 in t.get("data", [])[:1]):
                total_rows = t.get("data", [])
    else:
        total_rows = data.get("creditList", [])
        stock_rows = data.get("data", [])
    d = day.isoformat()
    totals = [(d, str(row[0]).strip(), _n(row[1]), _n(row[2]), _n(row[3]),
               _n(row[4]), _n(row[5]))
              for row in total_rows if len(row) >= 6]
    # 個股欄位: 0代號 1名稱 | 融資: 2買進 3賣出 4現金償還 5前日餘額 6今日餘額 7限額
    #          | 融券: 8買進 9賣出 10現券償還 11前日餘額 12今日餘額 13限額 | 14資券互抵 15註記
    stocks = [(d, str(row[0]).strip(), str(row[1]).strip(),
               _n(row[2]), _n(row[3]), _n(row[6]),
               _n(row[8]), _n(row[9]), _n(row[12]))
              for row in stock_rows if len(row) >= 13]
    return totals, stocks


def save_margin(day: date | None = None):
    """抓指定日(預設今天);無資料則往前找最近交易日(最多 7 天)。"""
    day = day or taiwan_today()
    for _ in range(7):
        totals, stocks = fetch_margin(day)
        if stocks:
            with get_conn() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO margin_total VALUES (?,?,?,?,?,?,?)", totals)
                conn.executemany(
                    "INSERT OR REPLACE INTO margin_stock VALUES (?,?,?,?,?,?,?,?,?)", stocks)
                update_stock_master(conn, [(r[1], r[2]) for r in stocks], stocks[0][0])
            log.info("融資融券 %s:整體 %d 項、個股 %d 檔已存入", day, len(totals), len(stocks))
            return
        log.info("%s 無融資融券資料,往前一天找", day)
        day -= timedelta(days=1)
        time.sleep(3)
    log.warning("最近 7 天都抓不到融資融券資料")


# ---------- 個股日 K ----------

def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def roc_date(day: date) -> str:
    return f"{day.year - 1911}/{day.month:02d}/{day.day:02d}"


def fetch_stock_day_all(day: date) -> list[tuple]:
    """MI_INDEX(全市場收盤行情):一次請求取得所有個股當日開高低收量。"""
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    params = {"date": day.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"}
    r = requests.get(url, params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("stat") != "OK":
        return []
    stock_rows = []
    for t in data.get("tables", []):
        fields = t.get("fields", [])
        if "證券代號" in fields and "收盤價" in fields:
            stock_rows = t.get("data", [])
            break
    d = day.isoformat()
    out = []
    for row in stock_rows:
        # 0代號 1名稱 2成交股數 3成交筆數 4成交金額 5開 6高 7低 8收 ...
        if len(row) < 9:
            continue
        out.append((d, str(row[0]).strip(), str(row[1]).strip(),
                    _f(row[5]), _f(row[6]), _f(row[7]), _f(row[8]),
                    _n(row[2]), _n(row[4])))
    return out


def fetch_tpex_stock_day_all(day: date) -> list[tuple]:
    """櫃買每日收盤行情。回傳格式與 fetch_stock_day_all 相同。"""
    url = "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php"
    params = {"l": "zh-tw", "d": roc_date(day), "se": "EW"}
    r = requests.get(url, params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    data = r.json()
    stock_rows = []
    for table in data.get("tables") or []:
        fields = [str(f).strip() for f in table.get("fields", [])]
        if "代號" in fields and any(f.startswith("收盤") for f in fields):
            stock_rows = table.get("data") or []
            break
    d = day.isoformat()
    out = []
    for row in stock_rows:
        # 0代號 1名稱 2收盤 3漲跌 4開 5高 6低 7成交股數 8成交金額
        if len(row) < 8:
            continue
        out.append((
            d,
            str(row[0]).strip(),
            str(row[1]).strip(),
            _f(row[4]),
            _f(row[5]),
            _f(row[6]),
            _f(row[2]),
            _n(row[7]),
            _n(row[8]) if len(row) > 8 else None,
        ))
    return out


def persist_stock_daily(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Write listed and/or OTC rows into the existing stock_daily table.

    Same 9-column schema as today — no new table or ALTER. TWSE and TPEX
    stock_ids do not overlap, so both markets share (trade_date, stock_id).
    """
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    update_stock_master(conn, [(r[1], r[2]) for r in rows], rows[0][0])
    return len(rows)


def load_stock_daily_bars(
    day: date,
    conn: sqlite3.Connection | None = None,
) -> dict[str, tuple]:
    """stock_id -> persisted official bar, if twse_data.db already has that day."""
    own = False
    if conn is None:
        if not Path(DB_PATH).exists():
            return {}
        conn = get_conn()
        own = True
    try:
        try:
            rows = conn.execute(
                "SELECT trade_date, stock_id, stock_name, open, high, low, close, "
                "volume, turnover FROM stock_daily WHERE trade_date = ?",
                (day.isoformat(),),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {
            str(row[1]).strip(): tuple(row)
            for row in rows
            if row[6] is not None
        }
    finally:
        if own:
            conn.close()


def official_session_bars(
    day: date,
    *,
    fetch_twse=None,
    fetch_tpex=None,
    use_db: bool = True,
    min_cached: int = MIN_COMBINED_STOCK_DAILY,
) -> dict[str, tuple]:
    """stock_id -> official (date, id, name, open, high, low, close, volume, turnover).

    Prefers persisted stock_daily when the 18:00 catch-up already stored
    listed+OTC. Otherwise live-fetches TWSE MI_INDEX + TPEX quotes — the same
    path the screener uses to fill Yahoo Close=NaN. Does not write; writers
    are save_stock_day_all / backfill_stock_daily.
    """
    live_overrides = fetch_twse is not None or fetch_tpex is not None
    if use_db and not live_overrides:
        cached = load_stock_daily_bars(day)
        if len(cached) >= min_cached:
            log.info("official bars %s from stock_daily (%d names)", day, len(cached))
            return cached
    twse = fetch_twse or fetch_stock_day_all
    tpex = fetch_tpex or fetch_tpex_stock_day_all
    out: dict[str, tuple] = {}
    for fetch in (twse, tpex):
        try:
            rows = fetch(day)
        except Exception as exc:
            log.warning("official bars %s failed: %s", day, exc)
            continue
        for row in rows:
            if row[6] is None:
                continue
            out[str(row[1]).strip()] = row
    return out


def save_stock_day_all(day: date | None = None):
    """抓指定日(預設今天)上市+上櫃個股日K;無資料則往前找(最多 7 天)。"""
    day = day or taiwan_today()
    for _ in range(7):
        twse_err = tpex_err = None
        try:
            twse_rows = fetch_stock_day_all(day)
        except Exception as exc:
            twse_err = exc
            twse_rows = []
            log.warning("%s 上市日K失敗: %s", day, exc)
        try:
            tpex_rows = fetch_tpex_stock_day_all(day)
        except Exception as exc:
            tpex_err = exc
            tpex_rows = []
            log.warning("%s 上櫃日K失敗: %s", day, exc)
        rows = twse_rows + tpex_rows
        if rows:
            with get_conn() as conn:
                persist_stock_daily(conn, rows)
            log.info(
                "個股日K %s:上市 %d 檔、上櫃 %d 檔已存入",
                day, len(twse_rows), len(tpex_rows),
            )
            if twse_err:
                raise RuntimeError(f"{day.isoformat()} TWSE fetch failed: {twse_err}")
            if tpex_err:
                raise RuntimeError(f"{day.isoformat()} TPEX fetch failed: {tpex_err}")
            if twse_rows and not tpex_rows:
                raise RuntimeError(f"{day.isoformat()} TPEX empty on TWSE trading day")
            return
        log.info("%s 無個股日K資料,往前一天找", day)
        day -= timedelta(days=1)
        time.sleep(3)
    log.warning("最近 7 天都抓不到個股日K")


def fetch_stock_month(stock_id: str, day: date) -> list[tuple]:
    """STOCK_DAY:單一個股該月份每日開高低收量(用於歷史回補)。"""
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
    params = {"date": day.strftime("%Y%m%d"), "stockNo": stock_id, "response": "json"}
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("stat") != "OK":
        return []
    # title 格式如 "115年07月 2330 台積電 各日成交資訊"
    parts = str(data.get("title", "")).split()
    name = parts[2] if len(parts) >= 3 else ""
    out = []
    for row in data.get("data", []):
        # 0日期(民國) 1成交股數 2成交金額 3開 4高 5低 6收 7漲跌價差 8成交筆數
        try:
            y, m, dd = str(row[0]).split("/")
            trade_date = f"{int(y) + 1911}-{m}-{dd}"
        except ValueError:
            continue
        out.append((trade_date, stock_id, name,
                    _f(row[3]), _f(row[4]), _f(row[5]), _f(row[6]),
                    _n(row[1]), _n(row[2])))
    return out


# ---------- 排程 ----------

def run_scheduler():
    import schedule  # pip install schedule

    def hourly_index():
        now = datetime.now()
        # 只在交易日 09:00–13:30 抓 (13 點那次抓收盤前資料,14 點抓收盤值)
        if is_tw_trading_day(now.date()) and 9 <= now.hour <= 14:
            try:
                save_taiex()
            except Exception:
                log.exception("抓大盤失敗")

    def daily_foreign():
        if is_tw_trading_day(datetime.now().date()):
            try:
                save_foreign()
            except Exception:
                log.exception("抓外資失敗")
            try:
                time.sleep(4)
                save_taiex_ohlc()
            except Exception:
                log.exception("抓日 K 失敗")
            try:
                time.sleep(4)
                save_hourly_ohlc()
            except Exception:
                log.exception("抓小時 K 失敗")
            try:
                time.sleep(4)
                save_margin()
            except Exception:
                log.exception("抓融資融券失敗")
            try:
                time.sleep(4)
                save_stock_day_all()
            except Exception:
                log.exception("抓個股日K失敗")

    schedule.every().hour.at(":05").do(hourly_index)
    schedule.every().day.at("17:30").do(daily_foreign)  # T86 約 16:00 後公布
    log.info("排程啟動:每小時抓指數(交易時段)、每天 17:30 抓外資買賣超")
    hourly_index()
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "index":
        save_taiex()
    elif cmd == "ohlc":
        save_taiex_ohlc()
    elif cmd == "hourly":
        save_hourly_ohlc()
    elif cmd == "margin":
        d = (datetime.strptime(sys.argv[2], "%Y%m%d").date()
             if len(sys.argv) > 2 else None)
        save_margin(d)
    elif cmd == "stocks":
        d = (datetime.strptime(sys.argv[2], "%Y%m%d").date()
             if len(sys.argv) > 2 else None)
        save_stock_day_all(d)
    elif cmd == "sync":
        sync_stock_master()
    elif cmd == "foreign":
        d = (datetime.strptime(sys.argv[2], "%Y%m%d").date()
             if len(sys.argv) > 2 else None)
        save_foreign(d)
    elif cmd == "run":
        run_scheduler()
    else:
        print(__doc__)
