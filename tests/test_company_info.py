import os
import unittest
from unittest.mock import patch

from notify.company_info import (
    CompanyProfile,
    fetch_profile,
    format_digest,
    format_slack_caption,
    maybe_enrich_themes,
    profile_from_info,
)
from notify.screener import chart_blocks, post_alert_charts


class CompanyInfoTests(unittest.TestCase):
    def test_profile_from_info(self):
        profile = profile_from_info(
            "2330.TW",
            {
                "shortName": "TSMC",
                "industry": "Semiconductors",
                "sector": "Technology",
                "longBusinessSummary": "World's largest dedicated foundry. Advanced nodes and packaging.",
            },
        )
        self.assertEqual(profile.ticker, "2330")
        self.assertEqual(profile.name, "TSMC")
        self.assertEqual(profile.industry, "Semiconductors")
        self.assertIn("foundry", profile.theme)

    def test_fetch_profile_swallows_errors(self):
        profile = fetch_profile("2330", info_loader=lambda _s: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(profile.ticker, "2330")
        self.assertEqual(profile.error, "boom")
        self.assertIsNone(profile.theme)

    def test_caption_and_digest(self):
        profile = CompanyProfile(
            ticker="2330",
            symbol="2330.TW",
            name="TSMC",
            industry="Semiconductors",
            theme="先進製程與先進封裝",
        )
        caption = format_slack_caption(profile)
        self.assertIn("*2330*", caption)
        self.assertIn("TSMC", caption)
        self.assertIn("產業: Semiconductors", caption)
        self.assertIn("題材: 先進製程與先進封裝", caption)

        digest = format_digest([(profile, "上影線反轉")])
        self.assertIn("今日訊號 1 檔", digest)
        self.assertIn("2330 — TSMC — Semiconductors — 上影線反轉", digest)
        self.assertIn("@Cursor", digest)

    def test_post_alert_charts_includes_theme(self):
        posted_messages = []
        uploads = []

        class FakeClient:
            def chat_postMessage(self, **kwargs):
                posted_messages.append(kwargs)

            def files_upload_v2(self, **kwargs):
                uploads.append(kwargs)
                return {"file": {"id": "F1"}}

        profile = CompanyProfile(ticker="2330", symbol="2330.TW", name="TSMC", industry="Semiconductors", theme="先進封裝")
        posted = post_alert_charts(
            FakeClient(),
            "C123",
            "heading",
            [("2330", "unused.png")],
            {"2330": profile},
            "上影線反轉",
        )
        self.assertEqual(len(posted), 1)
        self.assertTrue(any("先進封裝" in (u.get("initial_comment") or "") for u in uploads))
        self.assertEqual(posted_messages[0]["text"], "heading")

    def test_post_alert_charts_falls_back_to_text(self):
        from slack_sdk.errors import SlackApiError

        posted_messages = []

        class FakeClient:
            def chat_postMessage(self, **kwargs):
                posted_messages.append(kwargs)

            def files_upload_v2(self, **kwargs):
                raise SlackApiError("fail", {"error": "not_allowed_token_type"})

        profile = CompanyProfile(ticker="2308", symbol="2308.TW", name="Delta", theme="電源")
        posted = post_alert_charts(
            FakeClient(),
            "C123",
            "heading",
            [("2308", "unused.png")],
            {"2308": profile},
            "上影線反轉",
        )
        self.assertEqual(len(posted), 1)
        texts = [m.get("text") for m in posted_messages]
        self.assertTrue(any(t and "2308" in t and "電源" in t for t in texts))

    def test_chart_blocks_use_caption(self):
        blocks = chart_blocks("標的: *2330*  TSMC", "https://example.com/x.png", "2330 - 上影線反轉")
        self.assertEqual(blocks[0]["text"]["text"], "標的: *2330*  TSMC")
        self.assertEqual(blocks[1]["image_url"], "https://example.com/x.png")

    def test_enrich_themes_uses_completer(self):
        profile = CompanyProfile(ticker="2330", symbol="2330.TW", name="TSMC", theme="long english blurb")
        maybe_enrich_themes(
            [profile],
            completer=lambda _prompt: '{"2330": "晶圓代工龍頭，先進製程與 CoWoS"}',
        )
        self.assertEqual(profile.theme, "晶圓代工龍頭，先進製程與 CoWoS")

    def test_enrich_themes_skips_without_key_or_completer(self):
        profile = CompanyProfile(ticker="2330", symbol="2330.TW", theme="keep me")
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            maybe_enrich_themes([profile], completer=None)
        self.assertEqual(profile.theme, "keep me")


if __name__ == "__main__":
    unittest.main()
