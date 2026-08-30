"""Collect and summarize hot PTT Stock posts.

Usage:
    python ptt_stock.py
    python ptt_stock.py --days 7 --min-push 30 --json ptt_week.json
    python ptt_stock.py --raw
    python ptt_stock.py --no-chat
    python ptt_stock.py --chat-comments 4
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BOARD_URL = "https://www.ptt.cc/bbs/Stock/index.html"
PTT_ORIGIN = "https://www.ptt.cc"
USER_AGENT = "stockalert-ptt-weekly/1.0 (personal research; +https://github.com/tonyyoung3/stockalert)"

FLAIR_RE = re.compile(r"^\[([^\]]+)\]")
TICKER_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)(?!元|萬|億|張)")
YEAR_LIKE = {str(y) for y in range(1990, 2036)}
PRICE_LIKE = {"1000", "1100", "1200", "1500", "3000", "7000", "8000", "9000"}
ROUTINE_MARKERS = ("三大法人", "信用交易", "買賣超排行", "SCFI")
_STOCKS_PATH = Path(__file__).with_name("taiwan_stocks.txt")
_KNOWN_TICKERS: set[str] | None = None
COMPANY_ALIASES = (
    ("欣興", "欣興"),
    ("台積電", "台積電"),
    ("輝達", "輝達"),
    ("NVDA", "輝達"),
    ("nvidia", "輝達"),
    ("國巨", "國巨"),
    ("大立光", "大立光"),
    ("世界先進", "世界先進"),
    ("聯電", "聯電"),
    ("景碩", "景碩"),
    ("藥華藥", "藥華藥"),
    ("和碩", "和碩"),
    ("長鑫", "長鑫"),
    ("Fed", "Fed／升息"),
    ("聯準會", "Fed／升息"),
    ("升息", "Fed／升息"),
)


@dataclass
class Post:
    date: str
    push: int
    push_label: str
    title: str
    author: str
    url: str


@dataclass
class Push:
    tag: str
    user: str
    content: str


@dataclass
class Theme:
    key: str
    title: str
    posts: list[Post]
    max_push: int
    tickers: list[str]


@dataclass
class ChatDay:
    date: str
    kind: str
    post: Post
    tickers: list[tuple[str, int]]
    comments: list[Push]
    push_count: int


@dataclass
class Digest:
    posts: list[Post]
    themes: list[Theme]
    targets: list[Post]
    chats: list[ChatDay]
    routine_count: int


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
    if parsed - today > timedelta(days=30):
        parsed = date(today.year - 1, month, day)
    return parsed


def flair(title: str) -> str:
    match = FLAIR_RE.match(title or "")
    return match.group(1) if match else ""


def chat_kind(title: str) -> str | None:
    if "盤後閒聊" in title:
        return "盤後閒聊"
    if "盤中閒聊" in title:
        return "盤中閒聊"
    return None


def is_routine(title: str) -> bool:
    return any(marker in title for marker in ROUTINE_MARKERS)


def known_tickers(path: Path | None = None) -> set[str]:
    global _KNOWN_TICKERS
    if path is None and _KNOWN_TICKERS is not None:
        return _KNOWN_TICKERS
    codes: set[str] = set()
    try:
        for line in (path or _STOCKS_PATH).read_text(encoding="utf-8").splitlines():
            code = line.strip().split(".")[0]
            if code.isdigit() and len(code) in (4, 5, 6):
                codes.add(code)
    except OSError:
        codes = set()
    if path is None:
        _KNOWN_TICKERS = codes
    return codes


def extract_tickers(text: str, known: set[str] | None = None) -> list[str]:
    known = known_tickers() if known is None else known
    found = []
    for match in TICKER_RE.findall(text or ""):
        if match in YEAR_LIKE:
            continue
        if known:
            if match not in known and not match.startswith("00"):
                continue
        elif match in PRICE_LIKE:
            continue
        found.append(match)
    return list(dict.fromkeys(found))


def _is_noise_comment(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    without_url = re.sub(r"https?://\S+", "", stripped).strip()
    if re.search(r"https?://", stripped) and len(without_url) < 8:
        return True
    if re.fullmatch(r"(gif|imgur|youtu\.be|youtube)\b.*", stripped, flags=re.I):
        return True
    return False


def normalize_title(title: str) -> str:
    text = title or ""
    text = re.sub(r"^(Re:\s*)+", "", text, flags=re.I)
    text = FLAIR_RE.sub("", text).strip()
    return re.sub(r"\s+", " ", text)


def theme_key(title: str) -> str:
    for needle, key in COMPANY_ALIASES:
        if needle.lower() in title.lower():
            return key
    tickers = extract_tickers(title)
    if tickers:
        return tickers[0]
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalize_title(title))
    return compact[:12] or "其他"


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
            if post.push > min_push or chat_kind(post.title):
                kept.append(post)

        if older_streak >= max(8, len(posts)):
            break
        if prev_url is None:
            break
        url = prev_url
        if fetch is None:
            time.sleep(sleep_s)

    kept.sort(key=lambda p: (p.date, p.push), reverse=True)
    return kept


def cluster_themes(posts: list[Post], limit: int = 8) -> list[Theme]:
    buckets: dict[str, list[Post]] = defaultdict(list)
    for post in posts:
        if chat_kind(post.title) or is_routine(post.title):
            continue
        buckets[theme_key(post.title)].append(post)

    themes = []
    for key, group in buckets.items():
        tickers = []
        for post in group:
            tickers.extend(extract_tickers(post.title))
        representative = max(group, key=lambda p: (p.push, len(p.title)))
        themes.append(
            Theme(
                key=key,
                title=normalize_title(representative.title) or key,
                posts=sorted(group, key=lambda p: p.push, reverse=True),
                max_push=max(p.push for p in group),
                tickers=list(dict.fromkeys(tickers)),
            )
        )
    themes.sort(key=lambda t: (t.max_push, len(t.posts)), reverse=True)
    return themes[:limit]


def parse_article_pushes(html: str) -> list[Push]:
    soup = BeautifulSoup(html, "html.parser")
    pushes: list[Push] = []
    for row in soup.select("div.push"):
        tag_el = row.select_one("span.push-tag")
        user_el = row.select_one("span.push-userid")
        content_el = row.select_one("span.push-content")
        content = content_el.get_text(" ", strip=True) if content_el else ""
        content = content.lstrip(":：").strip()
        if not content:
            continue
        pushes.append(
            Push(
                tag=(tag_el.get_text(strip=True) if tag_el else ""),
                user=(user_el.get_text(strip=True) if user_el else ""),
                content=content,
            )
        )
    return pushes


def summarize_chat(post: Post, html: str, comment_limit: int = 8) -> ChatDay:
    pushes = parse_article_pushes(html)
    counts: Counter[str] = Counter()
    useful: list[Push] = []
    for push in pushes:
        if push.tag.startswith("噓") or _is_noise_comment(push.content):
            continue
        tickers = extract_tickers(push.content)
        counts.update(tickers)
        if tickers or len(push.content) >= 16:
            useful.append(push)
    useful.sort(key=lambda p: (0 if extract_tickers(p.content) else 1, -len(p.content)))
    picked: list[Push] = []
    seen_text: set[str] = set()
    for push in useful:
        key = push.content[:40]
        if key in seen_text:
            continue
        seen_text.add(key)
        picked.append(push)
        if len(picked) >= comment_limit:
            break
    return ChatDay(
        date=post.date,
        kind=chat_kind(post.title) or "閒聊",
        post=post,
        tickers=counts.most_common(10),
        comments=picked,
        push_count=len(pushes),
    )


def build_digest(
    posts: list[Post],
    *,
    fetch: Callable[[str], str] | None = None,
    sleep_s: float = 0.8,
    comment_limit: int = 8,
    include_chat: bool = True,
) -> Digest:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.cookies.set("over18", "1", domain="www.ptt.cc")

    def _fetch(url: str) -> str:
        return fetch(url) if fetch is not None else fetch_html(url, session)

    chats: list[ChatDay] = []
    if include_chat:
        chat_posts = [p for p in posts if chat_kind(p.title)]
        chat_posts.sort(key=lambda p: (p.date, 0 if "盤後" in p.title else 1), reverse=True)
        for i, post in enumerate(chat_posts):
            try:
                html = _fetch(post.url)
            except Exception as exc:
                print(f"  [warn] 閒聊抓取失敗 {post.title}: {exc}")
                continue
            chats.append(summarize_chat(post, html, comment_limit=comment_limit))
            if fetch is None and i + 1 < len(chat_posts):
                time.sleep(sleep_s)

    return Digest(
        posts=posts,
        themes=cluster_themes(posts),
        targets=[p for p in posts if flair(p.title) == "標的"],
        chats=chats,
        routine_count=sum(1 for p in posts if is_routine(p.title)),
    )


def format_table(posts: list[Post]) -> str:
    if not posts:
        return "近一週沒有推文數大於門檻的文章。"
    lines = [f"{'日期':<12} {'推':>4}  {'作者':<14} 標題", "-" * 72]
    for post in posts:
        lines.append(f"{post.date:<12} {post.push_label:>4}  {post.author:<14} {post.title}")
        lines.append(f"{'':>18}  {post.url}")
    return "\n".join(lines)


def format_digest(digest: Digest, days: int, min_push: int) -> str:
    lines = [
        f"PTT 股板週報：近 {days} 天、推文數 > {min_push}，共 {len(digest.posts)} 篇",
        f"例行文（法人表／信用／排行）{digest.routine_count} 篇已另外抽出，不列入題材。",
        "",
        "## 題材",
    ]
    if not digest.themes:
        lines.append("（沒有可分群的題材）")
    for i, theme in enumerate(digest.themes, 1):
        top = theme.posts[0]
        ticker_bit = f" 代碼 {', '.join(theme.tickers)}" if theme.tickers else ""
        lines.append(
            f"{i}. {theme.key}：{theme.title}（{len(theme.posts)} 篇，最高 {top.push_label}）{ticker_bit}"
        )
        lines.append(f"   代表：{top.title}")
        lines.append(f"   {top.url}")
    lines.append("")
    lines.append("## 標的文")
    if not digest.targets:
        lines.append("（沒有 [標的] 文）")
    for post in digest.targets:
        codes = " ".join(extract_tickers(post.title)) or ""
        lines.append(f"- {post.date} {post.push_label:>3} {codes} {normalize_title(post.title)}")
        lines.append(f"  {post.url}")
    lines.append("")
    lines.append("## 盤中／盤後閒聊")
    if not digest.chats:
        lines.append("（這週沒抓到閒聊文，或已用 --no-chat 略過）")
    for chat in digest.chats:
        ticker_txt = "、".join(f"{code}×{n}" for code, n in chat.tickers) or "（推文裡沒有明顯代碼）"
        lines.append(f"### {chat.date} {chat.kind}（{chat.push_count} 則推文，列表推數 {chat.post.push_label}）")
        lines.append(f"熱門代碼：{ticker_txt}")
        lines.append(f"{chat.post.url}")
        if chat.comments:
            lines.append("精選推文：")
            for push in chat.comments:
                lines.append(f"- {push.user}：{push.content}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def digest_json(digest: Digest) -> dict:
    return {
        "themes": [
            {
                "key": t.key,
                "title": t.title,
                "count": len(t.posts),
                "max_push": t.max_push,
                "tickers": t.tickers,
                "urls": [p.url for p in t.posts[:5]],
            }
            for t in digest.themes
        ],
        "targets": [asdict(p) for p in digest.targets],
        "chats": [
            {
                "date": c.date,
                "kind": c.kind,
                "url": c.post.url,
                "tickers": c.tickers,
                "comments": [asdict(p) for p in c.comments],
            }
            for c in digest.chats
        ],
        "posts": [asdict(p) for p in digest.posts],
        "routine_count": digest.routine_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="整理 PTT 股板近一週熱門文")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--min-push", type=int, default=30, help="推文數須大於此值")
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--json", dest="json_path", help="順便寫出 JSON")
    parser.add_argument("--raw", action="store_true", help="只列原始清單，不做週報")
    parser.add_argument("--no-chat", action="store_true", help="不抓盤中／盤後閒聊本文")
    parser.add_argument("--chat-comments", type=int, default=8)
    args = parser.parse_args(argv)

    posts = collect_posts(days=args.days, min_push=args.min_push, max_pages=args.max_pages)
    if args.raw:
        print(f"PTT Stock 近 {args.days} 天、推文數 > {args.min_push}：{len(posts)} 篇\n")
        print(format_table(posts))
        payload: dict | list = [asdict(p) for p in posts]
    else:
        digest = build_digest(
            posts,
            include_chat=not args.no_chat,
            comment_limit=args.chat_comments,
        )
        print(format_digest(digest, args.days, args.min_push))
        payload = digest_json(digest)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"已寫入 {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
