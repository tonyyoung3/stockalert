#!/usr/bin/env python3
"""FinMind institutional mapper + gap-fill fallback (T86 JSON/HTML fail)."""
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from market import backfill, collector
from market.broker_branch import FinMindError
from market import finmind_institutional as fmi

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class MapperTests(unittest.TestCase):
    def test_long_maps_trust_and_dealer_sum(self):
        payload = _load_fixture("finmind_institutional_long.json")
        tables = fmi.map_finmind_payload(
            payload, date(2026, 1, 5), names={"2330": "台積電", "2317": "鴻海"},
        )
        self.assertEqual(tables.foreign, [])
        by_trust = {r[1]: r for r in tables.trust}
        self.assertEqual(by_trust["2330"], ("2026-01-05", "2330", "台積電", 900000, 239000, 661000))
        self.assertEqual(by_trust["2317"], ("2026-01-05", "2317", "鴻海", 10000, 4000, 6000))
        by_dealer = {r[1]: r for r in tables.dealer}
        # Dealer + Dealer_self + Dealer_Hedging (T86 合計含避險)
        self.assertEqual(
            by_dealer["2330"],
            ("2026-01-05", "2330", "台積電", 79000 + 189000, 807000 + 493500, -1_032_500),
        )
        self.assertEqual(
            by_dealer["2330"][3] - by_dealer["2330"][4],
            by_dealer["2330"][5],
        )
        self.assertEqual(by_dealer["2317"][3:], (5000, 1500, 3500))

    def test_wide_matches_long(self):
        long_t = fmi.map_finmind_payload(
            _load_fixture("finmind_institutional_long.json"), date(2026, 1, 5),
        )
        wide_t = fmi.map_finmind_payload(
            _load_fixture("finmind_institutional_wide.json"), date(2026, 1, 5),
        )
        self.assertEqual(
            {r[1]: r[3:] for r in long_t.trust if r[1] in ("2330", "2317")},
            {r[1]: r[3:] for r in wide_t.trust},
        )
        self.assertEqual(
            {r[1]: r[3:] for r in long_t.dealer if r[1] in ("2330", "2317")},
            {r[1]: r[3:] for r in wide_t.dealer},
        )

    def test_old_era_dealer_combined_only(self):
        tables = fmi.map_long_rows(
            _load_fixture("finmind_institutional_long.json")["data"],
            date(2014, 11, 3),
        )
        self.assertEqual(tables.trust[0][3:], (500, 100, 400))
        self.assertEqual(tables.dealer[0][3:], (8000, 2000, 6000))

    def test_listed_filter_drops_otc(self):
        tables = fmi.map_finmind_payload(
            _load_fixture("finmind_institutional_long.json"),
            date(2026, 1, 5),
            listed_ids={"2330", "2317"},
        )
        self.assertNotIn("6488", {r[1] for r in tables.trust})
        self.assertIn("2330", {r[1] for r in tables.trust})

    def test_ignores_other_dates(self):
        tables = fmi.map_finmind_payload(
            _load_fixture("finmind_institutional_long.json"),
            date(2026, 1, 5),
        )
        self.assertTrue(all(r[0] == "2026-01-05" for r in tables.trust + tables.dealer))

    def test_string_numbers_and_commas(self):
        tables = fmi.map_long_rows(
            [{"date": "2026-01-05", "stock_id": "2330",
              "name": "Investment_Trust", "buy": "1,000", "sell": "200"}],
            date(2026, 1, 5),
        )
        self.assertEqual(tables.trust[0][3:], (1000, 200, 800))


