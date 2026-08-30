#!/usr/bin/env python3
"""台股資料收集器
- 每小時:抓取台灣加權指數 (TAIEX) 即時值
- 每天:抓取證交所 T86 個股外資買賣超

用法:
  python collector.py index    # 抓一次大盤指數
  python collector.py foreign  # 抓一次外資買賣超 (預設抓最近交易日)
  python collector.py foreign 20260731  # 抓指定日期
  python collector.py run      # 常駐執行,自動排程 (需 pip install schedule)
"""
import sys
import time
import sqlite3
import logging
from datetime import datetime, date, timedelta
from pathlib import Path

import requests

DB_PATH = Path(__file__).parent / "twse_data.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "collector.log", encoding="utf-8"),
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
    CREATE TABLE IF NOT EXISTS taiex_5sec_open (   -- 開盤時段每5秒指數 (09:00-10:05)
        trade_date TEXT,
        t          TEXT,   -- HH:MM:SS
        index_value REAL,
        PRIMARY KEY (trade_date, t)
    );
    """)
    conn.commit()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
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
    rows = fetch_taiex_ohlc_month(day or date.today())
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
    day = day or date.today()
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


# ---------- 外資買賣超 (每天) ----------

def fetch_foreign(day: date) -> list[tuple]:
    """證交所 T86:個股三大法人買賣超,取外資(不含外資自營商)欄位。"""
    url = "https://www.twse.com.tw/rwd/zh/fund/T86"
    params = {
        "date": day.strftime("%Y%m%d"),
        "selectType": "ALLBUT0999",  # 全部(不含權證等)
        "response": "json",
    }
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("stat") != "OK":
        return []  # 非交易日或資料未出
    rows = []
    trade_date = day.isoformat()
    for row in data.get("data", []):
        # 欄位: 0=代號 1=名稱 2=外資買進 3=外資賣出 4=外資買賣超 ...
        def n(v):
            try:
                return int(str(v).replace(",", ""))
            except ValueError:
                return 0
        rows.append((trade_date, row[0].strip(), row[1].strip(),
                     n(row[2]), n(row[3]), n(row[4])))
    return rows


def save_foreign(day: date | None = None):
    """抓指定日(預設今天);若無資料則往前找最近交易日(最多回溯 7 天)。"""
    day = day or date.today()
    for _ in range(7):
        rows = fetch_foreign(day)
        if rows:
            with get_conn() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO foreign_daily VALUES (?,?,?,?,?,?)", rows)
                update_stock_master(conn, [(r[1], r[2]) for r in rows], rows[0][0])
            log.info("外資買賣超 %s:%d 檔已存入", day, len(rows))
            return
        log.info("%s 無資料,往前一天找", day)
        day -= timedelta(days=1)
        time.sleep(3)  # 對證交所禮貌性間隔
    log.warning("最近 7 天都抓不到外資資料")


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
    day = day or date.today()
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


def save_stock_day_all(day: date | None = None):
    """抓指定日(預設今天)全市場個股日K;無資料則往前找(最多 7 天)。"""
    day = day or date.today()
    for _ in range(7):
        rows = fetch_stock_day_all(day)
        if rows:
            with get_conn() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)", rows)
                update_stock_master(conn, [(r[1], r[2]) for r in rows], rows[0][0])
            log.info("個股日K %s:%d 檔已存入", day, len(rows))
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
        if now.weekday() < 5 and 9 <= now.hour <= 14:
            try:
                save_taiex()
            except Exception:
                log.exception("抓大盤失敗")

    def daily_foreign():
        if datetime.now().weekday() < 5:
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
