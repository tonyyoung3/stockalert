import unittest
from datetime import date, datetime, timezone

from reddit.ideas import (
    DEFAULT_SUBS,
    Post,
    archive_posts_url,
    build_digest,
    cluster_themes,
    collect_posts,
    comment_url,
    extract_tickers,
    format_digest,
    is_daily_thread,
    is_idea_post,
    listing_url,
    parse_archive_posts,
    parse_comments,
    parse_listing,
    parse_subs,
    theme_key,
)


def _utc(y, m, d, h=16) -> float:
    return datetime(y, m, d, h, tzinfo=timezone.utc).timestamp()


def _child(**kwargs):
    row = {
        "id": "abc",
        "title": "Why I still like $NVDA",
        "selftext": "Long thesis on data-center spend and inference.",
        "score": 120,
        "num_comments": 40,
        "author": "foo",
        "created_utc": _utc(2026, 8, 28),
        "permalink": "/r/SecurityAnalysis/comments/abc/nvda/",
        "url": "https://www.reddit.com/r/SecurityAnalysis/comments/abc/nvda/",
        "link_flair_text": "DD",
        "subreddit": "SecurityAnalysis",
        "stickied": False,
    }
    row.update(kwargs)
    return {"kind": "t3", "data": row}


SAMPLE_LISTING = {
    "data": {
        "after": "t3_next",
        "children": [
            _child(id="pin", title="Daily Discussion Thread", score=900, stickied=True),
            _child(),
            _child(
                id="daily",
                title="Daily Discussion — August 28",
                score=800,
                link_flair_text="",
                selftext="",
                permalink="/r/investing/comments/daily/",
                subreddit="investing",
            ),
            _child(
                id="low",
                title="Hot take on $AAPL",
                score=3,
                link_flair_text="DD",
                permalink="/r/stocks/comments/low/",
                subreddit="stocks",
            ),
        ],
    }
}

OLDER_LISTING = {
    "data": {
        "after": None,
        "children": [
            _child(
                id="old",
                title="Old $TSLA writeup",
                created_utc=_utc(2026, 8, 1),
                permalink="/r/SecurityAnalysis/comments/old/",
            ),
        ],
    }
}

COMMENT_PAYLOAD = [
    {"data": {"children": []}},
    {
        "data": {
            "children": [
                {
                    "kind": "t1",
                    "data": {
                        "author": "aa",
                        "score": 40,
                        "body": "The $NVDA multiple is high but cash flow covers it if inference holds.",
                    },
                },
                {
                    "kind": "t1",
                    "data": {
                        "author": "AutoModerator",
                        "score": 1,
                        "body": "Please keep it civil and cite sources when you can.",
                    },
                },
                {
                    "kind": "t1",
                    "data": {"author": "bb", "score": 2, "body": "nice"},
                },
                {
                    "kind": "t1",
                    "data": {
                        "author": "cc",
                        "score": 12,
                        "body": "I would wait for a pullback before adding another tranche here.",
                    },
                },
            ]
        }
    },
]


