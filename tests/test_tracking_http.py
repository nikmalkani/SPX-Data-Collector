import json
import tempfile
import threading
import unittest
from datetime import date
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path

from spx_collector import backtest_prod
from spx_collector.tracking import ensure_tracking_db


class TrackingHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        market_db = Path(self.temp_dir.name) / "market.db"
        market_db.touch()
        tracking_db_url = f"sqlite:///{Path(self.temp_dir.name) / 'tracking.db'}"
        tracking_db = ensure_tracking_db(tracking_db_url)

        backtest_prod.SqlUiHandler.db_path = market_db
        backtest_prod.SqlUiHandler.tracking_db_path = tracking_db
        backtest_prod.SqlUiHandler.tracking_enabled = True
        backtest_prod.SqlUiHandler.tracking_metrics_enabled = True

        self.server = HTTPServer(("127.0.0.1", 0), backtest_prod.SqlUiHandler)
        self.host, self.port = self.server.server_address
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)

    def _shutdown_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, method, path, body=None):
        conn = HTTPConnection(self.host, self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type", "")
        conn.close()
        if "application/json" in content_type:
            return response.status, json.loads(raw.decode("utf-8"))
        return response.status, raw.decode("utf-8")

    def test_track_endpoint_and_metrics_routes(self):
        status, payload = self._request(
            "POST",
            "/api/track",
            {
                "event_name": "page_view",
                "event_version": 1,
                "anonymous_id": "anon-http",
                "session_id": "sess-http",
                "page_path": "/",
                "occurred_at": "2026-03-21T18:00:00Z",
                "data": {"title": "SPX Playground"},
            },
        )
        self.assertEqual(status, 202)
        self.assertTrue(payload["ok"])

        status, overview = self._request(
            "GET", "/api/ops/metrics/overview?from=2026-03-21&to=2026-03-21"
        )
        self.assertEqual(status, 200)
        self.assertEqual(overview["pageviews"], 1)
        self.assertEqual(overview["sessions"], 1)
        self.assertEqual(overview["unique_visitors"], 1)

        status, html = self._request("GET", "/ops/metrics")
        self.assertEqual(status, 200)
        self.assertIn("Local Tracking Metrics", html)


if __name__ == "__main__":
    unittest.main()
