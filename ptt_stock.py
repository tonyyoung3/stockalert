"""Collect hot PTT Stock posts from the last week (push count > 30).

Usage:
    python ptt_stock.py
    python ptt_stock.py --days 7 --min-push 30 --json ptt_week.json
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Callable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BOARD_URL = "https://www.ptt.cc/bbs/Stock/index.html"
PTT_ORIGIN = "https://www.ptt.cc"
USER_AGENT = "stockalert-ptt-weekly/1.0 (personal research; +https://github.com/tonyyoung3/stockalert)"


@dataclass
class Post:
    date: str
    push: int
    push_label: str
    title: str
    author: str
    url: str


def parse_push(label: str) -> int:
    text = (label or "").strip()
    if text == "爆":
        return 100
    if text.isdigit():
        return int(text)
    return 0


def parse_list_date(raw: str, today: date | None = None) -> date | None:
    """PTT list dates look like ' 8/30' and have no year."""
    today = today or date.today()
    match = re.search(r"(\d{1,2})/(\d{1,2})", raw or "")
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    try:
        parsed = date(today.year, month, day)
    except ValueError:
        return None
    # Early January lists still show December dates from last year.
    if parsed - today > timedelta(days=30):
        parsed = date(today.year - 1, month, day)
    return parsed


def parse_index_html(html: str, today: date | None = None, skip_pinned: bool = True) -> tuple[list[Post], str | None]:
    """Return posts on this index page and the previous-page URL (if any)."""
    today = today or date.today()
    soup = BeautifulSoup(html, "html.parser")
    posts: list[Post] = []

    ents = soup.select("div.r-ent")
    sep = soup.select_one("div.r-list-sep")
    if skip_pinned and sep is not None:
        pinned = set()
        for sibling in sep.find_previous_siblings("div", class_="r-ent"):
            pinned.add(id(sibling))
        ents = [ent for ent in ents if id(ent) not in pinned]

    for ent in ents:
        link = ent.select_one("div.title a")
        if link is None:
            continue
        href = link.get("href") or ""
        if not href:
            continue
        label = ent.select_one("div.nrec").get_text(" ", strip=True) if ent.select_one("div.nrec") else ""
        author = ent.select_one("div.author")
        date_el = ent.select_one("div.date")
        parsed = parse_list_date(date_el.get_text(strip=True) if date_el else "", today)
        posts.append(
            Post(
                date=str(parsed) if parsed else "",
                push=parse_push(label),
                push_label=label or "0",
                title=link.get_text(strip=True),
                author=author.get_text(strip=True) if author else "",
                url=urljoin(PTT_ORIGIN, href),
            )
        )

    prev_link = soup.find("a", string=re.compile(r"上頁"))
    prev_href = prev_link.get("href") if prev_link else None
    prev_url = urljoin(PTT_ORIGIN, prev_href) if prev_href else None
    return posts, prev_url


def fetch_html(url: str, session: requests.Session) -> str:
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def collect_posts(
    *,
    days: int = 7,
    min_push: int = 30,
    max_pages: int = 80,
    sleep_s: float = 0.8,
    today: date | None = None,
    fetch: Callable[[str], str] | None = None,
    start_url: str = BOARD_URL,
) -> list[Post]:
    today = today or date.today()
    cutoff = today - timedelta(days=days)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.cookies.set("over18", "1", domain="www.ptt.cc")

    def _fetch(url: str) -> str:
        return fetch(url) if fetch is not None else fetch_html(url, session)

    seen: set[str] = set()
    kept: list[Post] = []
    url = start_url
    older_streak = 0

    for page_i in range(max_pages):
        html = _fetch(url)
        posts, prev_url = parse_index_html(html, today=today, skip_pinned=(page_i == 0))
        if not posts:
            break

        for post in posts:
            if not post.url or post.url in seen:
                continue
            seen.add(post.url)
            if not post.date:
                continue
            posted = date.fromisoformat(post.date)
            if posted < cutoff:
                older_streak += 1
                continue
            older_streak = 0
            if post.push > min_push:
                kept.append(post)

        # A full page of older-than-cutoff posts means we walked past the window.
        if older_streak >= max(8, len(posts)):
            break
        if prev_url is None:
            break
        url = prev_url
        if fetch is None:
            time.sleep(sleep_s)

    kept.sort(key=lambda p: (p.date, p.push), reverse=True)
    return kept


def format_table(posts: list[Post]) -> str:
    if not posts:
        return "近一週沒有推文數大於門檻的文章。"
    lines = [f"{'日期':<12} {'推':>4}  {'作者':<14} 標題", "-" * 72]
    for post in posts:
        lines.append(f"{post.date:<12} {post.push_label:>4}  {post.author:<14} {post.title}")
        lines.append(f"{'':>18}  {post.url}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="整理 PTT 股板近一週熱門文")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--min-push", type=int, default=30, help="推文數須大於此值")
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--json", dest="json_path", help="順便寫出 JSON")
    args = parser.parse_args(argv)

    posts = collect_posts(days=args.days, min_push=args.min_push, max_pages=args.max_pages)
    print(f"PTT Stock 近 {args.days} 天、推文數 > {args.min_push}：{len(posts)} 篇\n")
    print(format_table(posts))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump([asdict(p) for p in posts], fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"\n已寫入 {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
