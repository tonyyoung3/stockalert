import unittest
from datetime import datetime, timezone
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from notify import daily_digest
from ptt.ptt_stock import ChatDay, Digest as PttDigest, Post as PttPost, Push, Theme as PttTheme
from reddit.ideas import Comment, Digest as RedditDigest, Post as RedditPost, Theme as RedditTheme

TAIPEI = ZoneInfo("Asia/Taipei")
SLACK_ENV = {"SLACK_BOT_TOKEN": "x", "SLACK_CHANNEL": "#alerts"}


def _ptt_digest() -> PttDigest:
    target = PttPost("2026-08-30", 99, "99", "[標的] 2330 多", "foo", "https://www.ptt.cc/bbs/Stock/M.hot.html")
    chat_post = PttPost(
        "2026-08-30", 100, "爆", "[閒聊] 2026/08/30 盤後閒聊", "laptic", "https://www.ptt.cc/bbs/Stock/M.chat.html"
    )
    return PttDigest(
        posts=[target, chat_post],
        themes=[
            PttTheme("2330", "2330 多", [target], 99, ["2330"]),
        ],
        targets=[target],
        chats=[
            ChatDay(
                date="2026-08-30",
                kind="盤後閒聊",
                post=chat_post,
                tickers=[("2330", 4)],
                comments=[Push("推", "aa", "2330 今天還行")],
                push_count=20,
            )
        ],
        routine_count=0,
    )


def _reddit_digest() -> RedditDigest:
    post = RedditPost(
        "2026-08-30",
        120,
        40,
        "Why I still like $NVDA",
        "foo",
        "https://www.reddit.com/r/SecurityAnalysis/comments/abc/nvda/",
        "/r/SecurityAnalysis/comments/abc/nvda/",
        "SecurityAnalysis",
        "DD",
        "Long thesis on inference demand.",
        "abc",
    )
    return RedditDigest(
        posts=[post],
        themes=[RedditTheme("NVDA", post.title, [post], 120, ["NVDA"])],
        ideas=[post],
        threads=[],
    )


def _slack_client(*, messages=None, history_error=None):
    client = Mock()
    if history_error is not None:
        client.conversations_history.side_effect = history_error
    else:
        client.conversations_history.return_value = {
            "ok": True,
            "messages": list(messages or []),
        }
    return client


def _run(client, **kwargs):
    kwargs.setdefault("collect_ptt", lambda **_: _ptt_digest())
    kwargs.setdefault("collect_reddit", lambda **_: (_reddit_digest(), "reddit", ("stocks",)))
    kwargs.setdefault("env", SLACK_ENV)
    kwargs.setdefault("dry_run", False)
    return daily_digest.run(client=client, **kwargs)


class DailyDigestTests(unittest.TestCase):
    def test_ptt_slack_is_compact(self):
        text = daily_digest.format_ptt_slack(_ptt_digest(), 1, 15)
        self.assertIn("*PTT 股板今日重點*", text)
        self.assertIn("*題材*", text)
        self.assertIn("*標的*", text)
        self.assertIn("2330", text)
        self.assertIn("<https://www.ptt.cc/bbs/Stock/M.hot.html|", text)
        self.assertIn("盤後閒聊", text)
        self.assertNotIn("## ", text)

    def test_reddit_slack_is_compact(self):
        text = daily_digest.format_reddit_slack(
            _reddit_digest(), 1, 20, ("SecurityAnalysis",), "archive"
        )
        self.assertIn("*Reddit 投資想法今日重點*", text)
        self.assertIn("NVDA", text)
        self.assertIn("Arctic Shift", text)
        self.assertIn("<https://www.reddit.com/r/SecurityAnalysis/comments/abc/nvda/|", text)

    def test_run_dry_run_uses_injected_collectors(self):
        client = Mock()
        messages = daily_digest.run(
            days=1,
            dry_run=True,
            collect_ptt=lambda **_: _ptt_digest(),
            collect_reddit=lambda **_: (_reddit_digest(), "archive", ("SecurityAnalysis",)),
            env=SLACK_ENV,
            client=client,
        )
        self.assertEqual(len(messages), 2)
        self.assertIn("PTT", messages[0])
        self.assertIn("Reddit", messages[1])
        client.conversations_history.assert_not_called()
        client.chat_postMessage.assert_not_called()

    def test_posts_when_configured(self):
        client = _slack_client(messages=[])
        messages = _run(client)
        self.assertEqual(client.chat_postMessage.call_count, 2)
        self.assertEqual(len(messages), 2)
        client.conversations_history.assert_called()

    def test_second_run_same_taiwan_digest_does_not_post(self):
        def boom(**_):
            raise AssertionError("already-sent run must not scrape forums")

        client = _slack_client(messages=[{"text": "*PTT 股板今日重點*\n已送過"}])
        messages = daily_digest.run(
            collect_ptt=boom,
            collect_reddit=boom,
            env=SLACK_ENV,
            client=client,
        )
        self.assertEqual(messages, [])
        client.chat_postMessage.assert_not_called()
        client.conversations_history.assert_called()

    def test_unrelated_slack_history_still_posts(self):
        client = _slack_client(messages=[{"text": "random channel chatter"}])
        messages = _run(client)
        self.assertEqual(client.chat_postMessage.call_count, 2)
        self.assertEqual(len(messages), 2)

    def test_history_error_is_not_already_sent(self):
        client = _slack_client(history_error=RuntimeError("missing_scope"))
        messages = _run(client)
        self.assertEqual(client.chat_postMessage.call_count, 2)
        self.assertEqual(len(messages), 2)

    def test_skips_slack_without_secrets(self):
        client = Mock()
        daily_digest.run(
            collect_ptt=lambda **_: _ptt_digest(),
            skip_reddit=True,
            env={},
            client=client,
        )
        client.conversations_history.assert_not_called()
        client.chat_postMessage.assert_not_called()

    def test_digest_date_groups_backup_with_evening(self):
        evening = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)  # 22:00 Taipei
        backup = datetime(2026, 9, 2, 17, 0, tzinfo=timezone.utc)  # 01:00 Taipei next day
        next_evening = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(daily_digest.digest_date(evening).isoformat(), "2026-09-02")
        self.assertEqual(daily_digest.digest_date(backup).isoformat(), "2026-09-02")
        self.assertEqual(daily_digest.digest_date(next_evening).isoformat(), "2026-09-03")

    def test_history_window_uses_taipei_digest_date(self):
        client = _slack_client(messages=[])
        backup = datetime(2026, 9, 2, 17, 0, tzinfo=timezone.utc)  # 01:00 Taipei Sep 3
        _run(client, now=backup)
        kwargs = client.conversations_history.call_args.kwargs
        oldest = datetime.fromtimestamp(float(kwargs["oldest"]), tz=timezone.utc)
        latest = datetime.fromtimestamp(float(kwargs["latest"]), tz=timezone.utc)
        self.assertEqual(oldest.astimezone(TAIPEI).isoformat(), "2026-09-02T00:00:00+08:00")
        self.assertEqual(latest, backup)

    def test_source_failure_still_posts_the_other(self):
        def boom(**_):
            raise RuntimeError("ptt down")

        messages = daily_digest.run(
            dry_run=True,
            collect_ptt=boom,
            collect_reddit=lambda **_: (_reddit_digest(), "reddit", ("stocks",)),
        )
        self.assertIn("抓取失敗", messages[0])
        self.assertIn("Reddit", messages[1])


if __name__ == "__main__":
    unittest.main()
