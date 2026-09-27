import asyncio
import unittest
from datetime import datetime, timezone

from home_internet_monitor.domain.models import ProbeOutcome, RoundStatus
from home_internet_monitor.monitor.models import (
    ProbeResult,
    ProbeTarget,
    RouteDecision,
    TargetKind,
)
from home_internet_monitor.monitor.rounds import RoundExecutor
from home_internet_monitor.monitor.scheduler import NonOverlappingScheduler


class FakeRoutes:
    def __init__(self, accepted=True):
        self.accepted = accepted

    async def inspect(self, endpoint, timeout_seconds):
        return RouteDecision(
            self.accepted,
            interface="eth0" if self.accepted else "wg0",
            reason=None if self.accepted else "vpn_interface",
        )

    async def default_gateway(self, timeout_seconds):
        return RouteDecision(True, interface="eth0", gateway="192.0.2.1")


class SuccessfulProbe:
    async def probe(self, target):
        return ProbeResult(target.target_id, target.kind, ProbeOutcome.SUCCESS, 1.0)


def targets():
    return (
        ProbeTarget("gw", TargetKind.GATEWAY, "auto"),
        ProbeTarget("ip1", TargetKind.EXTERNAL_IP, "1.1.1.1", require_native_route=True),
        ProbeTarget("ip2", TargetKind.EXTERNAL_IP, "8.8.8.8", require_native_route=True),
        ProbeTarget("dns", TargetKind.DNS, "example.com"),
        ProbeTarget("web", TargetKind.HTTPS, "https://example.com", require_native_route=True),
    )


class RoundTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_evidence_is_online(self):
        probe = SuccessfulProbe()
        adapters = {kind: probe for kind in TargetKind}
        completed = await RoundExecutor(
            targets(), FakeRoutes(), adapters, max_concurrency=2
        ).execute(datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertIs(RoundStatus.ONLINE, completed.status)
        self.assertTrue(completed.evidence.route_trusted)
        self.assertEqual(5, len(completed.results))

    async def test_vpn_route_makes_round_unknown(self):
        probe = SuccessfulProbe()
        adapters = {kind: probe for kind in TargetKind}
        completed = await RoundExecutor(
            targets(), FakeRoutes(accepted=False), adapters
        ).execute(datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertIs(RoundStatus.MONITORING_UNKNOWN, completed.status)
        self.assertFalse(completed.evidence.route_trusted)

    async def test_scheduler_prevents_overlap(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def callback():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()

        scheduler = NonOverlappingScheduler(callback, 10)
        first = asyncio.create_task(scheduler.run_once())
        await entered.wait()
        self.assertFalse(await scheduler.run_once())
        release.set()
        self.assertTrue(await first)
        self.assertEqual(1, calls)


if __name__ == "__main__":
    unittest.main()
