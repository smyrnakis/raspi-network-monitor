import json
import tempfile
import unittest
from pathlib import Path

from home_internet_monitor.monitor.config import ConfigError, parse_config
from home_internet_monitor.monitor.settings import RuntimeSettingsStore


def base_config(directory: Path):
    return parse_config(
        {
            "site": {"id": "home", "display_name": "Home", "timezone": "UTC"},
            "storage": {"database_path": str((directory / "monitor.db").resolve())},
            "monitor": {"interval_seconds": 10, "round_timeout_seconds": 8},
            "service_monitors": [
                {
                    "id": "remote_vpn",
                    "label": "Remote VPN",
                    "kind": "openvpn_client",
                    "endpoint": "10.8.0.2",
                    "interval_seconds": 20,
                    "timeout_seconds": 3,
                    "failure_threshold": 3,
                    "recovery_threshold": 2,
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


class RuntimeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.store = RuntimeSettingsStore(base_config(self.directory))

    def tearDown(self):
        self.temporary.cleanup()

    def test_save_round_trips_validated_settings_and_requests_restart(self):
        payload = self.store.read()
        payload["monitor"]["interval_seconds"] = 20
        payload["monitor"]["round_timeout_seconds"] = 9
        payload["probes"][1]["endpoint"] = "9.9.9.9"
        payload["retention"]["raw_samples_days"] = 7
        payload["retention"]["incidents_days"] = None
        payload["dashboard"]["hide_short_incidents"] = False
        payload["dashboard"]["mtbf_minimum_incident_minutes"] = 5
        payload["dashboard"]["default_timeline_window"] = "7d"
        payload["dashboard"]["default_latency_window"] = "24h"
        payload["service_monitors"][0]["interval_seconds"] = 30
        payload["service_monitors"][0]["failure_threshold"] = 4
        payload["service_monitors"][0]["dashboard"] = "detailed"

        saved = self.store.save(payload)

        self.assertEqual(1, saved["revision"])
        self.assertTrue(self.store.restart_request_path.exists())
        effective = self.store.load_config()
        self.assertEqual(20, effective.monitor.interval_seconds)
        self.assertEqual("9.9.9.9", effective.probes[1].endpoint)
        self.assertEqual(7, effective.retention.raw_samples_days)
        self.assertIsNone(effective.retention.incidents_days)
        self.assertFalse(effective.dashboard.hide_short_incidents)
        self.assertEqual(5, effective.dashboard.mtbf_minimum_incident_minutes)
        self.assertEqual("7d", effective.dashboard.default_timeline_window)
        self.assertEqual("24h", effective.dashboard.default_latency_window)
        self.assertEqual(30, effective.service_monitors[0].interval_seconds)
        self.assertEqual(4, effective.service_monitors[0].failure_threshold)
        self.assertEqual("detailed", effective.service_monitors[0].dashboard)
        self.assertTrue(self.store.consume_restart_request())
        self.assertFalse(self.store.consume_restart_request())

    def test_invalid_update_does_not_replace_existing_settings(self):
        valid = self.store.read()
        self.store.save(valid)
        before = self.store.path.read_text(encoding="utf-8")
        invalid = json.loads(before)
        invalid["probes"][1]["endpoint"] = "not-an-ip"

        with self.assertRaisesRegex(ConfigError, "require an IP address"):
            self.store.save(invalid)

        self.assertEqual(before, self.store.path.read_text(encoding="utf-8"))

    def test_probe_identity_and_required_probe_counts_cannot_be_bypassed(self):
        payload = self.store.read()
        payload["probes"][0]["kind"] = "https"
        with self.assertRaisesRegex(ConfigError, "kind cannot be changed"):
            self.store.save(payload)

        payload = self.store.read()
        payload["probes"][1]["enabled"] = False
        with self.assertRaisesRegex(ConfigError, "at least 2 enabled external_ip"):
            self.store.save(payload)

        payload = self.store.read()
        payload["service_monitors"][0]["kind"] = "https"
        with self.assertRaisesRegex(ConfigError, "service monitor kind cannot be changed"):
            self.store.save(payload)

        payload = self.store.read()
        payload["service_monitors"][0]["endpoint"] = None
        payload["service_monitors"][0]["status_file"] = None
        payload["service_monitors"][0]["client_name"] = None
        with self.assertRaisesRegex(ConfigError, "requires endpoint"):
            self.store.save(payload)

    def test_settings_from_older_release_without_retention_still_load(self):
        payload = self.store.read()
        del payload["retention"]
        del payload["dashboard"]
        del payload["service_monitors"]
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text(json.dumps(payload), encoding="utf-8")

        effective = self.store.load_config()

        self.assertEqual(7, effective.retention.raw_samples_days)
        self.assertIsNone(effective.retention.incidents_days)
        self.assertTrue(effective.dashboard.hide_short_incidents)
        self.assertEqual(1, effective.dashboard.mtbf_minimum_incident_minutes)
        self.assertEqual("24h", effective.dashboard.default_timeline_window)
        self.assertEqual("1h", effective.dashboard.default_latency_window)
        self.assertEqual("10.8.0.2", effective.service_monitors[0].endpoint)


if __name__ == "__main__":
    unittest.main()
