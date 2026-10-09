import asyncio
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from home_internet_monitor.domain.models import ComponentStatus, RoundEvidence, RoundStatus
from home_internet_monitor.monitor.models import CompletedRound
from home_internet_monitor.monitor.config import ServiceMonitorConfig, parse_config
from home_internet_monitor.monitor.service_monitors import ServiceObservation
from home_internet_monitor.storage import (
    ServiceMonitorRepository,
    connect_database,
    migrate,
)

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
        self.event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.event_loop)
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
                "service_monitors": [
                    {
                        "id": "remote_vpn",
                        "label": "Remote VPN",
                        "kind": "openvpn_client",
                        "endpoint": "10.8.0.2",
                        "interval_seconds": 20,
                        "timeout_seconds": 3,
                    }
                ],
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
        self.event_loop.close()
        asyncio.set_event_loop(None)

    def test_status_route_returns_initialized_site(self):
        status, _, body = asyncio.run(request(self.app, "/api/v1/status"))
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertEqual("home", payload["site"]["site_id"])
        self.assertEqual("monitoring_unknown", payload["stable_status"])
        self.assertTrue(payload["hostname"])
        self.assertEqual("0.5.11", payload["version"])
        self.assertTrue(payload["dashboard"]["hide_short_incidents"])
        self.assertEqual(1, payload["dashboard"]["mtbf_minimum_incident_minutes"])
        self.assertEqual("24h", payload["dashboard"]["default_timeline_window"])
        self.assertEqual("1h", payload["dashboard"]["default_latency_window"])

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

    def test_service_summary_and_timeline_routes(self):
        connection = connect_database(self.path)
        repository = ServiceMonitorRepository(connection)
        monitor = ServiceMonitorConfig(
            "remote_vpn", "Remote VPN", "openvpn_client", "10.8.0.2", None, None,
            failure_threshold=1, recovery_threshold=1,
        )
        now = datetime.now(timezone.utc) - timedelta(seconds=5)
        repository.prepare("home", (monitor,), now)
        repository.record_observation(monitor, now, "up", latency_ms=22.5)
        repository.record_observation(
            monitor,
            now + timedelta(seconds=1),
            "down",
            error_class="icmp_timeout",
        )
        repository.record_observation(
            monitor,
            now + timedelta(seconds=2),
            "up",
            latency_ms=21.0,
        )
        connection.close()

        status, _, body = asyncio.run(request(self.app, "/api/v1/services"))
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertEqual("remote_vpn", payload["items"][0]["id"])
        self.assertEqual("up", payload["items"][0]["status"])
        self.assertEqual("closed", payload["items"][0]["last_incident"]["lifecycle"])
        self.assertIsNotNone(payload["items"][0]["last_incident"]["confirmed_start"])
        self.assertEqual("recovered", payload["items"][0]["last_incident"]["end_reason"])

        query = (
            b"start=2026-01-01T00%3A00%3A00%2B00%3A00&"
            b"end=2027-01-01T00%3A00%3A00%2B00%3A00"
        )
        status, _, body = asyncio.run(
            request(self.app, "/api/v1/services/remote_vpn/timeline", query)
        )
        self.assertEqual(200, status)
        self.assertTrue(json.loads(body)["segments"])

        status, _, body = asyncio.run(request(self.app, "/api/v1/services/remote_vpn/latency"))
        self.assertEqual(200, status)
        ping = json.loads(body)["points"]
        self.assertEqual(21.75, ping[0]["avg_ms"])
        self.assertEqual(1, ping[0]["failure_count"])
        status, _, _ = asyncio.run(request(self.app, "/api/v1/services/missing/latency"))
        self.assertEqual(404, status)
        status, _, _ = asyncio.run(request(self.app, "/api/v1/services/remote_vpn/latency", query))
        self.assertEqual(400, status)

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
        self.assertIn(b"timeline-incidents", body)
        self.assertIn(b"Ping latency", body)
        self.assertIn(b'id="mtbf-value"', body)
        self.assertIn(b'data-latency-window="1h"', body)
        self.assertIn(b"latency-tooltip", body)
        self.assertIn(
            b'class="latency-window active" data-latency-window="1h"', body
        )
        self.assertNotIn(b"classified-value", body)
        self.assertNotIn(b"unknown-value", body)
        self.assertNotIn(b"Download CSV", body)
        self.assertEqual(2, body.count(b">Detailed history<"))
        self.assertIn(b'id="status-service-summary"', body)
        self.assertGreater(body.find(b'id="services-section"'), body.find(b'id="gaps-heading"'))

        status, headers, body = asyncio.run(request(self.app, "/assets/app.js"))
        self.assertEqual(200, status)
        self.assertIn(b"javascript", headers[b"content-type"])
        self.assertIn(b"selectedRange", body)
        self.assertIn(b"timelineBucketCount", body)
        self.assertIn(b"renderTimelineIncidents", body)
        self.assertIn(b"incidentDescription", body)
        self.assertIn(b"formatIncidentRange", body)
        self.assertIn(b"function validDate(value)", body)
        self.assertIn(b'if (!date) return "Unknown time"', body)
        self.assertIn(b"INCIDENT_TAG_LABELS", body)
        self.assertIn(b"IMPAIRED_STATUSES", body)
        self.assertIn(b"impairmentColor", body)
        self.assertIn(b"TOOLTIP_HIDE_DELAY_MS", body)
        self.assertIn(b'max-width: 540px', body)
        self.assertIn(b"setPointerCapture", body)
        self.assertIn(b"latencyTooltipLabel", body)
        self.assertIn(b"formatLatencyTooltipTime", body)
        self.assertIn(b"formatMetricDuration", body)
        self.assertIn(b"applyDashboardPreferences", body)
        self.assertIn(b"latency-incident", body)
        self.assertIn(b"renderStatusServices", body)
        self.assertIn(b"link.dataset.display = item.dashboard", body)
        self.assertIn(b'/api/v1/incidents?limit=5', body)
        self.assertIn(b'/api/v1/gaps?limit=5', body)
        self.assertEqual(2, body.count(b"minimum_duration_seconds=${minimumDuration}"))
        self.assertIn(b"focus_type=incident", body)

        status, headers, body = asyncio.run(request(self.app, "/service"))
        self.assertEqual(200, status)
        self.assertIn(b"text/html", headers[b"content-type"])
        self.assertIn(b'class="window-button service-window"', body)
        self.assertIn(b"Striped time has no monitoring data", body)
        self.assertIn(b'id="service-availability-note"', body)
        self.assertIn(b'id="service-settings-form"', body)
        self.assertIn(b"Detection methods", body)
        self.assertIn(b"Confirm disconnection after", body)

        status, headers, body = asyncio.run(request(self.app, "/assets/service.js"))
        self.assertEqual(200, status)
        self.assertIn(b"javascript", headers[b"content-type"])
        self.assertIn(b"fillTimelineGaps", body)
        self.assertIn(b"of selected window monitored", body)
        self.assertIn(b"incidentCard", body)
        self.assertIn(b"detectionDescription", body)
        self.assertIn(b"thresholdForSeconds", body)
        self.assertIn(b'api("/api/v1/settings"', body)

        status, headers, body = asyncio.run(request(self.app, "/settings"))
        self.assertEqual(200, status)
        self.assertIn(b"text/html", headers[b"content-type"])
        self.assertIn(b"Follow device theme", body)
        self.assertIn(b'data-theme-choice="light"', body)
        self.assertIn(b'data-theme-choice="dark"', body)
        self.assertIn(b"Check schedule", body)
        self.assertIn(b"Probe targets", body)
        self.assertIn(b"Data retention", body)
        self.assertIn(b"Hide events shorter than one minute", body)
        self.assertIn(b"MTBF sensitivity", body)
        self.assertIn(b"Default dashboard windows", body)
        self.assertIn(b'id="mtbf-minimum-incident-minutes"', body)
        self.assertIn(b'id="default-timeline-window"', body)
        self.assertIn(b'id="default-latency-window"', body)

        status, headers, body = asyncio.run(request(self.app, "/assets/settings.js"))
        self.assertEqual(200, status)
        self.assertIn(b"mtbf_minimum_incident_minutes", body)
        self.assertIn(b"default_timeline_window", body)
        self.assertIn(b"default_latency_window", body)
        self.assertIn(b"service_monitors: settings.service_monitors", body)

        status, headers, body = asyncio.run(request(self.app, "/assets/theme.js"))
        self.assertEqual(200, status)
        self.assertIn(b"javascript", headers[b"content-type"])
        self.assertIn(b"network-monitor-theme", body)

        status, headers, body = asyncio.run(request(self.app, "/assets/styles.css"))
        self.assertEqual(200, status)
        self.assertIn(b"text/css", headers[b"content-type"])
        self.assertIn(b"touch-action: pan-y", body)
        self.assertIn(b".history-event-content", body)
        self.assertIn(b"size: A4 portrait", body)
        self.assertIn(b'"metrics latency"', body)
        self.assertIn(b'"timeline timeline"', body)
        self.assertIn(b"border-bottom: 0.5pt solid #b8c6bf", body)
        self.assertIn(b".status-service-item > i", body)
        self.assertIn(b"indicator-breathe 1.2s", body)
        self.assertIn(b".status-service-item > span", body)
        self.assertIn(b"display: contents", body)
        self.assertIn(b".service-incident-facts", body)
        self.assertIn(b".service-settings-group", body)

        status, headers, body = asyncio.run(request(self.app, "/history"))
        self.assertEqual(200, status)
        self.assertIn(b"text/html", headers[b"content-type"])
        self.assertIn(b"Download filtered CSV", body)
        self.assertIn(b"View report", body)
        self.assertIn(b"20 events per page", body)

        status, headers, body = asyncio.run(request(self.app, "/assets/history.js"))
        self.assertEqual(200, status)
        self.assertIn(b"javascript", headers[b"content-type"])
        self.assertIn(b"/api/v1/history.csv", body)
        self.assertIn(b"focus_type", body)
        self.assertIn(b"Failed when confirmed", body)
        self.assertIn(b'ensureDefaultTime(elements.startDate, elements.startTime, "00:00")', body)
        self.assertIn(b'ensureDefaultTime(elements.endDate, elements.endTime, "23:59")', body)
        self.assertIn(b'group.addEventListener("focusout"', body)

        status, headers, body = asyncio.run(request(self.app, "/report"))
        self.assertEqual(200, status)
        self.assertIn(b"text/html", headers[b"content-type"])
        self.assertIn(b"Network monitoring report", body)
        self.assertIn(b"Print / Save as PDF", body)
        self.assertIn(b"Classification", body)
        self.assertIn(b'id="report-period"', body)
        self.assertIn(b"report-timeline-card", body)
        self.assertIn(b"report-latency-card", body)

        status, headers, body = asyncio.run(request(self.app, "/assets/report.js"))
        self.assertEqual(200, status)
        self.assertIn(b"javascript", headers[b"content-type"])
        self.assertIn(b"allHistory", body)
        self.assertIn(b"Monitoring gap", body)
        self.assertIn(b"7 * 24 * 3600", body)
        self.assertIn(b"Event status/reason", body)

    def test_manual_test_requires_action_header(self):
        status, _, body = asyncio.run(
            request(self.app, "/api/v1/test", method="POST")
        )
        self.assertEqual(403, status)
        self.assertIn("manual test header required", json.loads(body)["detail"])

    def test_manual_service_test_requires_action_header(self):
        status, _, _ = asyncio.run(
            request(self.app, "/api/v1/services/remote_vpn/test", method="POST")
        )
        self.assertEqual(403, status)

    def test_manual_service_test_returns_result_without_changing_history(self):
        connection = connect_database(self.path)
        before = list(connection.iterdump())
        connection.close()
        with patch(
            "home_internet_monitor.api.app.OpenVpnClientCheck.check",
            new=AsyncMock(return_value=ServiceObservation("up", 42.5)),
        ) as checker:
            status, _, body = asyncio.run(request(
                self.app, "/api/v1/services/remote_vpn/test", method="POST",
                headers=[(b"x-monitor-action", b"manual-test")],
            ))
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertEqual("up", payload["status"])
        self.assertEqual(42.5, payload["last_latency_ms"])
        self.assertTrue(payload["last_checked"].endswith("Z"))
        checker.assert_awaited_once()
        self.assertEqual("remote_vpn", checker.call_args.args[0].monitor_id)
        connection = connect_database(self.path)
        self.assertEqual(before, list(connection.iterdump()))
        connection.close()

    def test_manual_service_test_rejects_missing_monitor(self):
        status, _, _ = asyncio.run(request(
            self.app, "/api/v1/services/missing/test", method="POST",
            headers=[(b"x-monitor-action", b"manual-test")],
        ))
        self.assertEqual(404, status)

    def test_manual_service_test_reports_timeout(self):
        with patch(
            "home_internet_monitor.api.app.OpenVpnClientCheck.check",
            new=AsyncMock(side_effect=asyncio.TimeoutError),
        ):
            status, _, _ = asyncio.run(request(
                self.app, "/api/v1/services/remote_vpn/test", method="POST",
                headers=[(b"x-monitor-action", b"manual-test")],
            ))
        self.assertEqual(504, status)

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
        self.assertEqual([], payload["incidents"])

    def test_history_route_validates_filters(self):
        status, _, body = asyncio.run(
            request(self.app, "/api/v1/history", b"event_type=unknown")
        )
        self.assertEqual(400, status)
        self.assertIn("event_type", json.loads(body)["detail"])


if __name__ == "__main__":
    unittest.main()
