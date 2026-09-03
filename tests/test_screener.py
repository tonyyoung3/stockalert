import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from notify.screener import (
    already_sent_screener,
    format_empty_screener_message,
    is_screener_message,
    pick_scan_date,
    post_screener_results,
)


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


class ScreenerAlreadySentTests(unittest.TestCase):
    def test_empty_notice_matches_scan_date(self):
        self.assertTrue(
            is_screener_message("今日台股篩選（2026-09-03）沒有符合的標的。", "2026-09-03")
        )
        self.assertFalse(
            is_screener_message("今日台股篩選（2026-09-02）沒有符合的標的。", "2026-09-03")
        )
        self.assertTrue(is_screener_message("--- 🔺 台股篩選結果：上影線反轉 (Upper Shadow Reversal) ---"))

    def test_already_sent_reads_slack_history(self):
        client = Mock()
        client.conversations_history.return_value = {
            "ok": True,
            "messages": [{"text": "今日台股篩選（2026-09-03）沒有符合的標的。"}],
        }
        now = datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc)
        self.assertTrue(already_sent_screener(client, "C123", "2026-09-03", now=now))
        kwargs = client.conversations_history.call_args.kwargs
        oldest = datetime.fromtimestamp(float(kwargs["oldest"]), tz=timezone.utc)
        self.assertEqual(
            oldest.astimezone(ZoneInfo("Asia/Taipei")).isoformat(),
            "2026-09-03T00:00:00+08:00",
        )

    def test_already_sent_false_on_history_error(self):
        client = Mock()
        client.conversations_history.side_effect = RuntimeError("missing_scope")
        self.assertFalse(already_sent_screener(client, "C123", "2026-09-03"))


if __name__ == "__main__":
    unittest.main()
