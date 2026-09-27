import asyncio
import unittest

from home_internet_monitor.domain.models import ProbeOutcome
from home_internet_monitor.monitor.commands import CommandResult
from home_internet_monitor.monitor.models import ProbeTarget, TargetKind
from home_internet_monitor.monitor.probes import DnsProbe, HttpsProbe, IcmpProbe
from home_internet_monitor.monitor.routing import LinuxRouteInspector


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_icmp_success_parses_latency(self):
        async def runner(arguments, timeout):
            self.assertEqual("ping", arguments[0])
            return CommandResult(0, "64 bytes: time=12.4 ms", "")

        result = await IcmpProbe(runner).probe(
            ProbeTarget("ip", TargetKind.EXTERNAL_IP, "1.1.1.1")
        )
        self.assertIs(ProbeOutcome.SUCCESS, result.outcome)
        self.assertEqual(12.4, result.latency_ms)

    async def test_dns_empty_answer_is_failure(self):
        async def resolver(host):
            return ()

        result = await DnsProbe(resolver).probe(
            ProbeTarget("dns", TargetKind.DNS, "example.com")
        )
        self.assertIs(ProbeOutcome.FAILURE, result.outcome)
        self.assertEqual("empty_answer", result.error_class)

    async def test_https_enforces_expected_status(self):
        result = await HttpsProbe(lambda url, timeout: (200, url)).probe(
            ProbeTarget(
                "web",
                TargetKind.HTTPS,
                "https://example.com",
                expected_status=204,
            )
        )
        self.assertIs(ProbeOutcome.FAILURE, result.outcome)
        self.assertEqual("unexpected_status", result.error_class)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_vpn_interface(self):
        async def runner(arguments, timeout):
            return CommandResult(0, "1.1.1.1 via 192.0.2.1 dev wg0 src 192.0.2.2", "")

        async def resolver(host):
            return host

        decision = await LinuxRouteInspector(
            runner=runner, resolver=resolver
        ).inspect("1.1.1.1", 1)
        self.assertFalse(decision.accepted)
        self.assertEqual("vpn_interface", decision.reason)

    async def test_accepts_physical_interface(self):
        async def runner(arguments, timeout):
            return CommandResult(0, "1.1.1.1 via 192.0.2.1 dev eth0 src 192.0.2.2", "")

        async def resolver(host):
            return host

        decision = await LinuxRouteInspector(
            runner=runner, resolver=resolver
        ).inspect("1.1.1.1", 1)
        self.assertTrue(decision.accepted)
        self.assertEqual("eth0", decision.interface)


if __name__ == "__main__":
    unittest.main()
