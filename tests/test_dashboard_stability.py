"""Tier 1 production stability: threading (#67), rate limit (#68), XFF (#69), errors (#70)."""
import json
import os
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from web import dashboard


def _request(url, method="GET", headers=None, data=None, timeout=5):
    req = Request(url, data=data, method=method, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


@contextmanager
def _serve():
    httpd = dashboard.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _open_local_env(**extra):
    """Anonymous local-dev: no auth, no Cloud Run identity."""
    drop = {
        "DASHBOARD_USER",
        "DASHBOARD_PASSWORD",
        "K_SERVICE",
        "DASHBOARD_ALLOW_ANONYMOUS",
        "DASHBOARD_FAIL_CLOSED",
        "DASHBOARD_BACKTEST_RPM",
        "DASHBOARD_BACKTEST_ANON_RPM",
    }
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(extra)
    return env


class ThreadingAndIsolationTests(unittest.TestCase):
    def test_make_server_is_threading(self):
        httpd = dashboard.make_server("127.0.0.1", 0)
        try:
            self.assertIsInstance(httpd, dashboard.ThreadingHTTPServer)
            self.assertTrue(httpd.daemon_threads)
        finally:
            httpd.server_close()

    def test_contextvar_not_shared_across_threads(self):
        seen = {}
        errors = []
        barrier = threading.Barrier(2)

        def worker(label):
            marker = object()
            token = dashboard._request_conn.set(marker)
            try:
                barrier.wait(timeout=2)
                time.sleep(0.05)
                got = dashboard._request_conn.get()
                seen[label] = got
                if got is not marker:
                    errors.append(label)
            finally:
                dashboard._request_conn.reset(token)

        threads = [
            threading.Thread(target=worker, args=("a",)),
            threading.Thread(target=worker, args=("b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)
        self.assertEqual(len(seen), 2)
        self.assertIsNot(seen["a"], seen["b"])
        self.assertEqual(errors, [])
        self.assertIsNone(dashboard._request_conn.get())

    def test_concurrent_api_calls_use_distinct_connections(self):
        barrier = threading.Barrier(2)
        observed = []
        errors = []
        lock = threading.Lock()

        class FakeConn:
            def __init__(self):
                self.tid = threading.current_thread().ident

            def execute(self, *args, **kwargs):
                current = dashboard._request_conn.get()
                with lock:
                    observed.append((self.tid, id(self), id(current)))
                barrier.wait(timeout=2)
                if dashboard._request_conn.get() is not self:
                    errors.append("polluted")
                cur = MagicMock()
                cur.fetchall.return_value = [(None,)]
                return cur

            def close(self):
                pass

        def worker():
            conn = FakeConn()
            token = dashboard._request_conn.set(conn)
            try:
                dashboard.q("SELECT 1")
            finally:
                dashboard._request_conn.reset(token)
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)

        tids = {row[0] for row in observed}
        conn_ids = {row[1] for row in observed}
        bound_ids = {row[2] for row in observed}
        self.assertEqual(errors, [])
        self.assertEqual(len(tids), 2)
        self.assertEqual(len(conn_ids), 2)
        self.assertEqual(conn_ids, bound_ids)

    def test_api_binds_then_resets_request_conn(self):
        fake = MagicMock()
        seen = []

        def inner(path, qs):
            seen.append(dashboard._request_conn.get())
            return {"ok": True}

        with patch.object(dashboard.market_db, "connect", return_value=fake):
            with patch.object(dashboard, "_api", inner):
                out = dashboard.api("/api/summary", {})
        self.assertEqual(out, {"ok": True})
        self.assertEqual(seen, [fake])
        self.assertIsNone(dashboard._request_conn.get())
        fake.close.assert_called_once()

    def test_health_responds_during_slow_backtest(self):
        started = threading.Event()
        release = threading.Event()

        def hold():
            started.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test hold was not released")

        payload = json.dumps({"dataset": "2y_hourly"}).encode("utf-8")
        fake_conn = MagicMock()
        env = _open_local_env()
        with patch.dict(os.environ, env, clear=True):
            dashboard._backtest_limiter.reset()
            with _serve() as base:
                with patch.object(dashboard, "_synthetic_handler_hold", hold):
                    with patch.object(
                        dashboard.market_db, "connect_for_backtest", return_value=fake_conn
                    ):
                        with patch.object(dashboard, "execute_index_backtest", return_value={"n": 0}):
                            post_result = {}

                            def post():
                                post_result["resp"] = _request(
                                    base + "/api/backtest",
                                    method="POST",
                                    headers={"Content-Type": "application/json"},
                                    data=payload,
                                    timeout=5,
                                )

                            worker = threading.Thread(target=post)
                            worker.start()
                            self.assertTrue(started.wait(timeout=2))
                            t0 = time.monotonic()
                            status, _, body = _request(base + "/health", timeout=2)
                            elapsed = time.monotonic() - t0
                            release.set()
                            worker.join(timeout=3)
                self.assertEqual(status, 200)
                health = json.loads(body.decode("utf-8"))
                self.assertEqual(health["status"], "ok")
                self.assertTrue(health["ok"])
                self.assertLess(elapsed, 1.0)
                self.assertEqual(post_result["resp"][0], 200)


class RateLimitAndAnonymousTests(unittest.TestCase):
    def setUp(self):
        dashboard._backtest_limiter.reset()

    def test_allow_anonymous_local_vs_cloud_and_fail_closed(self):
        self.assertTrue(dashboard.allow_anonymous({}))
        self.assertFalse(dashboard.allow_anonymous({"K_SERVICE": "stockalert"}))
        self.assertTrue(
            dashboard.allow_anonymous(
                {"K_SERVICE": "stockalert", "DASHBOARD_ALLOW_ANONYMOUS": "1"}
            )
        )
        self.assertFalse(dashboard.allow_anonymous({"DASHBOARD_FAIL_CLOSED": "1"}))
        self.assertTrue(
            dashboard.allow_anonymous(
                {"DASHBOARD_FAIL_CLOSED": "1", "DASHBOARD_ALLOW_ANONYMOUS": "true"}
            )
        )

    def test_anonymous_rpm_is_stricter_than_auth(self):
        self.assertEqual(dashboard.backtest_max_hits({}), dashboard.BACKTEST_ANON_RATE_LIMIT)
        self.assertEqual(
            dashboard.backtest_max_hits(
                {"DASHBOARD_USER": "a", "DASHBOARD_PASSWORD": "b"}
            ),
            dashboard.BACKTEST_RATE_LIMIT,
        )
        self.assertEqual(
            dashboard.backtest_max_hits({"DASHBOARD_BACKTEST_ANON_RPM": "2"}),
            2,
        )

    def test_no_auth_backtest_is_still_rate_limited(self):
        env = _open_local_env(DASHBOARD_BACKTEST_ANON_RPM="2")
        payload = json.dumps({"dataset": "2y_hourly"}).encode("utf-8")
        fake_conn = MagicMock()
        with patch.dict(os.environ, env, clear=True):
            with _serve() as base:
                with patch.object(
                    dashboard.market_db, "connect_for_backtest", return_value=fake_conn
                ):
                    with patch.object(dashboard, "execute_index_backtest", return_value={"n": 0}):
                        for _ in range(2):
                            status, _, body = _request(
                                base + "/api/backtest",
                                method="POST",
                                headers={"Content-Type": "application/json"},
                                data=payload,
                            )
                            self.assertEqual(status, 200)
                            self.assertEqual(json.loads(body.decode("utf-8"))["n"], 0)
                        status, _, body = _request(
                            base + "/api/backtest",
                            method="POST",
                            headers={"Content-Type": "application/json"},
                            data=payload,
                        )
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body.decode("utf-8")), {"error": "rate_limited"})

    def test_stock_backtest_rate_limited_without_auth(self):
        env = _open_local_env(DASHBOARD_BACKTEST_ANON_RPM="1")
        payload = json.dumps({"stock_id": "2330", "pattern": "inside_day"}).encode("utf-8")
        fake_conn = MagicMock()
        with patch.dict(os.environ, env, clear=True):
            with _serve() as base:
                with patch.object(dashboard.market_db, "connect", return_value=fake_conn):
                    with patch.object(
                        dashboard,
                        "execute_stock_backtest",
                        return_value={"n": 0, "no_trigger": True},
                    ):
                        status, _, _ = _request(
                            base + "/api/backtest/stock",
                            method="POST",
                            headers={"Content-Type": "application/json"},
                            data=payload,
                        )
                        self.assertEqual(status, 200)
                        status, _, body = _request(
                            base + "/api/backtest/stock",
                            method="POST",
                            headers={"Content-Type": "application/json"},
                            data=payload,
                        )
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body.decode("utf-8")), {"error": "rate_limited"})

    def test_cloud_run_without_auth_or_allow_anonymous_fail_closed(self):
        env = _open_local_env(K_SERVICE="stockalert")
        with patch.dict(os.environ, env, clear=True):
            with _serve() as base:
                for path in ("/", "/api/summary", "/api/backtest"):
                    method = "POST" if path.endswith("backtest") else "GET"
                    data = b"{}" if method == "POST" else None
                    status, _, body = _request(
                        base + path,
                        method=method,
                        headers={"Content-Type": "application/json"} if data else None,
                        data=data,
                    )
                    self.assertEqual(status, 403, path)
                    self.assertEqual(
                        json.loads(body.decode("utf-8")),
                        {"error": "anonymous_disabled"},
                    )
                status, _, body = _request(base + "/health")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body.decode("utf-8"))["status"], "ok")