class RedditIdeasTests(unittest.TestCase):
    def test_extract_tickers(self):
        self.assertEqual(extract_tickers("Long $NVDA and (AAPL)"), ["NVDA", "AAPL"])
        self.assertEqual(extract_tickers("Nvidia and TSMC still win"), ["NVDA", "TSM"])
        self.assertNotIn("THE", extract_tickers("THE FED raised $PE"))
        self.assertEqual(extract_tickers("no tickers here"), [])

    def test_daily_thread(self):
        self.assertTrue(is_daily_thread("Daily Discussion Thread"))
        self.assertTrue(is_daily_thread("What Are Your Moves Tomorrow"))
        self.assertFalse(is_daily_thread("Why I still like $NVDA"))

    def test_parse_listing_skips_stickied(self):
        posts, after = parse_listing(SAMPLE_LISTING)
        ids = [p.post_id for p in posts]
        self.assertEqual(ids, ["abc", "daily", "low"])
        self.assertEqual(after, "t3_next")
        self.assertTrue(posts[0].url.startswith("https://www.reddit.com/"))

    def test_collect_filters_week_score_and_daily(self):
        pages = {
            listing_url("SecurityAnalysis"): SAMPLE_LISTING,
            listing_url("SecurityAnalysis", after="t3_next"): OLDER_LISTING,
        }
        posts, source = collect_posts(
            days=7,
            min_score=40,
            subs=("SecurityAnalysis",),
            today=date(2026, 8, 30),
            fetch=pages.__getitem__,
            sleep_s=0,
        )
        ids = {p.post_id for p in posts}
        self.assertEqual(ids, {"abc"})
        self.assertEqual(posts[0].score, 120)
        self.assertEqual(source, "reddit")

    def test_theme_key_uses_ticker(self):
        post = Post(
            "2026-08-28",
            120,
            10,
            "Still long Nvidia",
            "foo",
            "http://x",
            "/r/x",
            "investing",
            "DD",
            "",
            "x",
        )
        self.assertEqual(theme_key(post), "NVDA")

    def test_cluster_merges_same_ticker(self):
        posts = [
            Post("2026-08-29", 200, 10, "Long $NVDA", "a", "http://a", "/a", "stocks", "DD", "", "a"),
            Post("2026-08-28", 80, 4, "Nvidia inference note", "b", "http://b", "/b", "investing", "", "", "b"),
            Post("2026-08-27", 90, 3, "Why $AAPL is cheap", "c", "http://c", "/c", "investing", "Research", "", "c"),
        ]
        themes = cluster_themes(posts)
        keys = [t.key for t in themes]
        self.assertIn("NVDA", keys)
        self.assertIn("AAPL", keys)
        nvda = next(t for t in themes if t.key == "NVDA")
        self.assertEqual(len(nvda.posts), 2)

    def test_parse_comments_skips_bots_and_short(self):
        comments = parse_comments(COMMENT_PAYLOAD, limit=5)
        authors = [c.author for c in comments]
        self.assertEqual(authors, ["aa", "cc"])
        self.assertIn("NVDA", comments[0].body)

    def test_build_digest_uses_injected_comments(self):
        post = Post(
            "2026-08-28",
            120,
            40,
            "Why I still like $NVDA",
            "foo",
            "https://www.reddit.com/r/SecurityAnalysis/comments/abc/nvda/",
            "/r/SecurityAnalysis/comments/abc/nvda/",
            "SecurityAnalysis",
            "DD",
            "Long thesis on data-center spend and inference demand.",
            "abc",
        )
        pages = {comment_url(post): COMMENT_PAYLOAD}
        digest = build_digest([post], fetch=pages.__getitem__, sleep_s=0)
        text = format_digest(digest, 7, 40, ("SecurityAnalysis",))
        self.assertIn("## 題材", text)
        self.assertIn("## DD / 標的", text)
        self.assertIn("精選留言", text)
        self.assertIn("NVDA", text)
        self.assertIn("aa", text)

    def test_parse_archive_and_fallback(self):
        archive = {
            "data": [
                {
                    "id": "arc1",
                    "title": "Archive $MSFT thesis",
                    "selftext": "Cloud and office still print cash.",
                    "score": 88,
                    "num_comments": 12,
                    "author": "arc",
                    "created_utc": _utc(2026, 8, 27),
                    "permalink": "/r/SecurityAnalysis/comments/arc1/msft/",
                    "link_flair_text": "Research",
                    "subreddit": "SecurityAnalysis",
                    "stickied": False,
                }
            ]
        }
        posts, after = parse_archive_posts(archive)
        self.assertEqual([p.post_id for p in posts], ["arc1"])
        self.assertTrue(after)

        def fake(url: str):
            if "photon-reddit.com" in url:
                return archive
            err = __import__("requests").HTTPError("403")
            err.response = type("R", (), {"status_code": 403})()
            raise err

        posts, source = collect_posts(
            days=7,
            min_score=40,
            subs=("SecurityAnalysis",),
            today=date(2026, 8, 30),
            fetch=fake,
            sleep_s=0,
        )
        self.assertEqual(source, "archive")
        self.assertEqual([p.post_id for p in posts], ["arc1"])
        self.assertIn("SecurityAnalysis", archive_posts_url("SecurityAnalysis", after="2026-08-23"))

    def test_wsb_keeps_dd_drops_memes(self):
        dd = Post(
            "2026-08-29", 1, 20, "Why $NVDA still prints", "a",
            "http://a", "/a", "wallstreetbets", "DD", "long thesis " * 20, "w1",
        )
        meme = Post(
            "2026-08-29", 9000, 400, "stonks", "b",
            "http://b", "/b", "wallstreetbets", "Meme", "", "w2",
        )
        self.assertTrue(is_idea_post(dd, min_score=40))
        self.assertFalse(is_idea_post(meme, min_score=40))

    def test_parse_subs(self):
        self.assertEqual(parse_subs("r/investing, stocks"), ("investing", "stocks"))
        self.assertEqual(parse_subs("research"), ("research",))
        self.assertIn("wallstreetbets", DEFAULT_SUBS)
        self.assertIn("wallstreetbets", parse_subs(None))


if __name__ == "__main__":
    unittest.main()
