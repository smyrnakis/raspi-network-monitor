import asyncio
import tempfile
import unittest
from pathlib import Path

from home_internet_monitor.domain.models import ProbeOutcome
from home_internet_monitor.monitor.config import ServiceMonitorConfig
from home_internet_monitor.monitor.models import ProbeResult, TargetKind
from home_internet_monitor.monitor.service_monitors import (
    OpenVpnClientCheck,
    _client_is_connected,
)


class FakeIcmp:
    def __init__(self, outcome, error=None):
        self.outcome = outcome
        self.error = error

    async def probe(self, target):
        return ProbeResult(
            target.target_id,
            TargetKind.EXTERNAL_IP,
            self.outcome,
            latency_ms=24.5,
            error_class=self.error,
        )


def monitor(**changes):
    values = {
        "monitor_id": "remote_vpn",
        "label": "Remote VPN",
        "kind": "openvpn_client",
        "endpoint": "10.8.0.2",
        "status_file": None,
        "client_name": None,
    }
    values.update(changes)
    return ServiceMonitorConfig(**values)


class OpenVpnClientCheckTests(unittest.TestCase):
    def test_reachability_only_maps_success_and_failure(self):
        success = asyncio.run(
            OpenVpnClientCheck(FakeIcmp(ProbeOutcome.SUCCESS)).check(monitor())
        )
        failure = asyncio.run(
            OpenVpnClientCheck(FakeIcmp(ProbeOutcome.FAILURE, "unreachable")).check(
                monitor()
            )
        )
        self.assertEqual("up", success.status)
        self.assertEqual(24.5, success.latency_ms)
        self.assertEqual("down", failure.status)

    def test_session_present_with_failed_reachability_is_degraded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.log"
            path.write_text(
                "OpenVPN CLIENT LIST\n"
                "CLIENT_LIST,remote-client,198.51.100.1:1000,10.8.0.2,\n"
                "END\n",
                encoding="utf-8",
            )
            result = asyncio.run(
                OpenVpnClientCheck(
                    FakeIcmp(ProbeOutcome.FAILURE, "unreachable")
                ).check(monitor(status_file=path, client_name="remote-client"))
            )
        self.assertEqual("degraded", result.status)

    def test_status_parser_supports_comma_and_tab_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.log"
            path.write_text(
                "OpenVPN CLIENT LIST\nCLIENT_LIST,remote-client,real,10.8.0.2\nEND\n",
                encoding="utf-8",
            )
            self.assertTrue(_client_is_connected(path, "remote-client"))
            path.write_text(
                "OpenVPN CLIENT LIST\nCLIENT_LIST\tremote-client\treal\t10.8.0.2\nEND\n",
                encoding="utf-8",
            )
            self.assertTrue(_client_is_connected(path, "remote-client"))
            self.assertFalse(_client_is_connected(path, "another"))
