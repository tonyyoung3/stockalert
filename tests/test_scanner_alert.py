"""Daily scanner-alert job: hits → alerts + Slack; failures recorded (#86)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from alertsdb import store as db
from data import market_db
from market import collector
from notify import scanner_alert
from notify import scanner_profile as profile
from web.tw_calendar import taiwan_today


class ScannerAlertJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.market_path = Path(self.tmp.name) / "twse.db"
        self.screener = Path(self.tmp.name) / "screener.db"
        self._orig = collector.DB_PATH
        collector.DB_PATH = self.market_path
        self.conn = collector.get_conn()
        market_db.set_db_path(self.market_path)
        db.set_db_path(self.screener)
        db.init_db()

    def tearDown(self):
        self.conn.close()
        collector.DB_PATH = self._orig
        market_db.set_db_path(None)
        db.set_db_path(None)
        self.tmp.cleanup()

    def _price(self, day, stock_id, name, close, volume=100, turnover=1000):
        self.conn.execute(
            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?)",
            (day, stock_id, name, close - 1, close + 1, close - 2, close, volume, turnover),
        )

    def _chips(self, table, day, stock_id, name, buy, sell, net):
        self.conn.execute(
            f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?)",
            (day, stock_id, name, buy, sell, net),
        )

    def _days(self, n, start="2026-08-01"):
        from datetime import date, timedelta
        y, m, d = (int(x) for x in start.split("-"))
        first = date(y, m, d)
        return [(first + timedelta(days=i)).isoformat() for i in range(n)]

    def _load_series(self, stock_id, name, nets, start="2026-08-01"):
        days = self._days(len(nets), start)
        for day, net in zip(days, nets):
            self._price(day, stock_id, name, 100.0 + net, 1_000_000, 100_000_000)
            buy = max(net, 0) + 10
            sell = buy - net
            self._chips("foreign_daily", day, stock_id, name, buy, sell, net)
            self._chips("trust_daily", day, stock_id, name, 1, 0, 1)
            self._chips("dealer_daily", day, stock_id, name, 2, 1, 1)
        self.conn.commit()
        return days

    def _profile_path(self, **kwargs):
        body = {
            "tickers": ["2330", "2454"],
            "window": 5,
            "min_periods": 5,
            "field": "foreign_net_z",
            "min": 1.2,
        }
        body.update(kwargs)
        path = Path(self.tmp.name) / "profile.json"
        profile.write_profile_file(body, path)
        return path

    def test_hits_write_alert_row_and_slack(self):
        days = self._load_series("2330", "台積電", [10, 20, 30, 40, 50])
        self._load_series("2454", "聯發科", [50, 40, 30, 20, 10])
        client = Mock()
        result = scanner_alert.run(
            profile_path=str(self._profile_path()),
            market_conn=self.conn,
            env={"SLACK_BOT_TOKEN": "x", "SLACK_CHANNEL": "C1"},
            client=client,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual([h["stock_id"] for h in result["hits"]], ["2330"])
        self.assertTrue(db.has_alert("2330", "scanner_foreign_net_z", days[-1]))
        self.assertFalse(db.has_alert("2454", "scanner_foreign_net_z", days[-1]))
        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        self.assertIn("掃描每日告警", text)
        self.assertIn("2330", text)
        self.assertNotIn("2454", text)
        with db.get_conn() as conn:
            last = profile.last_run(conn)
        self.assertEqual(last["status"], "ok")
        self.assertEqual(last["hit_count"], 1)

    def test_empty_screen_still_notifies(self):
        self._load_series("2330", "台積電", [10, 20, 30, 40, 41])
        self._load_series("2454", "聯發科", [2, 4, 6, 8, 9])
        client = Mock()
        result = scanner_alert.run(
            profile_path=str(self._profile_path(min=9)),
            market_conn=self.conn,
            env={"SLACK_BOT_TOKEN": "x", "SLACK_CHANNEL": "C1"},
            client=client,
        )
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["hits"], [])
        self.assertIn("沒有符合的標的", client.chat_postMessage.call_args.kwargs["text"])

    def test_duplicate_asof_skips_second_slack(self):
        self._load_series("2330", "台積電", [10, 20, 30, 40, 50])
        self._load_series("2454", "聯發科", [50, 40, 30, 20, 10])
        path = self._profile_path()
        env = {"SLACK_BOT_TOKEN": "x", "SLACK_CHANNEL": "C1"}
        first = Mock()
        scanner_alert.run(
            profile_path=str(path), market_conn=self.conn, env=env, client=first,
        )
        second = Mock()
        result = scanner_alert.run(
            profile_path=str(path), market_conn=self.conn, env=env, client=second,
        )
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["skipped_duplicates"], 1)
        second.chat_postMessage.assert_not_called()

    def test_dry_run_does_not_write_alerts_or_slack(self):
        self._load_series("2330", "台積電", [10, 20, 30, 40, 50])
        self._load_series("2454", "聯發科", [50, 40, 30, 20, 10])
        client = Mock()
        result = scanner_alert.run(
            profile_path=str(self._profile_path()),
            market_conn=self.conn,
            dry_run=True,
            env={"SLACK_BOT_TOKEN": "x", "SLACK_CHANNEL": "C1"},
            client=client,
        )
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["dry_run"])
        self.assertFalse(db.has_alert(
            "2330", "scanner_foreign_net_z", result["asof"],
        ))
        client.chat_postMessage.assert_not_called()

    def test_disabled_and_empty_profile_are_observable(self):
        path = self._profile_path(enabled=False)
        skipped = scanner_alert.run(profile_path=str(path), market_conn=self.conn)
        self.assertEqual(skipped["status"], "skipped")
        empty = scanner_alert.run(
            profile_path=str(self._profile_path(tickers=[])),
            market_conn=self.conn,
        )
        self.assertEqual(empty["status"], "error")
        self.assertIn("no tickers", empty["error"])
        with db.get_conn() as conn:
            last = profile.last_run(conn)
        self.assertEqual(last["status"], "error")

    def test_missing_market_db_records_error(self):
        market_db.set_db_path(Path(self.tmp.name) / "missing.db")
        result = scanner_alert.run(
            profile_path=str(self._profile_path()),
            env={},
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("unavailable", result["error"])
        with db.get_conn() as conn:
            self.assertEqual(profile.last_run(conn)["status"], "error")

    def test_cli_save_writes_json_and_db(self):
        dest = Path(self.tmp.name) / "out.json"
        code = scanner_alert.main([
            "save",
            "--tickers", "2330,2454",
            "--field", "trust_net_z",
            "--min", "2",
            "--file", str(dest),
        ])
        self.assertEqual(code, 0)
        loaded = profile.profile_from_file(dest)
        self.assertEqual(loaded["tickers"], ["2330", "2454"])
        self.assertEqual(loaded["field"], "trust_net_z")
        with db.get_conn() as conn:
            row = profile.profile_from_conn(conn)
        self.assertEqual(row["min"], 2.0)

    def test_run_date_uses_taipei_helper(self):
        self.assertEqual(taiwan_today().__class__.__name__, "date")

    def test_workflow_extends_existing_screener_job(self):
        yml = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/run_screener.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("python -m notify.scanner_alert", yml)
        self.assertIn("python -m notify.notify_job", yml)
        self.assertIn("掃描每日告警失敗", yml)
        self.assertIn("twse_data.db", yml)
        self.assertIn("python -m notify.screener", yml)


if __name__ == "__main__":
    unittest.main()
