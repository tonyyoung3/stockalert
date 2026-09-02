import unittest
from unittest.mock import Mock, patch

from notify.screener import format_empty_screener_message, pick_scan_date, post_screener_results


class FakeClient:
    def __init__(self):
        self.messages = []

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)


class EmptyScreenerNotifyTests(unittest.TestCase):
    def test_empty_message_includes_date(self):
        self.assertEqual(
            format_empty_screener_message("2026-09-01"),
            "今日台股篩選（2026-09-01）沒有符合的標的。",
        )

    def test_empty_message_without_date(self):
        self.assertEqual(
            format_empty_screener_message(),
            "今日台股篩選沒有符合的標的。",
        )

    def test_empty_message_for_already_notified_hits(self):
        self.assertEqual(
            format_empty_screener_message("2026-09-01", skipped_duplicates=3),
            "今日台股篩選（2026-09-01）沒有新的符合標的（先前已通知）。",
        )

    def test_pick_scan_date_uses_most_common_complete_bar(self):
        self.assertIsNone(pick_scan_date([]))
        self.assertEqual(
            pick_scan_date(["2026-09-01", "2026-09-02", "2026-09-02"]),
            "2026-09-02",
        )

    def test_post_screener_results_sends_empty_notice(self):
        client = FakeClient()
        posted = post_screener_results(
            client,
            "C123",
            [],
            [],
            {},
            signal_date="2026-09-01",
        )
        self.assertEqual(posted, [])
        self.assertEqual(len(client.messages), 1)
        self.assertEqual(
            client.messages[0]["text"],
            "今日台股篩選（2026-09-01）沒有符合的標的。",
        )

    def test_post_screener_results_sends_already_notified_notice(self):
        client = FakeClient()
        post_screener_results(
            client,
            "C123",
            [],
            [],
            {},
            signal_date="2026-09-01",
            skipped_duplicates=2,
        )
        self.assertIn("沒有新的符合標的", client.messages[0]["text"])

    def test_post_screener_results_does_not_send_empty_when_hits_exist(self):
        client = FakeClient()

        def fake_charts(_client, _channel, _heading, hits, _profiles, pattern_title):
            if not hits:
                return []
            return [(Mock(), pattern_title)]

        with patch("notify.screener.post_alert_charts", side_effect=fake_charts) as charts, patch(
            "notify.screener.format_digest",
            return_value="今日訊號 1 檔",
        ):
            posted = post_screener_results(
                client,
                "C123",
                [("2330", "chart.png")],
                [],
                {},
                signal_date="2026-09-01",
            )
        self.assertEqual(len(posted), 1)
        charts.assert_called()
        texts = [m.get("text") for m in client.messages]
        self.assertTrue(any(t == "今日訊號 1 檔" for t in texts))
        self.assertFalse(any(t and "沒有符合的標的" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
