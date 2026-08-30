"""Daily PTT + Reddit highlights posted to Slack.

Usage:
    python -m notify.daily_digest
    python -m notify.daily_digest --dry-run
    python -m notify.daily_digest --skip-ptt
    python -m notify.daily_digest --days 1
"""

from __future__ import annotations

import argparse
import os
import traceback
from typing import Callable

from notify import notify_job
from ptt import ptt_stock
from reddit import ideas as reddit_ideas

SLACK_LIMIT = 3500


def slack_link(url: str, title: str) -> str:
    label = " ".join((title or url or "").split())
    for ch in ("<", ">", "|"):
        label = label.replace(ch, "")
    label = label[:80] or url
    return f"<{url}|{label}>"


def _clip(text: str, limit: int = SLACK_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 12].rstrip() + "\n…(truncated)"


def format_ptt_slack(digest: ptt_stock.Digest, days: int, min_push: int) -> str:
    lines = [
        f"*PTT 股板今日重點*（近 {days} 天、推 > {min_push}，{len(digest.posts)} 篇）",
    ]
    themes = digest.themes[:5]
    lines.append("")
    lines.append("*題材*")
    if not themes:
        lines.append("沒有可分群的題材")
    for i, theme in enumerate(themes, 1):
        top = theme.posts[0]
        codes = f" `{', '.join(theme.tickers)}`" if theme.tickers else ""
        lines.append(
            f"{i}. {theme.key}{codes}  {slack_link(top.url, theme.title)}（{len(theme.posts)} 篇，最高 {top.push_label}）"
        )

    targets = digest.targets[:6]
    lines.append("")
    lines.append("*標的*")
    if not targets:
        lines.append("沒有 [標的] 文")
    for post in targets:
        codes = " ".join(ptt_stock.extract_tickers(post.title))
        prefix = f"`{codes}` " if codes else ""
        lines.append(
            f"• {post.date} {post.push_label:>3} {prefix}{slack_link(post.url, ptt_stock.normalize_title(post.title))}"
        )

    chats = [c for c in digest.chats if "盤後" in c.kind] or digest.chats[:1]
    if chats:
        chat = chats[0]
        tickers = "、".join(f"{code}×{n}" for code, n in chat.tickers[:6]) or "沒有明顯代碼"
        lines.append("")
        lines.append(f"*盤後閒聊* {chat.date}（{tickers}）")
        lines.append(slack_link(chat.post.url, chat.post.title))
        for push in chat.comments[:3]:
            body = " ".join(push.content.split())[:120]
            lines.append(f"• {push.user}：{body}")
    return _clip("\n".join(lines))


def format_reddit_slack(
    digest: reddit_ideas.Digest,
    days: int,
    min_score: int,
    subs: tuple[str, ...],
    source: str = "reddit",
) -> str:
    sub_txt = ", ".join(f"r/{s}" for s in subs)
    feed = "Arctic Shift" if source == "archive" else "Reddit"
    lines = [
        f"*Reddit 投資想法今日重點*（近 {days} 天、分數 ≥ {min_score}，{len(digest.posts)} 篇）",
        f"{sub_txt} · {feed}",
    ]
    themes = digest.themes[:5]
    lines.append("")
    lines.append("*題材*")
    if not themes:
        lines.append("沒有可分群的題材")
    for i, theme in enumerate(themes, 1):
        top = theme.posts[0]
        codes = f" `{', '.join(theme.tickers)}`" if theme.tickers else ""
        lines.append(
            f"{i}. {theme.key}{codes}  {slack_link(top.url, theme.title)}（r/{top.subreddit}，{top.score}↑）"
        )

    ideas = digest.ideas[:6]
    lines.append("")
    lines.append("*DD / 標的*")
    if not ideas:
        lines.append("沒有帶 flair 或代碼的想法文")
    for post in ideas:
        codes = " ".join(reddit_ideas.extract_tickers(f"{post.title} {post.selftext}"))
        prefix = f"`{codes}` " if codes else ""
        flair = f"[{post.flair}] " if post.flair else ""
        lines.append(
            f"• {post.date} {post.score}↑ {prefix}{flair}{slack_link(post.url, post.title)}"
        )
        blurb = reddit_ideas.clip_text(post.selftext, 120)
        if blurb:
            lines.append(f"  {blurb}")

    if digest.threads:
        thread = digest.threads[0]
        lines.append("")
        lines.append(f"*精選討論* {slack_link(thread.post.url, thread.post.title)}")
        for comment in thread.comments[:2]:
            lines.append(f"• {comment.author}（{comment.score}↑）：{comment.body}")
    return _clip("\n".join(lines))