class ClientIpXffTests(unittest.TestCase):
    def test_rightmost_hop_wins(self):
        handler = MagicMock()
        handler.headers = {"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 9.9.9.9"}
        handler.client_address = ("10.0.0.1", 12345)
        self.assertEqual(dashboard.client_ip(handler), "9.9.9.9")

    def test_blank_xff_falls_back_to_socket(self):
        handler = MagicMock()
        handler.headers = {"X-Forwarded-For": "  ,  "}
        handler.client_address = ("10.0.0.1", 12345)
        self.assertEqual(dashboard.client_ip(handler), "10.0.0.1")

    def test_forged_leading_xff_cannot_create_unlimited_buckets(self):
        dashboard._backtest_limiter.reset()
        env = _open_local_env(DASHBOARD_BACKTEST_ANON_RPM="2")
        payload = json.dumps({"dataset": "2y_hourly"}).encode("utf-8")
        fake_conn = MagicMock()
        with patch.dict(os.environ, env, clear=True):
            with _serve() as base:
                with patch.object(
                    dashboard.market_db, "connect_for_backtest", return_value=fake_conn
                ):
                    with patch.object(dashboard, "execute_index_backtest", return_value={"n": 0}):
                        statuses = []
                        for spoof in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
                            status, _, body = _request(
                                base + "/api/backtest",
                                method="POST",
                                headers={
                                    "Content-Type": "application/json",
                                    "X-Forwarded-For": f"{spoof}, 203.0.113.9",
                                },
                                data=payload,
                            )
                            statuses.append((status, json.loads(body.decode("utf-8"))))
        self.assertEqual(statuses[0][0], 200)
        self.assertEqual(statuses[1][0], 200)
        self.assertEqual(statuses[2][0], 429)
        self.assertEqual(statuses[2][1], {"error": "rate_limited"})


