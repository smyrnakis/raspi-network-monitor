import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from home_internet_monitor.monitor.config import ServiceMonitorConfig
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
