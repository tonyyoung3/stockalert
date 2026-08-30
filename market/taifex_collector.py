#!/usr/bin/env python3
"""期交所(TAIFEX)三大法人未平倉資料收集器

兩張表:
  taifex_fut_oi  三大法人「區分各期貨契約」—— 台指期/小台/微台 等,自營/投信/外資 分計
  taifex_opt_oi  三大法人「選擇權買賣權分計」—— 台指選擇權 CALL/PUT 分開,自營/投信/外資 分計

用法:
  python -m market.taifex_collector test              # 抓最近 5 個交易日,印出來檢查欄位(不寫入)
  python -m market.taifex_collector recent            # 抓最近 30 天寫入 DB
  python -m market.taifex_collector backfill 730      # 回補近 730 天(逐月請求)
  python -m market.taifex_collector backfill 5500     # 回補約 15 年
  python -m market.taifex_collector summary           # 印出目前 DB 內容概況

⚠️ 資料深度限制(2026-08 實測):
   期交所這兩個下載端點只提供**近三年滾動視窗**的資料,再往前查一律回空字串
   (不是錯誤、不是空表,是完全空白的回應——所以會「跑完但一筆都沒有」)。
   實測邊界:2023/09/01 有資料、2023/08/01 無資料,即約當日往前推 3 年。
   要 2011-2014 的歷史資料,此來源做不到,需另尋付費/第三方來源。
   本程式已在 backfill() 自動裁切到可用範圍並提出警告。

注意:
- 期交所資料為 MS950(Big5)編碼,本程式已處理。
- 逐月分段請求,每次間隔 3 秒。支援中斷續跑(已有完整月份自動跳過)。
- 「投信」在台指期的未平倉通常極大且穩定(多為避險部位),解讀時需留意,
  它跟外資的「方向性押注」性質不同。
"""
import sys
import time
import sqlite3
import logging
from io import StringIO
from datetime import date, timedelta

import requests

from data.paths import repo_file

DB_PATH = repo_file("twse_data.db")
BASE = "https://www.taifex.com.tw/cht/3"
HEADERS = {"User-Agent": "Mozilla/5.0 (data-collector; personal use)"}
SLEEP = 3
# 期交所下載端點的滾動視窗深度(實測約 3 年,留 30 天緩衝)
MAX_LOOKBACK_DAYS = 365 * 3 - 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(repo_file("taifex_collector.log"),
                                  encoding="utf-8")],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------- DB

