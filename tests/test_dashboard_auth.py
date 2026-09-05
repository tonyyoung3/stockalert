"""HTTP Basic Auth for the dashboard: env gating, 401/200, /health, backtest."""
import base64
import json
import os
import threading
import unittest
from contextlib import contextmanager
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from web import dashboard


AUTH_USER = "alice"
AUTH_PASSWORD = "s3cret-pass"
AUTH_ENV = {
    "DASHBOARD_USER": AUTH_USER,
    "DASHBOARD_PASSWORD": AUTH_PASSWORD,
}


def _basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request(url, method="GET", headers=None, data=None, timeout=5):
    req = Request(url, data=data, method=method, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


@contextmanager
def _serve():
    httpd = HTTPServer(("127.0.0.1", 0), dashboard.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


class CredentialHelperTests(unittest.TestCase):
    def test_missing_either_env_is_open(self):
        self.assertFalse(dashboard.auth_enabled({}))
        self.assertFalse(dashboard.auth_enabled({"DASHBOARD_USER": "a"}))
        self.assertFalse(dashboard.auth_enabled({"DASHBOARD_PASSWORD": "b"}))
        self.assertFalse(dashboard.auth_enabled({"DASHBOARD_USER": "", "DASHBOARD_PASSWORD": "b"}))
        self.assertFalse(dashboard.auth_enabled({"DASHBOARD_USER": "a", "DASHBOARD_PASSWORD": ""}))
        self.assertFalse(dashboard.auth_enabled({"DASHBOARD_USER": "  ", "DASHBOARD_PASSWORD": "b"}))

    def test_both_set_enables_auth(self):
        self.assertTrue(dashboard.auth_enabled(AUTH_ENV))
        self.assertEqual(dashboard.dashboard_credentials(AUTH_ENV), (AUTH_USER, AUTH_PASSWORD))

    def test_path_rules(self):
        self.assertFalse(dashboard.path_requires_auth("/health"))
        self.assertTrue(dashboard.path_requires_auth("/"))
        self.assertTrue(dashboard.path_requires_auth("/index.html"))
        self.assertTrue(dashboard.path_requires_auth("/api/summary"))
        self.assertTrue(dashboard.path_requires_auth("/api/backtest"))
        self.assertFalse(dashboard.path_requires_auth("/favicon.ico"))

    def test_parse_and_validate_basic(self):
        header = _basic(AUTH_USER, AUTH_PASSWORD)
        self.assertEqual(
            dashboard.parse_basic_authorization(header),
            (AUTH_USER, AUTH_PASSWORD),
        )
        self.assertTrue(dashboard.valid_basic_header(header, AUTH_USER, AUTH_PASSWORD))
        self.assertFalse(dashboard.valid_basic_header(None, AUTH_USER, AUTH_PASSWORD))
        self.assertFalse(dashboard.valid_basic_header("Bearer x", AUTH_USER, AUTH_PASSWORD))
        self.assertFalse(dashboard.valid_basic_header("Basic $$$", AUTH_USER, AUTH_PASSWORD))
        self.assertFalse(
            dashboard.valid_basic_header(_basic(AUTH_USER, "nope"), AUTH_USER, AUTH_PASSWORD)
        )
        self.assertTrue(
            dashboard.valid_basic_header(
                _basic(AUTH_USER, "p:with:colons"), AUTH_USER, "p:with:colons"
            )
        )


class DashboardAuthHTTPTests(unittest.TestCase):
    def setUp(self):
        dashboard._backtest_limiter.reset()

    def test_missing_env_leaves_html_and_api_open(self):
        env = {k: v for k, v in os.environ.items() if k not in AUTH_ENV}
        with patch.dict(os.environ, env, clear=True):
            with _serve() as base:
                status, _, body = _request(base + "/")
                self.assertEqual(status, 200)
                self.assertIn(b"台股資料儀表板", body)
                with patch.object(dashboard, "api", return_value={"ok": True}):
                    status, _, body = _request(base + "/api/summary")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["ok"], True)

    def test_auth_on_without_header_is_401(self):
        with patch.dict(os.environ, AUTH_ENV, clear=False):
            with _serve() as base:
                for path in ("/", "/index.html", "/api/summary", "/api/freshness"):
                    status, headers, body = _request(base + path)
                    self.assertEqual(status, 401, path)
                    self.assertEqual(
                        headers.get("www-authenticate"),
                        'Basic realm="stockalert"',
                    )
                    payload = json.loads(body.decode("utf-8"))
                    self.assertEqual(payload, {"error": "unauthorized"})
                    text = body.decode("utf-8").lower()
                    self.assertNotIn("traceback", text)
                    self.assertNotIn("sqlite", text)
                    self.assertNotIn("/workspace", text)
                    self.assertNotIn("turso", text)

    def test_correct_basic_returns_200(self):
        headers = {"Authorization": _basic(AUTH_USER, AUTH_PASSWORD)}
        with patch.dict(os.environ, AUTH_ENV, clear=False):
            with _serve() as base:
                status, _, body = _request(base + "/", headers=headers)
                self.assertEqual(status, 200)
                self.assertIn(b"台股資料儀表板", body)
                with patch.object(dashboard, "api", return_value={"ok": True}):
                    status, _, body = _request(base + "/api/summary", headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["ok"], True)

    def test_wrong_password_is_401(self):
        headers = {"Authorization": _basic(AUTH_USER, "wrong")}
        with patch.dict(os.environ, AUTH_ENV, clear=False):
            with _serve() as base:
                status, resp_headers, body = _request(base + "/", headers=headers)
                self.assertEqual(status, 401)
                self.assertEqual(
                    resp_headers.get("www-authenticate"),
                    'Basic realm="stockalert"',
                )
                self.assertEqual(json.loads(body.decode("utf-8")), {"error": "unauthorized"})

    def test_health_open_when_auth_on(self):
        with patch.dict(os.environ, AUTH_ENV, clear=False):
            with patch.object(
                dashboard,
                "health_payload",
                return_value={"status": "ok", "ok": True, "freshness": {"stale": False}},
            ):
                with _serve() as base:
                    status, headers, body = _request(base + "/health")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("content-type", ""))
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["ok"])

    def test_backtest_post_protected(self):
        payload = json.dumps({"dataset": "2y_hourly"}).encode("utf-8")
        with patch.dict(os.environ, AUTH_ENV, clear=False):
            with _serve() as base:
                status, headers, body = _request(
                    base + "/api/backtest",
                    method="POST",
                    headers={"Content-Type": "application/json"},
                    data=payload,
                )
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(body.decode("utf-8")), {"error": "unauthorized"})
                self.assertEqual(
                    headers.get("www-authenticate"),
                    'Basic realm="stockalert"',
                )

                fake_conn = MagicMock()
                with patch.object(dashboard.market_db, "connect_for_backtest", return_value=fake_conn):
                    with patch("web.backtest_engine.run_backtest", return_value={"n": 0}):
                        status, _, body = _request(
                            base + "/api/backtest",
                            method="POST",
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": _basic(AUTH_USER, AUTH_PASSWORD),
                            },
                            data=payload,
                        )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body.decode("utf-8"))["n"], 0)

                with patch.object(
                    dashboard.market_db,
                    "connect_for_backtest",
                    side_effect=RuntimeError("/secret/db.sqlite connection failed"),
                ):
                    status, _, body = _request(
                        base + "/api/backtest",
                        method="POST",
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": _basic(AUTH_USER, AUTH_PASSWORD),
                        },
                        data=payload,
                    )
                text = body.decode("utf-8")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(text), {"error": "回測執行失敗"})
                self.assertNotIn("/secret", text)
                self.assertNotIn("sqlite", text.lower())

    def test_only_user_or_only_password_is_open(self):
        for env in (
            {"DASHBOARD_USER": AUTH_USER, "DASHBOARD_PASSWORD": ""},
            {"DASHBOARD_USER": "", "DASHBOARD_PASSWORD": AUTH_PASSWORD},
        ):
            merged = {**os.environ, **env}
            with patch.dict(os.environ, merged, clear=True):
                with _serve() as base:
                    status, _, _ = _request(base + "/")
                    self.assertEqual(status, 200, env)

    def test_html_fetch_uses_same_origin_credentials(self):
        self.assertIn("fetch(u, {credentials: 'same-origin'})", dashboard.HTML)
        self.assertIn("credentials:'same-origin'", dashboard.HTML)
        self.assertNotIn(AUTH_PASSWORD, dashboard.HTML)
        self.assertNotIn("DASHBOARD_PASSWORD", dashboard.HTML)

    def test_readme_documents_basic_auth_and_local_only_when_unset(self):
        text = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("DASHBOARD_USER", text)
        self.assertIn("DASHBOARD_PASSWORD", text)
        self.assertIn("--set-secrets", text)
        self.assertIn("--allow-unauthenticated", text)
        self.assertIn("不要在沒設這兩個變數的情況下公開部署", text)
        self.assertIn("僅本機", text)
        self.assertIn("GET /health", text)
        self.assertIn("WWW-Authenticate", text)

    def test_backtest_rate_limit_when_auth_on(self):
        limiter = dashboard._IpRateLimiter(max_hits=2, window_sec=60)
        self.assertTrue(limiter.allow("1.1.1.1", now=100.0))
        self.assertTrue(limiter.allow("1.1.1.1", now=100.5))
        self.assertFalse(limiter.allow("1.1.1.1", now=101.0))
        self.assertTrue(limiter.allow("2.2.2.2", now=101.0))
        self.assertTrue(limiter.allow("1.1.1.1", now=161.0))


if __name__ == "__main__":
    unittest.main()
