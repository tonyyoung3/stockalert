"""Structured scanner-alert profile — no DSL (#86)."""
import json
import tempfile
import unittest
from pathlib import Path

from alertsdb import store as db
from notify import scanner_profile as profile


class ParseProfileTests(unittest.TestCase):
    def test_defaults_and_ticker_dedupe(self):
        p = profile.parse_profile({"tickers": "2330, 2454,2330"})
        self.assertEqual(p["tickers"], ["2330", "2454"])
        self.assertEqual(p["window"], 20)
        self.assertEqual(p["min_periods"], 20)
        self.assertEqual(p["field"], "foreign_net_z")
        self.assertEqual(p["min"], 1.5)
        self.assertIsNone(p["max"])
        self.assertTrue(p["enabled"])

    def test_rejects_dsl_keys(self):
        for key in ("expr", "sql", "where", "dsl", "formula"):
            with self.assertRaises(ValueError) as ctx:
                profile.parse_profile({"tickers": ["2330"], key: "foreign_net_z > 2"})
            self.assertIn("unsupported_condition", str(ctx.exception))

    def test_rejects_unknown_field(self):
        with self.assertRaises(ValueError) as ctx:
            profile.parse_profile({"tickers": ["2330"], "field": "rsi_custom"})
        self.assertIn("unknown_field", str(ctx.exception))

    def test_clamps_window_and_requires_a_bound(self):
        p = profile.parse_profile({
            "tickers": ["2330"],
            "window": 9999,
            "min_periods": 1,
            "field": "trust_net_z",
        })
        self.assertEqual(p["window"], 252)
        self.assertEqual(p["min_periods"], 2)
        self.assertEqual(p["field"], "trust_net_z")
        self.assertEqual(p["min"], 1.5)


class RowHitTests(unittest.TestCase):
    def test_min_max_and_insufficient(self):
        p = profile.parse_profile({
            "tickers": ["2330"],
            "field": "foreign_net_z",
            "min": 1.5,
            "max": 3,
        })
        self.assertTrue(profile.row_hits(
            {"foreign_net_z": 2.0, "insufficient_sample": False}, p,
        ))
        self.assertFalse(profile.row_hits(
            {"foreign_net_z": 1.0, "insufficient_sample": False}, p,
        ))
        self.assertFalse(profile.row_hits(
            {"foreign_net_z": 4.0, "insufficient_sample": False}, p,
        ))
        self.assertFalse(profile.row_hits(
            {"foreign_net_z": 2.0, "insufficient_sample": True}, p,
        ))
        self.assertFalse(profile.row_hits(
            {"foreign_net_z": None, "insufficient_sample": False}, p,
        ))

    def test_pattern_type(self):
        self.assertEqual(
            profile.pattern_type("foreign_net_z"),
            "scanner_foreign_net_z",
        )
        self.assertEqual(
            profile.describe_condition({"field": "foreign_net_z", "min": 1.5, "max": None}),
            "foreign_net_z >= 1.5",
        )


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self.tmp.name) / "screener.db")
        db.init_db()

    def tearDown(self):
        db.set_db_path(None)
        self.tmp.cleanup()

    def test_roundtrip_file_and_db(self):
        path = Path(self.tmp.name) / "p.json"
        body = profile.parse_profile({
            "tickers": ["2330", "2317"],
            "field": "dealer_net_z",
            "min": 2,
        })
        profile.write_profile_file(body, path)
        loaded = profile.profile_from_file(path)
        self.assertEqual(loaded["tickers"], ["2330", "2317"])
        self.assertEqual(loaded["field"], "dealer_net_z")

        with db.get_conn() as conn:
            saved = profile.save_profile_conn(conn, body)
            self.assertEqual(saved["tickers"], ["2330", "2317"])
            again = profile.profile_from_conn(conn)
            self.assertEqual(again["min"], 2.0)
            profile.record_run(
                conn, run_date="2026-09-05", asof="2026-09-04",
                status="ok", hit_count=1,
            )
            last = profile.last_run(conn)
            self.assertEqual(last["status"], "ok")
            self.assertEqual(last["hit_count"], 1)
            self.assertTrue(profile.has_run_for(conn, "2026-09-04"))
            self.assertFalse(profile.has_run_for(conn, "2026-09-03"))

    def test_repo_default_file_is_valid(self):
        raw = json.loads(profile.DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))
        parsed = profile.parse_profile(raw)
        self.assertGreaterEqual(len(parsed["tickers"]), 2)
        self.assertIn(parsed["field"], profile.ALLOWED_FIELDS)


if __name__ == "__main__":
    unittest.main()
