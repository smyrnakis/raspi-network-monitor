import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from home_internet_monitor.monitor.config import ServiceMonitorConfig
from home_internet_monitor.api.queries import MonitoringQueries
from home_internet_monitor.storage.repository import MonitoringRepository
from dataclasses import replace
from home_internet_monitor.storage import (
    ServiceMonitorRepository,
    connect_database,
    migrate,
)


class ServiceMonitorRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temporary.name) / "monitor.db")
        migrate(self.connection)
        self.connection.execute(
            """
            INSERT INTO sites(site_id, display_name, timezone, created_at_ms, updated_at_ms)
            VALUES ('home', 'Home', 'UTC', 0, 0)
            """
        )
        self.connection.commit()
        self.monitor = ServiceMonitorConfig(
            "remote_vpn",
            "Remote VPN",
            "openvpn_client",
            "10.8.0.2",
            None,
            None,
            failure_threshold=2,
            recovery_threshold=2,
        )
        self.repository = ServiceMonitorRepository(self.connection)
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.repository.prepare("home", (self.monitor,), self.start)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def test_confirms_down_and_recovery_with_thresholds(self):
        self.repository.record_observation(self.monitor, self.start, "up")
        self.repository.record_observation(
            self.monitor, self.start + timedelta(seconds=20), "up"
        )
        self.repository.record_observation(
            self.monitor, self.start + timedelta(seconds=40), "down",
            error_class="unreachable",
        )
        self.repository.record_observation(
            self.monitor, self.start + timedelta(seconds=60), "down",
            error_class="unreachable",
        )
        state = self.connection.execute(
            "SELECT stable_status FROM service_monitor_state"
        ).fetchone()
        self.assertEqual("down", state["stable_status"])
        incident = self.connection.execute(
            "SELECT lifecycle, error_class FROM service_incidents"
        ).fetchone()
        self.assertEqual("open", incident["lifecycle"])
        self.assertEqual("unreachable", incident["error_class"])

        self.repository.record_observation(
            self.monitor, self.start + timedelta(seconds=80), "up"
        )
        self.repository.record_observation(
            self.monitor, self.start + timedelta(seconds=100), "up"
        )
        incident = self.connection.execute(
            "SELECT lifecycle, end_reason FROM service_incidents"
        ).fetchone()
        self.assertEqual("closed", incident["lifecycle"])
        self.assertEqual("recovered", incident["end_reason"])

    def test_ping_buckets_preserve_failures_gaps_and_monitor_scope(self):
        for seconds, status, latency in [(0, "up", 10), (20, "up", 30), (40, "down", None), (60, "down", None), (180, "up", 40)]:
            self.repository.record_observation(
                self.monitor, self.start + timedelta(seconds=seconds), status, latency_ms=latency,
            )
        queries = MonitoringQueries(self.connection)
        payload = queries.service_latency("home", "remote_vpn", self.start, self.start + timedelta(minutes=4))
        points = payload["points"]
        self.assertEqual(3, len(points))
        self.assertEqual((20, 10, 30, 1, 3), (points[0]["avg_ms"], points[0]["min_ms"], points[0]["max_ms"], points[0]["failure_count"], points[0]["sample_count"]))
        self.assertIsNone(points[1]["avg_ms"])
        self.assertEqual(1, points[1]["failure_count"])
        self.assertEqual(40, points[2]["avg_ms"])
        with self.assertRaises(KeyError):
            queries.service_latency("other-site", "remote_vpn", self.start, self.start + timedelta(minutes=4))
        with self.assertRaises(ValueError):
            queries.service_latency("home", "remote_vpn", self.start, self.start + timedelta(days=7))

    def test_ping_samples_only_saved_for_icmp_and_pruned_with_raw_retention(self):
        status_only = replace(self.monitor, monitor_id="status_only", endpoint=None)
        self.repository.prepare("home", (status_only,), self.start)
        self.repository.record_observation(status_only, self.start, "down")
        self.repository.record_observation(self.monitor, self.start, "up", latency_ms=10)
        self.repository.record_observation(self.monitor, self.start + timedelta(days=10), "up", latency_ms=20)
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM service_ping_samples").fetchone()[0])
        MonitoringRepository(self.connection).apply_retention(
            "home", self.start + timedelta(days=10), raw_samples_days=7,
            incidents_days=None, latency_aggregates_days=365,
        )
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM service_ping_samples").fetchone()[0])