def init_db(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS taifex_fut_oi (
        trade_date   TEXT,
        product      TEXT,     -- 臺股期貨 / 小型臺指期貨 / 微型臺指期貨 ...
        investor     TEXT,     -- 自營商 / 投信 / 外資及陸資
        long_lots    INTEGER,  -- 當日交易:多方口數
        long_amt     INTEGER,  --           多方契約金額(千元)
        short_lots   INTEGER,  --           空方口數
        short_amt    INTEGER,
        net_lots     INTEGER,  --           多空淨額口數
        net_amt      INTEGER,
        oi_long_lots INTEGER,  -- 未平倉:多方口數
        oi_long_amt  INTEGER,
        oi_short_lots INTEGER, --         空方口數
        oi_short_amt INTEGER,
        oi_net_lots  INTEGER,  --         多空淨額口數  ← 最常用的欄位
        oi_net_amt   INTEGER,
        PRIMARY KEY (trade_date, product, investor)
    );
    CREATE TABLE IF NOT EXISTS taifex_opt_oi (
        trade_date   TEXT,
        product      TEXT,     -- 臺指選擇權 / 電子選擇權 / ...
        cp           TEXT,     -- CALL / PUT
        investor     TEXT,     -- 自營商 / 投信 / 外資及陸資
        buy_lots     INTEGER,  -- 當日交易:買方口數
        buy_amt      INTEGER,
        sell_lots    INTEGER,  --           賣方口數
        sell_amt     INTEGER,
        net_lots     INTEGER,  --           買賣差額口數
        net_amt      INTEGER,
        oi_buy_lots  INTEGER,  -- 未平倉:買方口數
        oi_buy_amt   INTEGER,
        oi_sell_lots INTEGER,  --         賣方口數
        oi_sell_amt  INTEGER,
        oi_net_lots  INTEGER,  --         未平倉差額口數 ← 最常用的欄位
        oi_net_amt   INTEGER,
        PRIMARY KEY (trade_date, product, cp, investor)
    );
    CREATE INDEX IF NOT EXISTS idx_fut_oi_date ON taifex_fut_oi(trade_date);
    CREATE INDEX IF NOT EXISTS idx_opt_oi_date ON taifex_opt_oi(trade_date);
    """)
    conn.commit()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    return conn


# ---------------------------------------------------------------- 抓取

def _num(s: str):
    """'1,234' -> 1234 ; '' / '-' -> None"""
    s = (s or "").strip().replace(",", "").replace('"', "")
    if s in ("", "-", "--"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _fetch_csv(path: str, d1: date, d2: date) -> list[list[str]]:
    """向期交所要一段日期區間的 CSV,回傳去掉表頭的資料列。"""
    url = f"{BASE}/{path}"
    params = {
        "firstDate": d1.strftime("%Y/%m/%d"),
        "lastDate": d2.strftime("%Y/%m/%d"),
        "queryStartDate": d1.strftime("%Y/%m/%d"),
        "queryEndDate": d2.strftime("%Y/%m/%d"),
        "commodityId": "",
    }
    r = requests.get(url, params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    text = r.content.decode("ms950", errors="replace")
    rows = [ln.split(",") for ln in text.splitlines() if ln.strip()]
    if not rows:
        return []
    # 第一列是表頭(以「日期」開頭)
    if rows[0] and rows[0][0].strip().startswith("日期"):
        rows = rows[1:]
    return rows


def fetch_fut_oi(d1: date, d2: date) -> list[tuple]:
    """三大法人-區分各期貨契約。
    CSV 欄位:日期,商品名稱,身份別,多方口數,多方金額,空方口數,空方金額,
             多空淨額口數,多空淨額金額,多方未平倉口數,多方未平倉金額,
             空方未平倉口數,空方未平倉金額,多空未平倉淨額口數,多空未平倉淨額金額
    """
    out = []
    for r in _fetch_csv("futContractsDateDown", d1, d2):
        if len(r) < 15:
            continue
        d = r[0].strip().replace("/", "-")
        out.append((d, r[1].strip(), r[2].strip(), *[_num(x) for x in r[3:15]]))
    return out


def fetch_opt_oi(d1: date, d2: date) -> list[tuple]:
    """三大法人-選擇權買賣權分計。
    CSV 欄位:日期,商品名稱,買賣權別,身份別,買方口數,買方金額,賣方口數,賣方金額,
             買賣差額口數,買賣差額金額,買方未平倉口數,買方未平倉金額,
             賣方未平倉口數,賣方未平倉金額,未平倉差額口數,未平倉差額金額
    """
    out = []
    for r in _fetch_csv("callsAndPutsDateDown", d1, d2):
        if len(r) < 16:
            continue
        d = r[0].strip().replace("/", "-")
        out.append((d, r[1].strip(), r[2].strip(), r[3].strip(),
                    *[_num(x) for x in r[4:16]]))
    return out


# ---------------------------------------------------------------- 儲存

def _save(conn, table, rows, ncols):
    if not rows:
        return 0
    ph = ",".join("?" * ncols)
    with conn:
        conn.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({ph})", rows)
    return len(rows)


def collect_range(d1: date, d2: date, conn=None) -> tuple[int, int]:
    own = conn is None
    conn = conn or get_conn()
    nf = _save(conn, "taifex_fut_oi", fetch_fut_oi(d1, d2), 15)
    time.sleep(SLEEP)
    no = _save(conn, "taifex_opt_oi", fetch_opt_oi(d1, d2), 16)
    if own:
        conn.close()
    return nf, no


def _month_iter(start: date, end: date):
    m = date(start.year, start.month, 1)
    while m <= end:
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield max(m, start), min(nxt - timedelta(days=1), end)
        m = nxt


def backfill(days: int):
    """逐月回補。已有資料的月份(期貨與選擇權皆有)自動跳過。"""
    conn = get_conn()
    end = date.today()
    start = end - timedelta(days=days)
    limit = end - timedelta(days=MAX_LOOKBACK_DAYS)
    if start < limit:
        log.warning("期交所只提供近 3 年資料:要求起點 %s 早於可用邊界 %s,已自動裁切。"
                    "更早的歷史(如 2011-2014)此來源查不到,回傳一律為空。",
                    start, limit)
        start = limit
    have_f = {r[0] for r in conn.execute(
        "SELECT DISTINCT substr(trade_date,1,7) FROM taifex_fut_oi")}
    have_o = {r[0] for r in conn.execute(
        "SELECT DISTINCT substr(trade_date,1,7) FROM taifex_opt_oi")}
    done = have_f & have_o
    # 當月與上月一定重抓(資料可能還沒補齊)
    fresh = {end.strftime("%Y-%m"),
             (end.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")}
    done -= fresh

    tf = to = 0
    for a, b in _month_iter(start, end):
        ym = a.strftime("%Y-%m")
        if ym in done:
            continue
        try:
            nf, no = collect_range(a, b, conn)
            tf += nf
            to += no
            log.info("%s  期貨 %d 列 / 選擇權 %d 列", ym, nf, no)
        except Exception as e:
            log.warning("%s 失敗: %s", ym, e)
        time.sleep(SLEEP)
    log.info("回補完成:期貨 %d 列、選擇權 %d 列", tf, to)
    conn.close()


# ---------------------------------------------------------------- 檢查用

def test_run(n_days=10):
    """抓最近幾天,直接印出台指相關資料,不寫入 DB —— 用來確認解析正確。"""
    d2 = date.today()
    d1 = d2 - timedelta(days=n_days)
    print(f"查詢區間 {d1} ~ {d2}\n")

    fut = fetch_fut_oi(d1, d2)
    print(f"=== 期貨 共 {len(fut)} 列,以下只列「臺股期貨」 ===")
    print(f"{'日期':<12}{'身份別':<10}{'未平倉多':>10}{'未平倉空':>10}{'淨額口數':>10}")
    for r in fut:
        if r[1] == "臺股期貨":
            print(f"{r[0]:<12}{r[2]:<10}{r[9]:>10}{r[11]:>10}{r[13]:>10}")

    time.sleep(SLEEP)
    opt = fetch_opt_oi(d1, d2)
    print(f"\n=== 選擇權 共 {len(opt)} 列,以下只列「臺指選擇權」外資 ===")
    print(f"{'日期':<12}{'CP':<6}{'未平倉買':>10}{'未平倉賣':>10}{'淨額口數':>10}")
    for r in opt:
        if r[1] == "臺指選擇權" and r[3] == "外資及陸資":
            print(f"{r[0]:<12}{r[2]:<6}{r[10]:>10}{r[12]:>10}{r[14]:>10}")


def summary():
    conn = get_conn()
    for t in ("taifex_fut_oi", "taifex_opt_oi"):
        row = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT trade_date), MIN(trade_date), MAX(trade_date) "
            f"FROM {t}").fetchone()
        print(f"{t}: {row[0]:,} 列,{row[1]} 個交易日,{row[2]} ~ {row[3]}")
    print("\n商品別(期貨,前 10):")
    for p, n in conn.execute(
            "SELECT product, COUNT(*) FROM taifex_fut_oi GROUP BY product "
            "ORDER BY COUNT(*) DESC LIMIT 10"):
        print(f"  {p}  {n:,}")
    print("\n最近一日 臺股期貨未平倉淨額:")
    for r in conn.execute(
            "SELECT trade_date, investor, oi_net_lots FROM taifex_fut_oi "
            "WHERE product='臺股期貨' AND trade_date=(SELECT MAX(trade_date) FROM taifex_fut_oi)"):
        print(f"  {r[0]}  {r[1]:<10} {r[2]:>10,}")
    print("\n最近一日 臺指選擇權外資未平倉淨額:")
    for r in conn.execute(
            "SELECT trade_date, cp, oi_net_lots FROM taifex_opt_oi "
            "WHERE product='臺指選擇權' AND investor='外資及陸資' "
            "AND trade_date=(SELECT MAX(trade_date) FROM taifex_opt_oi)"):
        print(f"  {r[0]}  {r[1]:<6} {r[2]:>10,}")
    conn.close()


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "recent"
    n = int(args[1]) if len(args) > 1 else None

    if cmd == "test":
        test_run(n or 10)
    elif cmd == "recent":
        d2 = date.today()
        d1 = d2 - timedelta(days=n or 30)
        nf, no = collect_range(d1, d2)
        log.info("寫入:期貨 %d 列、選擇權 %d 列", nf, no)
    elif cmd == "backfill":
        backfill(n or 730)
    elif cmd == "summary":
        summary()
    else:
        print(__doc__)
