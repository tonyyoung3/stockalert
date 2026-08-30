import os
import unittest
from unittest.mock import patch

from company_info import (
    CompanyProfile,
    fetch_profile,
    format_digest,
    format_slack_caption,
    maybe_enrich_themes,
    profile_from_info,
)
from screener import chart_blocks, post_alert_charts


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

        class FakeClient:
            def chat_postMessage(self, **kwargs):
                posted_messages.append(kwargs)

            def files_upload_v2(self, **kwargs):
                return {"file": {"id": "F1"}}

            def files_sharedPublicURL(self, **kwargs):
                return {"ok": True, "file": {"permalink_public": "https://example.com/chart.png"}}

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
        captions = [m.get("text") for m in posted_messages]
        self.assertTrue(any(c and "TSMC" in c and "先進封裝" in c for c in captions))

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