class ErrorLoggingTests(unittest.TestCase):
    def setUp(self):
        dashboard._backtest_limiter.reset()

    def test_unexpected_backtest_error_is_500_logged_without_stack_to_client(self):
        env = _open_local_env()
        payload = json.dumps({"dataset": "2y_hourly"}).encode("utf-8")
        with patch.dict(os.environ, env, clear=True):
            with _serve() as base:
                with patch.object(
                    dashboard.market_db,
                    "connect_for_backtest",
                    side_effect=RuntimeError("/secret/db.sqlite connection failed"),
                ):
                    with self.assertLogs("web.dashboard", level="ERROR") as cm:
                        status, _, body = _request(
                            base + "/api/backtest",
                            method="POST",
                            headers={"Content-Type": "application/json"},
                            data=payload,
                        )
        text = body.decode("utf-8")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(text), {"error": "回測執行失敗"})
        self.assertNotIn("traceback", text.lower())
        self.assertNotIn("/secret", text)
        self.assertNotIn("sqlite", text.lower())
        self.assertTrue(any("Traceback" in line or "RuntimeError" in line for line in cm.output))

    def test_bad_json_is_400(self):
        env = _open_local_env()
        with patch.dict(os.environ, env, clear=True):
            with _serve() as base:
                status, _, body = _request(
                    base + "/api/backtest",
                    method="POST",
                    headers={"Content-Type": "application/json"},
                    data=b"not-json",
                )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body.decode("utf-8")), {"error": "invalid_json"})

    def test_get_api_unexpected_error_is_500_without_stack(self):
        env = _open_local_env()
        with patch.dict(os.environ, env, clear=True):
            with _serve() as base:
                with patch.object(dashboard, "api", side_effect=RuntimeError("boom /tmp/secret")):
                    with self.assertLogs("web.dashboard", level="ERROR") as cm:
                        status, _, body = _request(base + "/api/summary")
        text = body.decode("utf-8")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(text), {"error": "internal_error"})
        self.assertNotIn("traceback", text.lower())
        self.assertNotIn("/tmp/secret", text)
        self.assertNotIn("status", json.loads(text))
        self.assertTrue(any("unexpected error GET" in line for line in cm.output))

    def test_health_stays_ok_and_distinct_from_business_500(self):
        env = _open_local_env()
        with patch.dict(os.environ, env, clear=True):
            with _serve() as base:
                with patch.object(
                    dashboard, "health_payload", return_value={"status": "ok", "ok": True}
                ):
                    h_status, _, h_body = _request(base + "/health")
                with patch.object(dashboard, "api", side_effect=RuntimeError("nope")):
                    b_status, _, b_body = _request(base + "/api/summary")
        health = json.loads(h_body.decode("utf-8"))
        business = json.loads(b_body.decode("utf-8"))
        self.assertEqual(h_status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["ok"])
        self.assertEqual(b_status, 500)
        self.assertEqual(business["error"], "internal_error")
        self.assertNotIn("ok", business)
        self.assertNotIn("status", business)

    def test_access_log_is_not_silent(self):
        env = _open_local_env()
        with patch.dict(os.environ, env, clear=True):
            with _serve() as base:
                with self.assertLogs("web.dashboard", level="INFO") as cm:
                    _request(base + "/health")
        self.assertTrue(any("GET" in line and "/health" in line for line in cm.output))

    def test_health_freshness_failure_does_not_leak_exception(self):
        with patch.object(dashboard.market_db, "available", return_value=True):
            with patch.object(dashboard, "api", side_effect=RuntimeError("turso://secret")):
                with self.assertLogs("web.dashboard", level="ERROR"):
                    body = dashboard.health_payload()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["ok"])
        self.assertEqual(body["freshness"]["error"], "freshness_unavailable")
        dumped = json.dumps(body)
        self.assertNotIn("turso://secret", dumped)


class ReadmeStabilityDocsTests(unittest.TestCase):
    def test_readme_documents_tier1_ops(self):
        from pathlib import Path

        text = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("ThreadingHTTPServer", text)
        self.assertIn("最後一段", text)
        self.assertIn("DASHBOARD_BACKTEST_ANON_RPM", text)
        self.assertIn("DASHBOARD_FAIL_CLOSED", text)
        self.assertIn("max-instances", text)
        self.assertIn("logging.exception", text)


if __name__ == "__main__":
    unittest.main()
