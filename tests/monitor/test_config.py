import unittest
from pathlib import Path

from home_internet_monitor.monitor.config import ConfigError, load_config, parse_config
from home_internet_monitor.monitor.models import TargetKind


def valid_config():
    return {
        "site": {"id": "home", "display_name": "Home", "timezone": "UTC"},
        "storage": {"database_path": "/var/lib/monitor.db"},
        "monitor": {"interval_seconds": 10, "round_timeout_seconds": 8},
        "probes": [
            {"id": "gw", "kind": "gateway", "endpoint": "auto"},
            {"id": "ip1", "kind": "external_ip", "endpoint": "1.1.1.1"},
            {"id": "ip2", "kind": "external_ip", "endpoint": "8.8.8.8"},
            {"id": "dns", "kind": "dns", "endpoint": "example.com"},
            {
                "id": "web",
                "kind": "https",
                "endpoint": "https://example.com/",
                "expected_status": 200,
            },
        ],
    }


class ConfigTests(unittest.TestCase):
    def test_repository_example_is_valid_and_contains_no_secrets(self):
        path = Path(__file__).parents[2] / "config.example.toml"
        config = load_config(path)
        self.assertEqual("home", config.site.site_id)
        self.assertEqual(5, len(config.probes))
        self.assertEqual(7, config.retention.raw_samples_days)
        self.assertIsNone(config.retention.incidents_days)
        self.assertEqual(548, config.retention.latency_aggregates_days)
        self.assertEqual(1, config.dashboard.mtbf_minimum_incident_minutes)
        self.assertEqual("24h", config.dashboard.default_timeline_window)
        self.assertEqual("1h", config.dashboard.default_latency_window)
        self.assertTrue(
            next(p for p in config.probes if p.kind is TargetKind.HTTPS).require_native_route
        )
        text = path.read_text(encoding="utf-8").lower()
        self.assertNotIn("no-ip", text)
        self.assertNotIn("@", text)

    def test_requires_two_independent_external_ip_targets(self):
        raw = valid_config()
        raw["probes"] = [p for p in raw["probes"] if p["id"] != "ip2"]
        with self.assertRaisesRegex(ConfigError, "at least 2 enabled external_ip"):
            parse_config(raw)

    def test_accepts_private_and_wildcard_dashboard_bindings(self):
        raw = valid_config()
        raw["web"] = {"host": "0.0.0.0"}
        self.assertEqual("0.0.0.0", parse_config(raw).web.host)
        raw["web"] = {"host": "192.168.1.20"}
        self.assertEqual("192.168.1.20", parse_config(raw).web.host)

    def test_rejects_public_dashboard_binding(self):
        raw = valid_config()
        raw["web"] = {"host": "8.8.8.8"}
        with self.assertRaisesRegex(ConfigError, "loopback, private, or wildcard"):
            parse_config(raw)

    def test_rejects_probe_timeout_longer_than_round(self):
        raw = valid_config()
        raw["probes"][0]["timeout_seconds"] = 8
        with self.assertRaisesRegex(ConfigError, "below the round timeout"):
            parse_config(raw)

    def test_rejects_invalid_gateway_and_external_ip_endpoints(self):
        raw = valid_config()
        raw["probes"][0]["endpoint"] = "router.local"
        with self.assertRaisesRegex(ConfigError, "gateway probes require"):
            parse_config(raw)

        raw = valid_config()
        raw["probes"][1]["endpoint"] = "not-an-ip"
        with self.assertRaisesRegex(ConfigError, "external IP probes require"):
            parse_config(raw)

    def test_retention_requires_aggregates_to_outlive_raw_samples(self):
        raw = valid_config()
        raw["retention"] = {
            "raw_samples_days": 30,
            "latency_aggregates_days": 30,
        }
        with self.assertRaisesRegex(ConfigError, "must exceed raw sample retention"):
            parse_config(raw)

    def test_rejects_invalid_dashboard_defaults(self):
        raw = valid_config()
        raw["dashboard"] = {"default_timeline_window": "1h"}
        with self.assertRaisesRegex(ConfigError, "default timeline window"):
            parse_config(raw)

        raw = valid_config()
        raw["dashboard"] = {"mtbf_minimum_incident_minutes": -1}
        with self.assertRaisesRegex(ConfigError, "MTBF minimum incident duration"):
            parse_config(raw)

if __name__ == "__main__":
    unittest.main()