def collect_ptt_digest(
    *,
    days: int,
    min_push: int,
    fetch: Callable[[str], str] | None = None,
) -> ptt_stock.Digest:
    posts = ptt_stock.collect_posts(
        days=days,
        min_push=min_push,
        max_pages=30,
        sleep_s=0 if fetch is not None else 0.8,
        fetch=fetch,
    )
    return ptt_stock.build_digest(
        posts,
        fetch=fetch,
        sleep_s=0 if fetch is not None else 0.8,
        include_chat=True,
        comment_limit=3,
    )


def collect_reddit_digest(
    *,
    days: int,
    min_score: int,
    fetch: Callable | None = None,
    source: str = "auto",
) -> tuple[reddit_ideas.Digest, str, tuple[str, ...]]:
    subs = reddit_ideas.DEFAULT_SUBS
    posts, used = reddit_ideas.collect_posts(
        days=days,
        min_score=min_score,
        subs=subs,
        max_pages=2,
        sleep_s=0 if fetch is not None else 0.8,
        fetch=fetch,
        source=source,
    )
    digest = reddit_ideas.build_digest(
        posts,
        fetch=fetch,
        sleep_s=0 if fetch is not None else 0.8,
        include_comments=True,
        comment_limit=2,
        thread_limit=2,
        source=used,
    )
    return digest, used, subs


def post_messages(
    texts: list[str],
    *,
    env: dict[str, str] | None = None,
    client=None,
) -> str:
    env = env if env is not None else os.environ
    if not notify_job.configured(env):
        return "skipped"
    token = (env.get("SLACK_BOT_TOKEN") or "").strip()
    channel = (env.get("SLACK_CHANNEL") or "").strip()
    if client is None:
        from slack_sdk import WebClient

        client = WebClient(token=token)
    for text in texts:
        if text:
            client.chat_postMessage(channel=channel, text=text)
    return "sent"


def run(
    *,
    days: int = 1,
    min_push: int = 15,
    min_score: int = 20,
    skip_ptt: bool = False,
    skip_reddit: bool = False,
    dry_run: bool = False,
    collect_ptt: Callable | None = None,
    collect_reddit: Callable | None = None,
    env: dict[str, str] | None = None,
    client=None,
) -> list[str]:
    messages: list[str] = []
    if not skip_ptt:
        try:
            getter = collect_ptt or collect_ptt_digest
            digest = getter(days=days, min_push=min_push)
            messages.append(format_ptt_slack(digest, days, min_push))
        except Exception as exc:
            print(f"  [warn] PTT digest failed: {exc}")
            traceback.print_exc()
            messages.append(f"*PTT 股板今日重點*\n抓取失敗：{exc}")
    if not skip_reddit:
        try:
            getter = collect_reddit or collect_reddit_digest
            digest, source, subs = getter(days=days, min_score=min_score)
            messages.append(format_reddit_slack(digest, days, min_score, subs, source))
        except Exception as exc:
            print(f"  [warn] Reddit digest failed: {exc}")
            traceback.print_exc()
            messages.append(f"*Reddit 投資想法今日重點*\n抓取失敗：{exc}")

    for text in messages:
        print(text)
        print()
    if dry_run:
        print("dry-run: not posting to Slack")
        return messages
    status = post_messages(messages, env=env, client=client)
    print(f"slack: {status}")
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每天整理 PTT / Reddit 重點並送到 Slack")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--min-push", type=int, default=15)
    parser.add_argument("--min-score", type=int, default=20)
    parser.add_argument("--skip-ptt", action="store_true")
    parser.add_argument("--skip-reddit", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    run(
        days=args.days,
        min_push=args.min_push,
        min_score=args.min_score,
        skip_ptt=args.skip_ptt,
        skip_reddit=args.skip_reddit,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
