"""Collect and summarize interesting Reddit investment ideas.

Usage:
    python -m reddit.ideas
    python -m reddit.ideas --days 7 --min-score 40 --json reddit_week.json
    python -m reddit.ideas --raw
    python -m reddit.ideas --no-comments
    python -m reddit.ideas --subs SecurityAnalysis,ValueInvesting
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urljoin

import requests

REDDIT_ORIGIN = "https://www.reddit.com"
ARCHIVE_ORIGIN = "https://arctic-shift.photon-reddit.com"
USER_AGENT = "stockalert-reddit-ideas/1.0 (personal research; +https://github.com/tonyyoung3/stockalert)"
DEFAULT_SUBS = (
    "SecurityAnalysis",
    "ValueInvesting",
    "investing",
    "stocks",
    "wallstreetbets",
)
WSB_SUBS = {"wallstreetbets"}
WSB_KEEP_FLAIRS = {
    "dd",
    "due diligence",
    "research",
    "thesis",
    "fundamentals",
    "fundamental",
    "technical analysis",
}
WSB_SKIP_FLAIRS = {
    "meme",
    "gain",
    "loss",
    "shitpost",
    "daily discussion",
    "weekend discussion",
    "news",
    "chart",
}
SKIP_TITLE_RE = re.compile(
    r"(daily discussion|weekend discussion|daily advice|"
    r"what are your moves|rate my portfolio|megathread|"
    r"weekly earnings thread|daily thread)",
    re.I,
)
IDEA_FLAIRS = {
    "dd",
    "due diligence",
    "research",
    "thesis",
    "fundamentals",
    "fundamental",
    "stock analysis",
    "analysis",
    "discussion",
    "stock",
}
CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
PAREN_TICKER_RE = re.compile(r"\(([A-Za-z]{1,5})\)")
TICKER_STOP = {
    "A", "I", "AM", "BE", "IT", "ON", "OR", "FOR", "ALL", "NOW", "THE", "AND",
    "ARE", "YOU", "CAN", "HAS", "WAS", "OUT", "NEW", "BIG", "LOW", "HIGH",
    "BUY", "SELL", "PUT", "CALL", "LONG", "SHORT", "DD", "YOLO", "ETF", "CEO",
    "CFO", "CTO", "IPO", "GDP", "FED", "AI", "US", "USA", "UK", "EU", "IMO",
    "SEC", "FDA", "EPS", "PE", "PB", "ROE", "ROI", "ATH", "ATL", "WSB",
    "OTC", "NYSE", "NASDAQ", "AMEX", "TLDR", "EDIT", "ELI5",
    "Q1", "Q2", "Q3", "Q4", "FY", "YOY", "QOQ", "TTM",
}

COMPANY_ALIASES = (
    ("nvidia", "NVDA"),
    ("nvda", "NVDA"),
    ("tesla", "TSLA"),
    ("apple", "AAPL"),
    ("microsoft", "MSFT"),
    ("amazon", "AMZN"),
    ("google", "GOOGL"),
    ("alphabet", "GOOGL"),
    ("meta", "META"),
    ("facebook", "META"),
    ("berkshire", "BRK.B"),
    ("tsmc", "TSM"),
    ("台積電", "TSM"),
    ("asml", "ASML"),
    ("broadcom", "AVGO"),
    ("amd", "AMD"),
)


@dataclass
class Post:
    date: str
    score: int
    comments: int
    title: str
    author: str
    url: str
    permalink: str
    subreddit: str
    flair: str
    selftext: str
    post_id: str


@dataclass
class Comment:
    author: str
    score: int
    body: str


@dataclass
class Theme:
    key: str
    title: str
    posts: list[Post]
    max_score: int
    tickers: list[str]


@dataclass
class IdeaThread:
    post: Post
    tickers: list[str]
    comments: list[Comment]
    blurb: str


@dataclass
class Digest:
    posts: list[Post]
    themes: list[Theme]
    ideas: list[Post]
    threads: list[IdeaThread]


def utc_date(ts: float) -> date:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()


def clip_text(text: str, max_len: int = 180) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_len:
        return cleaned
    cut = cleaned[:max_len].rsplit(" ", 1)[0]
    return (cut or cleaned[:max_len]) + "…"


def is_daily_thread(title: str) -> bool:
    return bool(SKIP_TITLE_RE.search(title or ""))


def idea_flair(flair: str) -> bool:
    return (flair or "").strip().lower() in IDEA_FLAIRS


def extract_tickers(text: str) -> list[str]:
    found: list[str] = []
    blob = text or ""
    for match in CASHTAG_RE.findall(blob):
        found.append(match.upper())
    for match in PAREN_TICKER_RE.findall(blob):
        found.append(match.upper())
    lower = blob.lower()
    for needle, ticker in COMPANY_ALIASES:
        if needle.lower() in lower:
            found.append(ticker)
    return list(dict.fromkeys(t for t in found if t not in TICKER_STOP))


def theme_key(post: Post) -> str:
    tickers = extract_tickers(f"{post.title} {post.selftext}")
    if tickers:
        return tickers[0]
    compact = re.sub(r"[^\w]+", "", post.title or "")
    return compact[:18] or "other"


def is_wsb(subreddit: str) -> bool:
    return (subreddit or "").strip().lower() in WSB_SUBS


def is_idea_post(post: Post, min_score: int) -> bool:
    if is_daily_thread(post.title):
        return False
    flair = (post.flair or "").strip().lower()
    if is_wsb(post.subreddit):
        if flair in WSB_SKIP_FLAIRS:
            return False
        # WSB is noisy; only keep DD-style posts. Archive scores often
        # sit at 1 for a day, so do not require min_score here.
        return flair in WSB_KEEP_FLAIRS
    if post.score < min_score:
        return False
    if idea_flair(post.flair):
        return True
    if extract_tickers(f"{post.title} {post.selftext}"):
        return True
    if len((post.selftext or "").strip()) >= 180:
        return True
    return False


def row_to_post(row: dict) -> Post | None:
    if row.get("stickied") or row.get("removed_by_category"):
        return None
    title = row.get("title") or ""
    created = row.get("created_utc")
    if not title or created is None:
        return None
    permalink = row.get("permalink") or ""
    url = row.get("url") or ""
    if permalink:
        url = urljoin(REDDIT_ORIGIN, permalink)
    return Post(
        date=str(utc_date(created)),
        score=int(row.get("score") or 0),
        comments=int(row.get("num_comments") or 0),
        title=title,
        author=row.get("author") or "",
        url=url,
        permalink=permalink,
        subreddit=row.get("subreddit") or "",
        flair=(row.get("link_flair_text") or "").strip(),
        selftext=row.get("selftext") or "",
        post_id=str(row.get("id") or "").removeprefix("t3_"),
    )


def parse_listing(payload: dict) -> tuple[list[Post], str | None]:
    data = (payload or {}).get("data") or {}
    after = data.get("after")
    posts: list[Post] = []
    for child in data.get("children") or []:
        if child.get("kind") != "t3":
            continue
        post = row_to_post(child.get("data") or {})
        if post is not None:
            posts.append(post)
    return posts, after


def parse_archive_posts(payload: dict) -> tuple[list[Post], str | None]:
    rows = (payload or {}).get("data") or []
    posts: list[Post] = []
    oldest: float | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        post = row_to_post(row)
        if post is None:
            continue
        posts.append(post)
        created = row.get("created_utc")
        if created is not None:
            oldest = float(created) if oldest is None else min(oldest, float(created))
    return posts, (str(int(oldest)) if oldest is not None else None)


def comment_from_row(row: dict) -> Comment | None:
    body = (row.get("body") or "").strip()
    author = row.get("author") or ""
    if not body or author in {"[deleted]", "AutoModerator"}:
        return None
    if body in {"[deleted]", "[removed]"}:
        return None
    if body.startswith("http") and len(body) < 40:
        return None
    if len(body) < 40:
        return None
    return Comment(author=author, score=int(row.get("score") or 0), body=clip_text(body, 220))


def parse_comments(payload: list | dict, limit: int = 6) -> list[Comment]:
    """Accept Reddit comment JSON or Arctic Shift `{data: [row, ...]}`."""
    comments: list[Comment] = []
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        rows = [row for row in payload.get("data") or [] if isinstance(row, dict)]
        comments = [c for row in rows if (c := comment_from_row(row))]
    else:
        if isinstance(payload, list) and len(payload) >= 2:
            listing = payload[1]
        else:
            listing = payload
        children = ((listing or {}).get("data") or {}).get("children") or []
        for child in children:
            if child.get("kind") != "t1":
                continue
            comment = comment_from_row(child.get("data") or {})
            if comment is not None:
                comments.append(comment)
    comments.sort(key=lambda c: c.score, reverse=True)
    return comments[:limit]


def fetch_json(url: str, session: requests.Session) -> dict | list:
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    return resp.json()


def listing_url(sub: str, after: str | None = None, limit: int = 100) -> str:
    q = f"t=week&limit={limit}&raw_json=1"
    if after:
        q += f"&after={after}"
    return f"{REDDIT_ORIGIN}/r/{sub}/top.json?{q}"


def archive_posts_url(
    sub: str,
    *,
    after: str,
    before: str | None = None,
    limit: int = 100,
) -> str:
    q = f"subreddit={sub}&after={after}&sort=desc&limit={limit}"
    if before:
        q += f"&before={before}"
    return f"{ARCHIVE_ORIGIN}/api/posts/search?{q}"


def comment_url(post: Post) -> str:
    path = post.permalink or f"/comments/{post.post_id}"
    return urljoin(REDDIT_ORIGIN, f"{path}.json?limit=20&sort=top&raw_json=1")


def archive_comment_url(post: Post, limit: int = 50) -> str:
    return f"{ARCHIVE_ORIGIN}/api/comments/search?link_id={post.post_id}&limit={limit}"


def _blocked(exc: Exception) -> bool:
    if not isinstance(exc, requests.HTTPError) or exc.response is None:
        return False
    return exc.response.status_code in {403, 429}


def _keep_post(post: Post, cutoff: date, today: date, min_score: int, seen: set[str]) -> bool:
    key = post.post_id or post.url
    if not key or key in seen:
        return False
    seen.add(key)
    posted = date.fromisoformat(post.date)
    if posted < cutoff or posted > today:
        return False
    return is_idea_post(post, min_score)


def collect_posts(
    *,
    days: int = 7,
    min_score: int = 40,
    subs: tuple[str, ...] = DEFAULT_SUBS,
    max_pages: int = 3,
    sleep_s: float = 0.8,
    today: date | None = None,
    fetch: Callable[[str], dict | list] | None = None,
    source: str = "auto",
) -> tuple[list[Post], str]:
    today = today or date.today()
    cutoff = today - timedelta(days=days)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    backend = source

    def _fetch(url: str) -> dict | list:
        return fetch(url) if fetch is not None else fetch_json(url, session)

    seen: set[str] = set()
    kept: list[Post] = []

    for sub_i, sub in enumerate(subs):
        cursor = None
        pages = max_pages * 2 if is_wsb(sub) else max_pages
        archive_limit = 40 if sub.lower() in {"investing", "stocks", "wallstreetbets"} else 100
        skip_sub = False
        for _page in range(pages):
            posts: list[Post] = []
            if backend != "archive":
                url = listing_url(sub, after=cursor)
                try:
                    payload = _fetch(url)
                    backend = "reddit"
                    posts, cursor = parse_listing(payload if isinstance(payload, dict) else {})
                except Exception as exc:
                    if backend == "auto" and _blocked(exc):
                        print("  [warn] Reddit 拒絕未登入請求，改走 Arctic Shift 封存。")
                        backend = "archive"
                    else:
                        raise
            if backend == "archive":
                try:
                    payload = _fetch(
                        archive_posts_url(
                            sub, after=str(cutoff), before=cursor, limit=archive_limit
                        )
                    )
                    posts, cursor = parse_archive_posts(
                        payload if isinstance(payload, dict) else {}
                    )
                except Exception as exc:
                    print(f"  [warn] archive r/{sub} failed: {exc}")
                    skip_sub = True
            if skip_sub or not posts:
                break
            for post in posts:
                if _keep_post(post, cutoff, today, min_score, seen):
                    kept.append(post)
            if not cursor:
                break
            if fetch is None:
                time.sleep(sleep_s)
        if fetch is None and sub_i + 1 < len(subs):
            time.sleep(sleep_s)

    kept.sort(key=lambda p: (p.score, p.comments), reverse=True)
    return kept, (backend if backend != "auto" else "reddit")


def cluster_themes(posts: list[Post], limit: int = 8) -> list[Theme]:
    buckets: dict[str, list[Post]] = defaultdict(list)
    for post in posts:
        buckets[theme_key(post)].append(post)

    themes = []
    for key, group in buckets.items():
        tickers: list[str] = []
        for post in group:
            tickers.extend(extract_tickers(f"{post.title} {post.selftext}"))
        representative = max(group, key=lambda p: (p.score, len(p.selftext)))
        themes.append(
            Theme(
                key=key,
                title=representative.title,
                posts=sorted(group, key=lambda p: p.score, reverse=True),
                max_score=max(p.score for p in group),
                tickers=list(dict.fromkeys(tickers)),
            )
        )
    themes.sort(key=lambda t: (t.max_score, len(t.posts)), reverse=True)
    return themes[:limit]


def summarize_thread(post: Post, payload: list | dict, comment_limit: int = 6) -> IdeaThread:
    tickers = extract_tickers(f"{post.title} {post.selftext}")
    return IdeaThread(
        post=post,
        tickers=tickers,
        comments=parse_comments(payload, limit=comment_limit),
        blurb=clip_text(post.selftext, 180),
    )


def build_digest(
    posts: list[Post],
    *,
    fetch: Callable[[str], dict | list] | None = None,
    sleep_s: float = 0.8,
    comment_limit: int = 6,
    include_comments: bool = True,
    thread_limit: int = 5,
    source: str = "reddit",
) -> Digest:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    def _fetch(url: str) -> dict | list:
        return fetch(url) if fetch is not None else fetch_json(url, session)

    ideas = [p for p in posts if idea_flair(p.flair) or extract_tickers(f"{p.title} {p.selftext}")]
    ideas.sort(key=lambda p: (p.score, p.comments), reverse=True)

    threads: list[IdeaThread] = []
    if include_comments:
        for i, post in enumerate(ideas[:thread_limit]):
            url = archive_comment_url(post) if source == "archive" else comment_url(post)
            try:
                payload = _fetch(url)
            except Exception as exc:
                if source != "archive" and _blocked(exc):
                    try:
                        payload = _fetch(archive_comment_url(post))
                    except Exception as archive_exc:
                        print(f"  [warn] comments failed {post.title}: {archive_exc}")
                        threads.append(summarize_thread(post, {}, comment_limit=comment_limit))
                        continue
                else:
                    print(f"  [warn] comments failed {post.title}: {exc}")
                    threads.append(summarize_thread(post, {}, comment_limit=comment_limit))
                    continue
            threads.append(summarize_thread(post, payload, comment_limit=comment_limit))
            if fetch is None and i + 1 < min(thread_limit, len(ideas)):
                time.sleep(sleep_s)

    return Digest(
        posts=posts,
        themes=cluster_themes(posts),
        ideas=ideas[:20],
        threads=threads,
    )


def format_table(posts: list[Post]) -> str:
    if not posts:
        return "近一週沒有通過門檻的投資想法。"
    lines = [f"{'日期':<12} {'分':>5}  {'板':<18} 標題", "-" * 78]
    for post in posts:
        lines.append(f"{post.date:<12} {post.score:>5}  {post.subreddit:<18} {post.title}")
        lines.append(f"{'':>20}  {post.url}")
    return "\n".join(lines)


def format_digest(
    digest: Digest,
    days: int,
    min_score: int,
    subs: tuple[str, ...],
    source: str = "reddit",
) -> str:
    sub_txt = ", ".join(f"r/{s}" for s in subs)
    feed = "Arctic Shift 封存（Reddit 未登入被擋時的備援）" if source == "archive" else "Reddit"
    lines = [
        f"Reddit 投資想法：近 {days} 天、分數 ≥ {min_score}，共 {len(digest.posts)} 篇",
        f"來源：{sub_txt} · {feed}",
        "略過每日討論串與置頂，優先 DD / 研究文與有標的的長文。",
        "",
        "## 題材",
    ]
    if not digest.themes:
        lines.append("（沒有可分群的題材）")
    for i, theme in enumerate(digest.themes, 1):
        top = theme.posts[0]
        ticker_bit = f" 代碼 {', '.join(theme.tickers)}" if theme.tickers else ""
        lines.append(
            f"{i}. {theme.key}：{theme.title}（{len(theme.posts)} 篇，最高 {top.score}↑）{ticker_bit}"
        )
        lines.append(f"   r/{top.subreddit} · {top.date}")
        lines.append(f"   {top.url}")
    lines.append("")
    lines.append("## DD / 標的")
    if not digest.ideas:
        lines.append("（沒有帶 flair 或代碼的想法文）")
    for post in digest.ideas:
        codes = " ".join(extract_tickers(f"{post.title} {post.selftext}")) or ""
        flair = f"[{post.flair}] " if post.flair else ""
        lines.append(f"- {post.date} {post.score:>4}↑ {codes} {flair}{post.title}")
        blurb = clip_text(post.selftext, 160)
        if blurb:
            lines.append(f"  {blurb}")
        lines.append(f"  {post.url}")
    lines.append("")
    lines.append("## 精選留言")
    if not digest.threads:
        lines.append("（這週沒抓留言，或已用 --no-comments 略過）")
    for thread in digest.threads:
        codes = "、".join(thread.tickers) or "（沒有明顯代碼）"
        lines.append(
            f"### {thread.post.date} r/{thread.post.subreddit} {thread.post.score}↑（{codes}）"
        )
        lines.append(thread.post.title)
        if thread.blurb:
            lines.append(thread.blurb)
        lines.append(thread.post.url)
        if thread.comments:
            lines.append("精選留言：")
            for comment in thread.comments:
                lines.append(f"- {comment.author}（{comment.score}↑）：{comment.body}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def digest_json(digest: Digest) -> dict:
    return {
        "themes": [
            {
                "key": t.key,
                "title": t.title,
                "count": len(t.posts),
                "max_score": t.max_score,
                "tickers": t.tickers,
                "urls": [p.url for p in t.posts[:5]],
            }
            for t in digest.themes
        ],
        "ideas": [asdict(p) for p in digest.ideas],
        "threads": [
            {
                "date": c.post.date,
                "title": c.post.title,
                "url": c.post.url,
                "tickers": c.tickers,
                "blurb": c.blurb,
                "comments": [asdict(x) for x in c.comments],
            }
            for c in digest.threads
        ],
        "posts": [asdict(p) for p in digest.posts],
    }


def parse_subs(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_SUBS
    parts = []
    for raw_part in raw.split(","):
        text = raw_part.strip()
        if text.startswith("r/"):
            text = text[2:]
        if text:
            parts.append(text)
    return tuple(parts) or DEFAULT_SUBS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="整理 Reddit 近一週投資想法")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--min-score", type=int, default=40, help="分數須大於等於此值")
    parser.add_argument(
        "--subs",
        help="逗號分隔板名，預設含 wallstreetbets（只收 DD）",
    )
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--json", dest="json_path", help="順便寫出 JSON")
    parser.add_argument("--raw", action="store_true", help="只列原始清單，不做週報")
    parser.add_argument("--no-comments", action="store_true", help="不抓文章留言")
    parser.add_argument("--comment-limit", type=int, default=6)
    parser.add_argument(
        "--source",
        choices=("auto", "reddit", "archive"),
        default="auto",
        help="auto：先 Reddit，403/429 再改 Arctic Shift",
    )
    args = parser.parse_args(argv)

    subs = parse_subs(args.subs)
    posts, source = collect_posts(
        days=args.days,
        min_score=args.min_score,
        subs=subs,
        max_pages=args.max_pages,
        source=args.source,
    )
    if args.raw:
        print(f"Reddit 近 {args.days} 天、分數 ≥ {args.min_score}：{len(posts)} 篇\n")
        print(format_table(posts))
        payload: dict | list = [asdict(p) for p in posts]
    else:
        digest = build_digest(
            posts,
            include_comments=not args.no_comments,
            comment_limit=args.comment_limit,
            source=source,
        )
        print(format_digest(digest, args.days, args.min_score, subs, source=source))
        payload = digest_json(digest)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"已寫入 {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
