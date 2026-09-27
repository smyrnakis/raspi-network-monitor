import asyncio
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from home_internet_monitor.domain.models import ComponentStatus, RoundEvidence, RoundStatus
from home_internet_monitor.monitor.models import CompletedRound
from home_internet_monitor.monitor.config import parse_config
from home_internet_monitor.storage import connect_database, migrate

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None

if FASTAPI_AVAILABLE:
    from home_internet_monitor.api.app import create_app


async def request(
    app,
    path,
    query_string=b"",
    method="GET",
    headers=None,
    body=b"",
):
    messages = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query_string,
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8080),
        "root_path": "",
    }
    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start["status"], dict(start["headers"]), body


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependency is not installed")
class AppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "monitor.db"
        connection = connect_database(self.path)
        migrate(connection)
        connection.execute(
            """
            INSERT INTO sites(site_id, display_name, timezone, created_at_ms, updated_at_ms)
            VALUES ('home', 'Home', 'UTC', 0, 0)
            """
        )
        connection.commit()
        connection.close()
        config = parse_config(
            {
                "site": {"id": "home", "display_name": "Home", "timezone": "UTC"},
                "storage": {"database_path": str(self.path.resolve())},
                "probes": [
                    {"id": "gw", "kind": "gateway", "endpoint": "auto"},
                    {"id": "ip1", "kind": "external_ip", "endpoint": "1.1.1.1"},
                    {"id": "ip2", "kind": "external_ip", "endpoint": "8.8.8.8"},
                    {"id": "dns", "kind": "dns", "endpoint": "example.com"},
                    {"id": "web", "kind": "https", "endpoint": "https://example.com"},
                ],
            }
        )
        self.app = create_app(config)

    def tearDown(self):
        self.temporary.cleanup()

    def test_status_route_returns_initialized_site(self):
        status, _, body = asyncio.run(request(self.app, "/api/v1/status"))
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertEqual("home", payload["site"]["site_id"])
        self.assertEqual("monitoring_unknown", payload["stable_status"])
        self.assertTrue(payload["hostname"])
        self.assertEqual("0.4.4", payload["version"])
        self.assertTrue(payload["dashboard"]["hide_short_incidents"])

    def test_invalid_availability_window_returns_400(self):
        query = (
            b"start=2026-01-02T00%3A00%3A00%2B00%3A00&"
            b"end=2026-01-01T00%3A00%3A00%2B00%3A00"
        )
        status, _, body = asyncio.run(
            request(self.app, "/api/v1/availability", query)
        )
        self.assertEqual(400, status)
        self.assertIn("end must be after start", json.loads(body)["detail"])

    def test_csv_route_sets_attachment_and_contains_header(self):
        status, headers, body = asyncio.run(
            request(self.app, "/api/v1/incidents.csv")
        )
        self.assertEqual(200, status)
        self.assertIn(b"attachment", headers[b"content-disposition"])
        self.assertTrue(body.startswith(b"incident_id,status,lifecycle"))

        status, headers, body = asyncio.run(
            request(self.app, "/api/v1/history.csv", b"event_type=gap")
        )
        self.assertEqual(200, status)
        self.assertIn(b"attachment", headers[b"content-disposition"])
        self.assertTrue(body.startswith(b"event_type,event_id,category,state"))

    def test_dashboard_and_local_assets_are_served(self):
        status, headers, body = asyncio.run(request(self.app, "/"))
        self.assertEqual(200, status)
        self.assertIn(b"text/html", headers[b"content-type"])
        self.assertIn(b">Timeline<", body)
        self.assertIn(b"24h", body)
        self.assertIn(b"7d", body)
        self.assertIn(b"30d", body)
        self.assertIn(b"Custom", body)
        self.assertIn(b"timeline-tooltip", body)
        self.assertIn(b"Ping latency", body)
        self.assertIn(b'data-latency-window="1h"', body)
        self.assertIn(b"latency-tooltip", body)
        self.assertIn(
            b'class="latency-window active" data-latency-window="1h"', body
        )
        self.assertNotIn(b"classified-value", body)
        self.assertNotIn(b"unknown-value", body)
        self.assertNotIn(b"Download CSV", body)
        self.assertEqual(2, body.count(b">Detailed history<"))

        status, headers, body = asyncio.run(request(self.app, "/assets/app.js"))
        self.assertEqual(200, status)
        self.assertIn(b"javascript", headers[b"content-type"])
        self.assertIn(b"selectedRange", body)
        self.assertIn(b"timelineBucketCount", body)
        self.assertIn(b"TOOLTIP_HIDE_DELAY_MS", body)
        self.assertIn(b'max-width: 540px', body)
        self.assertIn(b"setPointerCapture", body)
        self.assertIn(b"latencyTooltipLabel", body)
        self.assertIn(b"formatLatencyTooltipTime", body)
        self.assertIn(b'/api/v1/incidents?limit=5', body)
        self.assertIn(b'/api/v1/gaps?limit=5', body)
        self.assertIn(b"focus_type=incident", body)

        status, headers, body = asyncio.run(request(self.app, "/settings"))
        self.assertEqual(200, status)
        self.assertIn(b"text/html", headers[b"content-type"])
        self.assertIn(b"Follow device theme", body)
        self.assertIn(b'data-theme-choice="light"', body)
        self.assertIn(b'data-theme-choice="dark"', body)
        self.assertIn(b"Check schedule", body)
        self.assertIn(b"Probe targets", body)
        self.assertIn(b"Data retention", body)
        self.assertIn(b"Hide incidents shorter than one minute", body)

        status, headers, body = asyncio.run(request(self.app, "/assets/theme.js"))
        self.assertEqual(200, status)
        self.assertIn(b"javascript", headers[b"content-type"])
        self.assertIn(b"network-monitor-theme", body)

        status, headers, body = asyncio.run(request(self.app, "/assets/styles.css"))
        self.assertEqual(200, status)
        self.assertIn(b"text/css", headers[b"content-type"])
        self.assertIn(b"touch-action: pan-y", body)

        status, headers, body = asyncio.run(request(self.app, "/history"))
        self.assertEqual(200, status)
        self.assertIn(b"text/html", headers[b"content-type"])
        self.assertIn(b"Download filtered CSV", body)
        self.assertIn(b"20 events per page", body)

        status, headers, body = asyncio.run(request(self.app, "/assets/history.js"))
        self.assertEqual(200, status)
        self.assertIn(b"javascript", headers[b"content-type"])
        self.assertIn(b"/api/v1/history.csv", body)
        self.assertIn(b"focus_type", body)
        self.assertIn(b"Failed when confirmed", body)

    def test_manual_test_requires_action_header(self):
        status, _, body = asyncio.run(
            request(self.app, "/api/v1/test", method="POST")
        )
        self.assertEqual(403, status)
        self.assertIn("manual test header required", json.loads(body)["detail"])

    def test_manual_test_returns_transient_probe_result(self):
        completed = CompletedRound(
            observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            evidence=RoundEvidence(
                gateway=ComponentStatus.REACHABLE,
                external_ip=ComponentStatus.REACHABLE,
                dns=ComponentStatus.REACHABLE,
                https=ComponentStatus.REACHABLE,
            ),
            status=RoundStatus.ONLINE,
            results=(),
        )

        class FakeExecutor:
            def __init__(self, *args, **kwargs):
                pass

            async def execute(self):
                return completed

        with patch("home_internet_monitor.api.app.RoundExecutor", FakeExecutor):
            status, _, body = asyncio.run(
                request(
                    self.app,
                    "/api/v1/test",
                    method="POST",
                    headers=[(b"x-monitor-action", b"manual-test")],
                )
            )
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertEqual("online", payload["status"])
        self.assertEqual("reachable", payload["components"]["dns"])

    def test_settings_update_is_validated_and_requests_monitor_restart(self):
        status, _, body = asyncio.run(request(self.app, "/api/v1/settings"))
        self.assertEqual(200, status)
        payload = json.loads(body)
        payload["monitor"]["interval_seconds"] = 20
        payload["monitor"]["round_timeout_seconds"] = 9
        payload["probes"][1]["endpoint"] = "9.9.9.9"
        encoded = json.dumps(payload).encode("utf-8")

        status, _, body = asyncio.run(
            request(
                self.app,
                "/api/v1/settings",
                method="PUT",
                headers=[
                    (b"content-type", b"application/json"),
                    (b"x-monitor-action", b"settings-update"),
                ],
                body=encoded,
            )
        )

        self.assertEqual(200, status)
        result = json.loads(body)
        self.assertTrue(result["monitor_restart_requested"])
        self.assertEqual(20, result["settings"]["monitor"]["interval_seconds"])

    def test_settings_update_requires_header_and_rejects_invalid_target(self):
        _, _, body = asyncio.run(request(self.app, "/api/v1/settings"))
        payload = json.loads(body)
        encoded = json.dumps(payload).encode("utf-8")
        status, _, _ = asyncio.run(
            request(
                self.app,
                "/api/v1/settings",
                method="PUT",
                headers=[(b"content-type", b"application/json")],
                body=encoded,
            )
        )
        self.assertEqual(403, status)

        payload["probes"][1]["endpoint"] = "not-an-ip"
        status, _, body = asyncio.run(
            request(
                self.app,
                "/api/v1/settings",
                method="PUT",
                headers=[
                    (b"content-type", b"application/json"),
                    (b"x-monitor-action", b"settings-update"),
                ],
                body=json.dumps(payload).encode("utf-8"),
            )
        )
        self.assertEqual(400, status)
        self.assertIn("require an IP address", json.loads(body)["detail"])

    def test_timeline_route_rejects_inverted_window(self):
        query = (
            b"start=2026-01-02T00%3A00%3A00%2B00%3A00&"
            b"end=2026-01-01T00%3A00%3A00%2B00%3A00"
        )
        status, _, body = asyncio.run(request(self.app, "/api/v1/timeline", query))
        self.assertEqual(400, status)
        self.assertIn("end must be after start", json.loads(body)["detail"])

    def test_latency_route_is_bounded_and_available_without_samples(self):
        query = (
            b"start=2026-01-01T00%3A00%3A00%2B00%3A00&"
            b"end=2026-01-02T00%3A00%3A00%2B00%3A00&bucket_seconds=300"
        )
        status, _, body = asyncio.run(request(self.app, "/api/v1/latency", query))
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertEqual(300, payload["bucket_seconds"])
        self.assertEqual([], payload["series"])

    def test_history_route_validates_filters(self):
        status, _, body = asyncio.run(
            request(self.app, "/api/v1/history", b"event_type=unknown")
        )
        self.assertEqual(400, status)
        self.assertIn("event_type", json.loads(body)["detail"])


if __name__ == "__main__":
    unittest.main()
