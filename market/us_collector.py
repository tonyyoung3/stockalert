#!/usr/bin/env python3
"""美股 ETF 資料收集器(SPY / QQQ 等)

設計對應台股資料庫,方便跨市場比較:
  us_daily   ←→ taiex_daily      (日K,完整歷史)
  us_hourly  ←→ taiex_hourly_ohlc(小時K,yfinance 上限 730 天)
  us_minute  ←→ taiex_5sec_open  (1分K,yfinance 上限 7 天,需每週跑才能累積)

用法:
  python -m market.us_collector daily              # 抓日K(預設 SPY,QQQ,近15年)
  python -m market.us_collector daily SPY,QQQ,IWM 20
  python -m market.us_collector hourly             # 抓小時K(近730天,上限)
  python -m market.us_collector minute             # 抓1分K(近7天;要長期累積需定期執行)
  python -m market.us_collector all                # daily + hourly + minute

安裝: pip install yfinance

注意:
- 美股 09:30-16:00 ET,1小時K 每天 7 根(最後一根只有 30 分鐘)
- 預設不含盤前盤後(prepost=False),隔夜報酬 = 前收→今開,定義乾淨
- 1分K 只有 7 天滾動視窗,建議排程每週執行以長期累積
"""
import sys
import time
import sqlite3
import logging
from datetime import datetime

from data.paths import repo_file

DB_PATH = repo_file("us_data.db")
DEFAULT_TICKERS = ["SPY", "QQQ"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(repo_file("us_collector.log"), encoding="utf-8")],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------- DB

def init_db(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS us_daily (
        ticker TEXT, trade_date TEXT,
        open REAL, high REAL, low REAL, close REAL,
        adj_close REAL, volume INTEGER,
        PRIMARY KEY (ticker, trade_date)
    );
    CREATE TABLE IF NOT EXISTS us_hourly (
        ticker TEXT, ts TEXT,          -- ISO8601 美東時間
        trade_date TEXT,
        open REAL, high REAL, low REAL, close REAL, volume INTEGER,
        PRIMARY KEY (ticker, ts)
    );
    CREATE TABLE IF NOT EXISTS us_minute (
        ticker TEXT, ts TEXT,
        trade_date TEXT,
        open REAL, high REAL, low REAL, close REAL, volume INTEGER,
        PRIMARY KEY (ticker, ts)
    );
    CREATE INDEX IF NOT EXISTS idx_us_hourly_date ON us_hourly(ticker, trade_date);
    CREATE INDEX IF NOT EXISTS idx_us_minute_date ON us_minute(ticker, trade_date);
    """)
    conn.commit()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    return conn


# ---------------------------------------------------------------- 抓取

def fetch(ticker: str, period: str, interval: str):
    """回傳 yfinance DataFrame(欄位已攤平)。"""
    import yfinance as yf
    df = yf.download(ticker, period=period, interval=interval,
                     progress=False, auto_adjust=False, prepost=False)
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)   # 攤平 MultiIndex
    return df


def _rows_daily(ticker, df):
    out = []
    for idx, r in df.iterrows():
        d = idx.strftime("%Y-%m-%d")
        out.append((ticker, d,
                    _f(r.get("Open")), _f(r.get("High")), _f(r.get("Low")),
                    _f(r.get("Close")), _f(r.get("Adj Close")), _i(r.get("Volume"))))
    return out


def _rows_intraday(ticker, df):
    out = []
    for idx, r in df.iterrows():
        ts = idx.strftime("%Y-%m-%dT%H:%M:%S")
        out.append((ticker, ts, idx.strftime("%Y-%m-%d"),
                    _f(r.get("Open")), _f(r.get("High")), _f(r.get("Low")),
                    _f(r.get("Close")), _i(r.get("Volume"))))
    return out


def _f(v):
    try:
        f = float(v)
        return None if f != f else f     # NaN → None
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        f = float(v)
        return None if f != f else int(f)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 儲存

def save_daily(tickers, years=15):
    conn = get_conn()
    for t in tickers:
        try:
            df = fetch(t, f"{years}y", "1d")
            if df is None:
                log.warning("%s 日K 無資料", t); continue
            rows = _rows_daily(t, df)
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO us_daily VALUES (?,?,?,?,?,?,?,?)", rows)
            log.info("%s 日K %d 筆 (%s ~ %s)", t, len(rows), rows[0][1], rows[-1][1])
        except Exception:
            log.exception("%s 日K 失敗", t)
        time.sleep(1)
    conn.close()


def save_hourly(tickers, days=730):
    conn = get_conn()
    for t in tickers:
        try:
            df = fetch(t, f"{days}d", "1h")
            if df is None:
                log.warning("%s 小時K 無資料", t); continue
            rows = _rows_intraday(t, df)
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO us_hourly VALUES (?,?,?,?,?,?,?,?)", rows)
            log.info("%s 小時K %d 根 (%s ~ %s)", t, len(rows), rows[0][2], rows[-1][2])
        except Exception:
            log.exception("%s 小時K 失敗", t)
        time.sleep(1)
    conn.close()


def save_minute(tickers, days=7):
    """1分K 只有 7 天滾動視窗 —— 定期執行才能長期累積。"""
    conn = get_conn()
    for t in tickers:
        try:
            df = fetch(t, f"{days}d", "1m")
            if df is None:
                log.warning("%s 1分K 無資料", t); continue
            rows = _rows_intraday(t, df)
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO us_minute VALUES (?,?,?,?,?,?,?,?)", rows)
            n = conn.execute("SELECT COUNT(*) FROM us_minute WHERE ticker=?", (t,)).fetchone()[0]
            log.info("%s 1分K 本次 %d 筆,累計 %d 筆", t, len(rows), n)
        except Exception:
            log.exception("%s 1分K 失敗", t)
        time.sleep(1)
    conn.close()


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "all"
    tickers = args[1].split(",") if len(args) > 1 else DEFAULT_TICKERS
    n = int(args[2]) if len(args) > 2 else None

    if cmd == "daily":
        save_daily(tickers, n or 15)
    elif cmd == "hourly":
        save_hourly(tickers, n or 730)
    elif cmd == "minute":
        save_minute(tickers, n or 7)
    elif cmd == "all":
        save_daily(tickers, n or 15)
        save_hourly(tickers, 730)
        save_minute(tickers, 7)
    else:
        print(__doc__)
