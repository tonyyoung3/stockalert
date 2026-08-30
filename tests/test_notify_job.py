import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import notify_job


class TailTextTests(unittest.TestCase):
    def test_missing_file(self):
        self.assertEqual(notify_job.tail_text(Path("/no/such/log")), "(no log file)")
        self.assertEqual(notify_job.tail_text(None), "(no log file)")

    def test_keeps_last_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.log"
            path.write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
            tail = notify_job.tail_text(path, max_lines=3, max_chars=500)
            self.assertEqual(tail, "line 97\nline 98\nline 99")

    def test_clips_long_line_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.log"
            path.write_text("aaaa\n" + ("b" * 80) + "\ncccc", encoding="utf-8")
            tail = notify_job.tail_text(path, max_lines=10, max_chars=10)
            self.assertLessEqual(len(tail), 10)


class MessageTests(unittest.TestCase):
    def test_includes_run_link_and_fence(self):
        msg = notify_job.build_message(
            "市場資料更新失敗",
            "ohlc failed",
            "https://github.com/tonyyoung3/stockalert/actions/runs/1",
        )
        self.assertIn(":x: *市場資料更新失敗*", msg)
        self.assertIn("<https://github.com/tonyyoung3/stockalert/actions/runs/1|打開 Actions log>", msg)
        self.assertIn("```\nohlc failed\n```", msg)


class NotifyTests(unittest.TestCase):
    def test_skips_without_secrets(self):
        self.assertFalse(notify_job.configured({}))
        self.assertEqual(
            notify_job.notify("x", env={"SLACK_BOT_TOKEN": "t"}),
            "skipped",
        )

    def test_posts_and_uploads(self):
        client = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market-update.log"
            path.write_text("turso push failed\n", encoding="utf-8")
            result = notify_job.notify(
                "市場資料更新失敗",
                log_path=path,
                run_url="https://example.test/run/9",
                env={"SLACK_BOT_TOKEN": "x", "SLACK_CHANNEL": "#alerts"},
                client=client,
            )
        self.assertEqual(result, "sent")
        client.chat_postMessage.assert_called_once()
        kwargs = client.chat_postMessage.call_args.kwargs
        self.assertEqual(kwargs["channel"], "#alerts")
        self.assertIn("turso push failed", kwargs["text"])
        client.files_upload_v2.assert_called_once()

    def test_message_only_when_log_missing(self):
        client = Mock()
        result = notify_job.notify(
            "市場資料更新失敗",
            log_path=Path("/no/log"),
            env={"SLACK_BOT_TOKEN": "x", "SLACK_CHANNEL": "C1"},
            client=client,
        )
        self.assertEqual(result, "sent")
        client.files_upload_v2.assert_not_called()
        self.assertIn("(no log file)", client.chat_postMessage.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
