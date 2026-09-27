import unittest
from datetime import datetime, timezone

from home_internet_monitor.domain.models import (
    ComponentStatus,
    ProbeOutcome,
    RoundEvidence,
    RoundStatus,
)
from home_internet_monitor.monitor.config import parse_config
from home_internet_monitor.monitor.models import CompletedRound, ProbeResult, TargetKind
from home_internet_monitor.monitor.service import MonitorService
from home_internet_monitor.storage import MonitoringRepository, connect_database, migrate


def config():
    return parse_config(
        {
            "site": {"id": "home", "display_name": "Home", "timezone": "UTC"},
            "storage": {"database_path": "/var/lib/monitor.db"},
            "monitor": {"interval_seconds": 10, "round_timeout_seconds": 8},
            "probes": [
                {"id": "gw", "kind": "gateway", "endpoint": "auto"},
                {"id": "ip1", "kind": "external_ip", "endpoint": "1.1.1.1"},
                {"id": "ip2", "kind": "external_ip", "endpoint": "8.8.8.8"},
                {"id": "dns", "kind": "dns", "endpoint": "example.com"},
                {"id": "web", "kind": "https", "endpoint": "https://example.com"},
            ],
        }
    )


class FakeExecutor:
    def __init__(self, completed):
        self.completed = completed

    async def execute(self, observed_at=None):
        return self.completed


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_flows_atomically_from_executor_to_database(self):
        connection = connect_database(":memory:")
        migrate(connection)
        repository = MonitoringRepository(connection)
        observed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        evidence = RoundEvidence(
            gateway=ComponentStatus.REACHABLE,
            external_ip=ComponentStatus.REACHABLE,
            dns=ComponentStatus.REACHABLE,
            https=ComponentStatus.REACHABLE,
        )
        completed = CompletedRound(
            observed,
            evidence,
            RoundStatus.ONLINE,
            (
                ProbeResult("gw", TargetKind.GATEWAY, ProbeOutcome.SUCCESS, 1.2),
                ProbeResult("ip1", TargetKind.EXTERNAL_IP, ProbeOutcome.SUCCESS, 2.3),
            ),
        )
        service = MonitorService(config(), repository, FakeExecutor(completed))
        service.prepare(observed)

        first = await service.run_round(observed)
        second = await service.run_round(observed)

        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(first.round_id, second.round_id)
        self.assertEqual(
            1, connection.execute("SELECT COUNT(*) FROM probe_rounds").fetchone()[0]
        )
        self.assertEqual(
            2, connection.execute("SELECT COUNT(*) FROM probe_samples").fetchone()[0]
        )
        state = repository.load_state("home")
        self.assertIs(RoundStatus.ONLINE, state.last_observed_status)
        connection.close()


if __name__ == "__main__":
    unittest.main()
