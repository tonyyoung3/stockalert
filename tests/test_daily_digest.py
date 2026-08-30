import unittest
from unittest.mock import Mock

from notify import daily_digest
from ptt.ptt_stock import ChatDay, Digest as PttDigest, Post as PttPost, Push, Theme as PttTheme
from reddit.ideas import Comment, Digest as RedditDigest, Post as RedditPost, Theme as RedditTheme


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
        messages = daily_digest.run(
            days=1,
            dry_run=True,
            collect_ptt=lambda **_: _ptt_digest(),
            collect_reddit=lambda **_: (_reddit_digest(), "archive", ("SecurityAnalysis",)),
        )
        self.assertEqual(len(messages), 2)
        self.assertIn("PTT", messages[0])
        self.assertIn("Reddit", messages[1])

    def test_posts_when_configured(self):
        client = Mock()
        messages = daily_digest.run(
            dry_run=False,
            collect_ptt=lambda **_: _ptt_digest(),
            collect_reddit=lambda **_: (_reddit_digest(), "reddit", ("stocks",)),
            env={"SLACK_BOT_TOKEN": "x", "SLACK_CHANNEL": "#alerts"},
            client=client,
        )
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
        client.chat_postMessage.assert_not_called()

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
