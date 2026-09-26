"""Validated 0050 stock holdings from Yuanta's public PCF feed.

Reject partial/future/old lists. Cache dates come from the source, never the
fetch time. Only StockWeights is used; futures/cash/ETF rows are not members.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from data.paths import repo_file
from web.tw_calendar import is_tw_trading_day, taiwan_now

SOURCE_URL = "https://www.yuantaetfs.com/product/detail/0050/ratio"
API_URL = "https://etfapi.yuantaetfs.com/ectranslation/api/bridge"
DEFAULT_CACHE = repo_file(".cache", "0050-members.json")
MAX_AGE_TRADING_DAYS = 3
log = logging.getLogger(__name__)


def validate(snapshot, today):
    if snapshot.get("etf") != "0050":
        raise ValueError("Expected 0050 holdings")
    asof = date.fromisoformat(snapshot["asof"])
    if asof > today:
        raise ValueError("Holdings date is in the future")
    # Bound work before counting trading days; longer closures require refresh.
    if (today - asof).days > 31:
        raise ValueError("Holdings cache too old")
    age = sum(is_tw_trading_day(asof + timedelta(days=i))
              for i in range(1, (today - asof).days + 1))
    if age > MAX_AGE_TRADING_DAYS:
        raise ValueError("Holdings older than 3 trading days; monitoring paused")
    stocks = snapshot["stocks"]
    if not isinstance(stocks, list) or len(stocks) != 50:
        raise ValueError("Expected all 50 stock holdings; refusing partial list")
    codes = set()
    for row in stocks:
        code = row["code"]
        if not isinstance(code, str) or not re.fullmatch(r"[1-9][0-9]{3}", code):
            raise ValueError("Invalid stock member code")
        if code in codes or not isinstance(row["name"], str) or not row["name"].strip():
            raise ValueError("Duplicate or unnamed stock member")
        if not math.isfinite(float(row["weight"])) or float(row["weight"]) <= 0:
            raise ValueError("Invalid member weight")
        codes.add(code)
    return snapshot


def parse_holdings(payload, today):
    pcf = payload["PCF"]
    if pcf.get("markcd") != "0050":
        raise ValueError("Wrong ETF returned")
    asof = datetime.strptime(pcf["trandate"], "%Y%m%d").date().isoformat()
    rows = payload["FundWeights"]["StockWeights"]
    stocks = [{"code": str(r["code"]), "name": r["name"], "weight": float(r["weights"])}
              for r in rows]
    version = hashlib.sha256(
        (asof + ":" + ",".join(sorted(r["code"] for r in stocks))).encode()
    ).hexdigest()[:16]
    return validate({"etf": "0050", "asof": asof, "stocks": stocks,
                     "version": version, "source": SOURCE_URL}, today)


def fetch_holdings(today, session=None):
    client = session or requests
    response = client.get(API_URL, params={
        "APIType": "ETFAPI", "CompanyName": "YUANTAFUNDS", "FuncId": "PCF/Daily",
        "ticker": "0050", "AppName": "ETF", "Device": "3", "Platform": "ETF",
    }, timeout=20)
    response.raise_for_status()
    return parse_holdings(response.json(), today)


def write_cache(path, snapshot):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_members(path=DEFAULT_CACHE, *, now=None, refresh=False, fetcher=None):
    now = taiwan_now(now)
    today = now.date()
    cached = None
    try:
        cached = validate(json.loads(Path(path).read_text()), today)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if cached and not refresh and cached.get("checked_on") == today.isoformat():
        return cached
    try:
        fresh = (fetcher or fetch_holdings)(today)
        validate(fresh, today)
        if cached and fresh["asof"] < cached["asof"]:
            raise ValueError("Source date regressed; keeping latest valid cache")
        fresh = {**fresh, "checked_on": today.isoformat()}
        write_cache(path, fresh)
        return fresh
    except (requests.RequestException, ValueError, KeyError, TypeError, OSError) as exc:
        if cached:
            log.warning("0050 refresh failed (%s); using validated cache asof=%s",
                        type(exc).__name__, cached["asof"])
            return cached
        raise RuntimeError("No current validated 0050 list; monitoring paused") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    snapshot = load_members(args.cache, refresh=args.refresh)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
