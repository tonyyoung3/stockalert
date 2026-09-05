"""Static dashboard HTML: extract from Python, ETag / 304 / Cache-Control."""
import json
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from web import dashboard
from web import freshness as freshness_mod


def _request(url, headers=None, timeout=5):
    req = Request(url, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


class StaticExtractTests(unittest.TestCase):
    def test_html_is_loaded_from_web_static(self):
        disk = dashboard.INDEX_HTML_PATH.read_text(encoding="utf-8")
        self.assertEqual(dashboard.HTML, disk)
        self.assertTrue(dashboard.INDEX_HTML_PATH.is_file())
        src = Path(dashboard.__file__).read_text(encoding="utf-8")
        self.assertNotIn("<!DOCTYPE html>", src)
        self.assertIn("web/static", src)
        self.assertIn("樣本不足", disk)
        self.assertIn("function renderBacktestResult", disk)
        self.assertIn("showSection", disk)
        self.assertIn("/api/backtest/stock", disk)
        self.assertIn("broker_branch", disk)

    def test_static_path_rejects_traversal(self):
        self.assertIsNone(dashboard.resolve_static_path("/static/../dashboard.py"))
        self.assertIsNone(dashboard.resolve_static_path("/static/foo/bar.js"))
        self.assertIsNone(dashboard.resolve_static_path("/static/nope.js"))
        self.assertEqual(
            dashboard.resolve_static_path("/static/index.html"),
            dashboard.INDEX_HTML_PATH.resolve(),
        )
        self.assertEqual(
            dashboard.resolve_static_path("/static/tw_range.js"),
            (dashboard.STATIC_DIR / "tw_range.js").resolve(),
        )
        self.assertEqual(
            dashboard.resolve_static_path("/static/sc_overlay.js"),
            (dashboard.STATIC_DIR / "sc_overlay.js").resolve(),
        )
        self.assertEqual(
            dashboard.resolve_static_path("/"),
            dashboard.INDEX_HTML_PATH.resolve(),
        )


class StaticServeTests(unittest.TestCase):
    def setUp(self):
        self.httpd = dashboard.make_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_root_and_static_index_send_etag(self):
        for path in ("/", "/index.html", "/static/index.html"):
            status, headers, body = _request(self.base + path)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", headers.get("content-type", ""))
            etag = headers.get("etag")
            self.assertTrue(etag and etag.startswith('"') and etag.endswith('"'), path)
            self.assertEqual(headers.get("cache-control"), dashboard._STATIC_CACHE_CONTROL)
            self.assertIn("台股資料儀表板".encode("utf-8"), body)
            self.assertEqual(etag, dashboard.static_etag(body))

    def test_if_none_match_is_304(self):
        status, headers, body = _request(self.base + "/")
        self.assertEqual(status, 200)
        etag = headers["etag"]
        status2, headers2, body2 = _request(
            self.base + "/",
            headers={"If-None-Match": etag},
        )
        self.assertEqual(status2, 304)
        self.assertEqual(headers2.get("etag"), etag)
        self.assertEqual(headers2.get("cache-control"), dashboard._STATIC_CACHE_CONTROL)
        self.assertEqual(body2, b"")

    def test_tw_range_js_is_served(self):
        status, headers, body = _request(self.base + "/static/tw_range.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("content-type", ""))
        self.assertTrue(headers.get("etag", "").startswith('"'))
        text = body.decode("utf-8")
        self.assertIn("root.TwRange", text)
        self.assertIn("function onOrBefore", text)
        self.assertIn("function toTopQuery", text)
        self.assertIn("function toScanner", text)

    def test_sc_overlay_js_is_served(self):
        status, headers, body = _request(self.base + "/static/sc_overlay.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("content-type", ""))
        self.assertTrue(headers.get("etag", "").startswith('"'))
        text = body.decode("utf-8")
        self.assertIn("root.ScOverlay", text)
        self.assertIn("function normalizePoints", text)
        self.assertIn("function sliceToWindow", text)
        self.assertIn("function lookbackDays", text)

    def test_stale_etag_returns_200(self):
        status, headers, body = _request(
            self.base + "/",
            headers={"If-None-Match": '"deadbeef"'},
        )
        self.assertEqual(status, 200)
        self.assertGreater(len(body), 1000)
        self.assertNotEqual(headers.get("etag"), '"deadbeef"')


class AlertsTaipeiBoundaryTests(unittest.TestCase):
    def test_alerts_since_uses_taipei_today(self):
        # UTC Fri 16:30 == Taipei Sat 00:30. date.today() on UTC would still be Fri.
        utc_sat = datetime(2026, 9, 4, 16, 30, tzinfo=timezone.utc)
        tw = datetime(2026, 9, 5, 0, 30, tzinfo=timezone(timedelta(hours=8)))
        with patch("web.freshness.taiwan_now", return_value=tw):
            self.assertEqual(freshness_mod.taiwan_today(), tw.date())
            since = dashboard.api_alerts({"days": ["7"]})["since"]
        self.assertEqual(since, str(tw.date() - timedelta(days=7)))
        self.assertNotEqual(since, str(utc_sat.date() - timedelta(days=7)))


class StaticAuthTests(unittest.TestCase):
    def test_static_requires_auth_when_enabled(self):
        env = {"DASHBOARD_USER": "alice", "DASHBOARD_PASSWORD": "s3cret"}
        with patch.dict(os.environ, env, clear=False):
            httpd = dashboard.make_server("127.0.0.1", 0)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{httpd.server_address[1]}"
                status, headers, body = _request(base + "/static/index.html")
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(body.decode("utf-8")), {"error": "unauthorized"})
                self.assertIn("Basic", headers.get("www-authenticate", ""))
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
