#!/usr/bin/env python3
"""Post a job failure notice and log tail to Slack.

Used by GitHub Actions when update_market_data.py (or another job) fails.
Needs SLACK_BOT_TOKEN and SLACK_CHANNEL. Missing secrets skip notify; the
job still fails from the original step.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

DEFAULT_TAIL_LINES = 80
DEFAULT_TAIL_CHARS = 3500


def tail_text(
    path: Path | None,
    max_lines: int = DEFAULT_TAIL_LINES,
    max_chars: int = DEFAULT_TAIL_CHARS,
) -> str:
    if path is None or not path.exists():
        return "(no log file)"
    text = path.read_text(encoding="utf-8", errors="replace")
    clipped = "\n".join(text.splitlines()[-max_lines:])
    if len(clipped) > max_chars:
        clipped = clipped[-max_chars:]
        nl = clipped.find("\n")
        if nl != -1:
            clipped = clipped[nl + 1 :]
    return clipped or "(empty log)"


def build_message(title: str, tail: str, run_url: str | None = None) -> str:
    parts = [f":x: *{title}*"]
    if run_url:
        parts.append(f"<{run_url}|打開 Actions log>")
    parts.append("```")
    parts.append(tail or "(empty log)")
    parts.append("```")
    return "\n".join(parts)


def configured(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else os.environ
    return bool((env.get("SLACK_BOT_TOKEN") or "").strip() and (env.get("SLACK_CHANNEL") or "").strip())


def notify(
    title: str,
    log_path: Path | None = None,
    run_url: str | None = None,
    env: dict[str, str] | None = None,
    client=None,
) -> str:
    """Send the failure notice. Returns 'sent' or 'skipped'."""
    env = env if env is not None else os.environ
    token = (env.get("SLACK_BOT_TOKEN") or "").strip()
    channel = (env.get("SLACK_CHANNEL") or "").strip()
    if not token or not channel:
        return "skipped"

    text = build_message(title, tail_text(log_path), run_url)
    if client is None:
        from slack_sdk import WebClient

        client = WebClient(token=token)
    client.chat_postMessage(channel=channel, text=text)
    if log_path is not None and log_path.exists() and log_path.stat().st_size > 0:
        try:
            client.files_upload_v2(
                channel=channel,
                file=str(log_path),
                filename=log_path.name,
                title=title,
            )
        except Exception:
            pass
    return "sent"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post a job failure log to Slack")
    parser.add_argument("--title", default="市場資料更新失敗")
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument(
        "--run-url",
        default=os.environ.get("GITHUB_RUN_URL") or "",
        help="Actions run URL (defaults to $GITHUB_RUN_URL)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_url = args.run_url or None
    result = notify(args.title, log_path=args.log, run_url=run_url)
    print(f"slack notify: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