class FetchClientTests(unittest.TestCase):
    def test_all_stocks_omits_data_id(self):
        captured = {}

        class Resp:
            status_code = 200
            def json(self):
                return {"status": 200, "msg": "success", "data": []}
            def raise_for_status(self):
                return None

        def fake_get(url, params=None, headers=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return Resp()

        sess = MagicMock()
        sess.get.side_effect = fake_get
        rows = fmi.fetch_institutional_day(
            date(2026, 1, 5), "secret-token", session=sess, sleep=lambda _: None,
        )
        self.assertEqual(rows, [])
        self.assertEqual(captured["url"], fmi.FINMIND_DATA_URL)
        self.assertEqual(captured["params"]["dataset"], fmi.DATASET_LONG)
        self.assertEqual(captured["params"]["start_date"], "2026-01-05")
        self.assertNotIn("data_id", captured["params"])
        self.assertIn("Bearer", captured["headers"]["Authorization"])
        self.assertNotIn("secret-token", str(captured["params"]))

    def test_per_stock_sets_data_id_and_docs_pacing(self):
        self.assertIn("data_id", fmi.PER_STOCK_PACING)
        self.assertIn("6000", fmi.PER_STOCK_PACING)

        class Resp:
            status_code = 200
            def json(self):
                return {"status": 200, "msg": "success", "data": []}

        sess = MagicMock()
        sess.get.return_value = Resp()
        fmi.fetch_institutional_day(
            date(2026, 1, 5), "secret", stock_id="2330",
            session=sess, sleep=lambda _: None,
        )
        params = sess.get.call_args.kwargs["params"]
        self.assertEqual(params["data_id"], "2330")

    def test_all_stocks_denied_mentions_per_stock_pacing(self):
        class Resp:
            status_code = 400
            def json(self):
                return {"status": 400, "msg": "data_id is required"}

        sess = MagicMock()
        sess.get.return_value = Resp()
        with self.assertRaises(FinMindError) as ctx:
            fmi.fetch_institutional_day(
                date(2026, 1, 5), "secret", session=sess, sleep=lambda _: None,
            )
        self.assertIn("PER_STOCK" in fmi.PER_STOCK_PACING or "data_id", str(ctx.exception))
        self.assertIn("data_id", str(ctx.exception))
        self.assertNotIn("secret", str(ctx.exception))

    def test_missing_token_refuses(self):
        with self.assertRaises(FinMindError) as ctx:
            fmi.fetch_institutional_day(date(2026, 1, 5), "")
        self.assertIn("FINMIND_TOKEN", str(ctx.exception))

    def test_request_context_refuses_even_with_token(self):
        from market import broker_branch
        token = broker_branch.forbid_request_time_finmind()
        try:
            with self.assertRaises(FinMindError) as ctx:
                fmi.fetch_institutional_day(date(2026, 1, 5), "secret")
            self.assertIn("not allowed on dashboard/API request", str(ctx.exception))
        finally:
            broker_branch.reset_request_time_finmind(token)

    def test_auth_error_does_not_include_token(self):
        class Resp:
            status_code = 401
            def json(self):
                return {"msg": "unauthorized"}

        sess = MagicMock()
        sess.get.return_value = Resp()
        with self.assertRaises(FinMindError) as ctx:
            fmi.fetch_institutional_day(
                date(2026, 1, 5), "super-secret-token",
                session=sess, sleep=lambda _: None,
            )
        self.assertNotIn("super-secret-token", str(ctx.exception))

    def test_logs_never_include_token(self):
        class Resp:
            status_code = 503
            def json(self):
                return {}

        sess = MagicMock()
        sess.get.return_value = Resp()
        secret = "tok_should_never_appear_xyz"
        with self.assertLogs(fmi.log, level="WARNING") as cm:
            with self.assertRaises(FinMindError):
                fmi.fetch_institutional_day(
                    date(2026, 1, 5), secret, session=sess, sleep=lambda _: None,
                )
        text = "\n".join(cm.output)
        self.assertNotIn(secret, text)


class T86ParseErrorTests(unittest.TestCase):
    def test_empty_body_raises_t86_fetch_error(self):
        resp = MagicMock()
        resp.text = ""
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("Expecting value: line 1 column 1")
        with patch.object(collector.requests, "get", return_value=resp):
            with self.assertRaises(collector.T86FetchError) as ctx:
                collector.fetch_t86(date(2026, 1, 5))
        self.assertIn("2026-01-05", str(ctx.exception))

    def test_html_body_raises_t86_fetch_error(self):
        resp = MagicMock()
        resp.text = "<html>blocked</html>"
        resp.raise_for_status.return_value = None
        resp.json.side_effect = json.JSONDecodeError("Expecting value", "<html>", 0)
        with patch.object(collector.requests, "get", return_value=resp):
            with self.assertRaises(collector.T86FetchError):
                collector.fetch_t86(date(2026, 1, 5))


class GapFillFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "test.db"
        self._orig = collector.DB_PATH
        collector.DB_PATH = self.db

    def tearDown(self):
        collector.DB_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_gap(self):
        con = collector.get_conn()
        with con:
            con.execute(
                "INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                ("2026-01-05", "2330", "台積電", 1, 1, 0),
            )
        con.close()

    def _finmind_tables(self, day=date(2026, 1, 5)):
        ds = day.isoformat()
        return collector.T86Tables(
            [],
            [(ds, "2330", "台積電", 900000, 239000, 661000)],
            [(ds, "2330", "台積電", 268000, 1300500, -1032500)],
        )

    def test_t86_json_fail_falls_back_to_finmind(self):
        """Daily prefer=t86: JSON/HTML fail → FinMind. Gap fill with token is FinMind-first."""
        seen = {"t86": 0, "fm": 0}

        def boom(_day):
            seen["t86"] += 1
            raise collector.T86FetchError("T86 JSON parse failed (JSONDecodeError)")

        def fm(_day):
            seen["fm"] += 1
            return self._finmind_tables()

        fetched = backfill.fetch_institutional_day(
            date(2026, 1, 5),
            prefer="t86",
            env={"FINMIND_TOKEN": "secret"},
            t86_fetch=boom,
            finmind_fetch=fm,
        )
        self.assertEqual(fetched.source, "finmind")
        self.assertEqual(fetched.status, "ok")
        self.assertIn("t86_error", fetched.reason)
        self.assertEqual(seen, {"t86": 1, "fm": 1})
        self.assertEqual(fetched.tables.trust[0][5], 661000)

        self._seed_gap()
        with patch.object(backfill.time, "sleep"):
            n = backfill.backfill_institutional_gaps(
                today=date(2026, 1, 5),
                env={"FINMIND_TOKEN": "secret"},
                t86_fetch=boom,
                finmind_fetch=fm,
            )
        self.assertEqual(n, 1)
        con = sqlite3.connect(self.db)
        try:
            trust = con.execute(
                "SELECT trust_buy, trust_sell, trust_net FROM trust_daily "
                "WHERE trade_date='2026-01-05' AND stock_id='2330'"
            ).fetchone()
            dealer = con.execute(
                "SELECT dealer_buy, dealer_sell, dealer_net FROM dealer_daily "
                "WHERE trade_date='2026-01-05' AND stock_id='2330'"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(trust, (900000, 239000, 661000))
        self.assertEqual(dealer, (268000, 1300500, -1032500))

    def test_token_present_uses_finmind_primary_skips_t86(self):
        self._seed_gap()

        def t86(_day):
            raise AssertionError("T86 must not run when FinMind is primary")

        with patch.object(backfill.time, "sleep"):
            n = backfill.backfill_institutional_gaps(
                today=date(2026, 1, 5),
                env={"FINMIND_TOKEN": "secret"},
                t86_fetch=t86,
                finmind_fetch=lambda d: self._finmind_tables(d),
            )
        self.assertEqual(n, 1)

    def test_no_token_stays_on_t86(self):
        self._seed_gap()
        seen = []

        def t86(day):
            seen.append(day)
            ds = day.isoformat()
            return collector.T86Tables(
                [(ds, "2330", "台積電", 1, 0, 1)],
                [(ds, "2330", "台積電", 3, 1, 2)],
                [(ds, "2330", "台積電", 8, 3, 5)],
            )

        def fm(_day):
            raise AssertionError("FinMind must not run without token")

        with patch.object(backfill.time, "sleep"):
            n = backfill.backfill_institutional_gaps(
                today=date(2026, 1, 5),
                env={},
                t86_fetch=t86,
                finmind_fetch=fm,
            )
        self.assertEqual(n, 1)
        self.assertEqual(seen, [date(2026, 1, 5)])

    def test_finmind_does_not_overwrite_foreign(self):
        self._seed_gap()
        with patch.object(backfill.time, "sleep"):
            backfill.backfill_institutional_gaps(
                today=date(2026, 1, 5),
                env={"FINMIND_TOKEN": "secret"},
                t86_fetch=lambda d: (_ for _ in ()).throw(AssertionError("no t86")),
                finmind_fetch=lambda d: self._finmind_tables(d),
            )
        con = sqlite3.connect(self.db)
        try:
            row = con.execute(
                "SELECT foreign_net FROM foreign_daily WHERE stock_id='2330'"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(row[0], 0)

    def test_pace_uses_finmind_sleep_when_finmind_writes(self):
        self._seed_gap()
        slept = []
        with patch.object(backfill.time, "sleep", side_effect=lambda s: slept.append(s)):
            backfill.backfill_institutional_gaps(
                today=date(2026, 1, 5),
                env={"FINMIND_TOKEN": "secret"},
                finmind_fetch=lambda d: self._finmind_tables(d),
            )
        self.assertEqual(slept, [fmi.FINMIND_SLEEP])
        self.assertLess(fmi.FINMIND_SLEEP, backfill.SLEEP)
        self.assertGreaterEqual(3600 / fmi.FINMIND_SLEEP, 3000)

    def test_token_not_logged_during_gap_fill(self):
        self._seed_gap()
        secret = "gap_fill_secret_token_zzz"
        with self.assertLogs(backfill.log, level="INFO") as cm:
            with patch.object(backfill.time, "sleep"):
                backfill.backfill_institutional_gaps(
                    today=date(2026, 1, 5),
                    env={"FINMIND_TOKEN": secret},
                    finmind_fetch=lambda d: self._finmind_tables(d),
                )
        text = "\n".join(cm.output)
        self.assertNotIn(secret, text)
        self.assertIn("token=present", text)

    def test_no_token_never_calls_finmind_even_on_t86_empty(self):
        fm = MagicMock(side_effect=AssertionError("must not call FinMind"))
        fetched = backfill.fetch_institutional_day(
            date(2026, 1, 5),
            prefer="finmind",
            env={},
            t86_fetch=lambda d: collector.EMPTY_T86,
            finmind_fetch=fm,
        )
        fm.assert_not_called()
        self.assertEqual(fetched.source, "t86")
        self.assertEqual(fetched.status, "empty")
        self.assertNotEqual(fetched.status, "ok")

    def test_no_token_t86_error_is_not_success(self):
        fm = MagicMock(side_effect=AssertionError("must not call FinMind"))

        def boom(_day):
            raise collector.T86FetchError("Expecting value: line 1 column 1")

        fetched = backfill.fetch_institutional_day(
            date(2026, 1, 5),
            prefer="t86",
            env={},
            t86_fetch=boom,
            finmind_fetch=fm,
        )
        fm.assert_not_called()
        self.assertEqual(fetched.status, "error")
        self.assertEqual(fetched.source, "none")
        self.assertNotEqual(fetched.status, "ok")

    def test_empty_t86_without_token_fails_gap_job(self):
        self._seed_gap()
        fm = MagicMock(side_effect=AssertionError("must not call FinMind"))
        with patch.object(backfill.time, "sleep"):
            with self.assertRaises(backfill.InstitutionalGapError) as ctx:
                backfill.backfill_institutional_gaps(
                    today=date(2026, 1, 5),
                    env={},
                    t86_fetch=lambda d: collector.EMPTY_T86,
                    finmind_fetch=fm,
                )
        fm.assert_not_called()
        self.assertEqual(ctx.exception.wrote, 0)
        self.assertEqual(ctx.exception.failed, 1)
        self.assertEqual(ctx.exception.missing, 1)
        self.assertEqual(ctx.exception.failed_dates, ["2026-01-05"])

    def test_gap_fill_logs_wrote_failed_counts_and_source_switch(self):
        self._seed_gap()
        with self.assertLogs(backfill.log, level="INFO") as cm:
            with patch.object(backfill.time, "sleep"):
                with self.assertRaises(backfill.InstitutionalGapError):
                    backfill.backfill_institutional_gaps(
                        today=date(2026, 1, 5),
                        env={"FINMIND_TOKEN": "secret"},
                        t86_fetch=lambda d: collector.EMPTY_T86,
                        finmind_fetch=lambda d: collector.EMPTY_T86,
                    )
        text = "\n".join(cm.output)
        self.assertIn("wrote=0", text)
        self.assertIn("failed=1", text)
        self.assertIn("missing=1", text)
        self.assertIn("source_switch finmind->t86", text)
        self.assertIn("status=empty", text)
        self.assertNotIn("secret", text)

    def test_partial_fill_still_non_success(self):
        con = collector.get_conn()
        with con:
            con.execute(
                "INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                ("2026-01-05", "2330", "台積電", 1, 1, 0),
            )
            con.execute(
                "INSERT INTO foreign_daily VALUES (?,?,?,?,?,?)",
                ("2026-01-06", "2330", "台積電", 1, 1, 0),
            )
        con.close()

        def fm(day):
            if day == date(2026, 1, 5):
                return self._finmind_tables(day)
            return collector.EMPTY_T86

        with patch.object(backfill.time, "sleep"):
            with self.assertRaises(backfill.InstitutionalGapError) as ctx:
                backfill.backfill_institutional_gaps(
                    today=date(2026, 1, 6),
                    env={"FINMIND_TOKEN": "secret"},
                    t86_fetch=lambda d: collector.EMPTY_T86,
                    finmind_fetch=fm,
                )
        self.assertEqual(ctx.exception.wrote, 1)
        self.assertEqual(ctx.exception.failed, 1)
        self.assertEqual(ctx.exception.failed_dates, ["2026-01-06"])


class WorkflowDocsTests(unittest.TestCase):
    def test_market_workflow_has_finmind_secret_not_literal(self):
        text = Path(__file__).resolve().parents[1].joinpath(
            ".github", "workflows", "update_market_data.yml",
        ).read_text(encoding="utf-8")
        self.assertIn("secrets.FINMIND_TOKEN", text)
        self.assertIn("institutional_gaps", text)
        self.assertNotRegex(text, r"FINMIND_TOKEN:\s+['\"]?[A-Za-z0-9_\-]{8,}")

    def test_readme_notes_actions_cannot_scrape_t86_history(self):
        text = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("不能穩定爬證交所 T86", text)
        self.assertIn("TaiwanStockInstitutionalInvestorsBuySell", text)
        self.assertIn("FINMIND_TOKEN", text)
        self.assertIn("source_switch", text)
        self.assertIn("InstitutionalGapError", text)

    def test_env_example_still_empty_placeholder(self):
        text = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text()
        self.assertIn("FINMIND_TOKEN=", text)
        self.assertNotRegex(text, r"FINMIND_TOKEN=\S+")
        self.assertIn("institutional", text.lower())

    def test_per_stock_pacing_documented_in_module(self):
        blob = (fmi.PER_STOCK_PACING + " " + (fmi.__doc__ or "")).lower()
        self.assertIn("all-stocks", blob)
        self.assertIn("data_id", blob)
        self.assertIn("trade_date", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
