"""Table-driven tests for immediate round classification."""

import unittest

from home_internet_monitor.domain import (
    ComponentStatus as C,
    RoundEvidence,
    RoundStatus as S,
    classify_round,
)


class ClassifyRoundTests(unittest.TestCase):
    def test_primary_classification_table(self) -> None:
        cases = [
            ("healthy", C.REACHABLE, C.REACHABLE, C.REACHABLE, C.REACHABLE, S.ONLINE),
            (
                "healthy without gateway probe",
                C.NOT_APPLICABLE,
                C.REACHABLE,
                C.REACHABLE,
                C.REACHABLE,
                S.ONLINE,
            ),
            (
                "independent external failures",
                C.REACHABLE,
                C.UNREACHABLE,
                C.UNKNOWN,
                C.UNREACHABLE,
                S.INTERNET_DOWN,
            ),
            (
                "gateway and external failures",
                C.UNREACHABLE,
                C.UNREACHABLE,
                C.UNKNOWN,
                C.UNREACHABLE,
                S.GATEWAY_UNREACHABLE,
            ),
            (
                "gateway failure with one external unknown",
                C.UNREACHABLE,
                C.UNKNOWN,
                C.UNKNOWN,
                C.UNREACHABLE,
                S.GATEWAY_UNREACHABLE,
            ),
            (
                "gateway contradicted by external IP",
                C.UNREACHABLE,
                C.REACHABLE,
                C.REACHABLE,
                C.UNKNOWN,
                S.PARTIAL_CONNECTIVITY,
            ),
            (
                "gateway contradicted by HTTPS",
                C.UNREACHABLE,
                C.UNREACHABLE,
                C.REACHABLE,
                C.REACHABLE,
                S.PARTIAL_CONNECTIVITY,
            ),
            (
                "system DNS failure",
                C.REACHABLE,
                C.REACHABLE,
                C.UNREACHABLE,
                C.UNKNOWN,
                S.DNS_FAILURE,
            ),
            (
                "DNS failure also breaks HTTPS",
                C.REACHABLE,
                C.REACHABLE,
                C.UNREACHABLE,
                C.UNREACHABLE,
                S.DNS_FAILURE,
            ),
            (
                "ICMP filtered while HTTPS works",
                C.REACHABLE,
                C.UNREACHABLE,
                C.REACHABLE,
                C.REACHABLE,
                S.PARTIAL_CONNECTIVITY,
            ),
            (
                "HTTPS endpoints fail",
                C.REACHABLE,
                C.REACHABLE,
                C.REACHABLE,
                C.UNREACHABLE,
                S.PARTIAL_CONNECTIVITY,
            ),
            (
                "DNS evidence incomplete",
                C.REACHABLE,
                C.REACHABLE,
                C.UNKNOWN,
                C.REACHABLE,
                S.PARTIAL_CONNECTIVITY,
            ),
            (
                "all evidence unknown",
                C.UNKNOWN,
                C.UNKNOWN,
                C.UNKNOWN,
                C.UNKNOWN,
                S.MONITORING_UNKNOWN,
            ),
            (
                "gateway alone fails",
                C.UNREACHABLE,
                C.UNKNOWN,
                C.UNKNOWN,
                C.UNKNOWN,
                S.MONITORING_UNKNOWN,
            ),
            (
                "one external group fails",
                C.REACHABLE,
                C.UNREACHABLE,
                C.UNKNOWN,
                C.UNKNOWN,
                S.MONITORING_UNKNOWN,
            ),
            (
                "gateway disabled during external failure",
                C.NOT_APPLICABLE,
                C.UNREACHABLE,
                C.UNKNOWN,
                C.UNREACHABLE,
                S.MONITORING_UNKNOWN,
            ),
        ]

        for name, gateway, external_ip, dns, https, expected in cases:
            with self.subTest(name=name):
                evidence = RoundEvidence(
                    gateway=gateway,
                    external_ip=external_ip,
                    dns=dns,
                    https=https,
                )
                self.assertEqual(classify_round(evidence), expected)

    def test_untrusted_context_forces_unknown(self) -> None:
        healthy = {
            "gateway": C.REACHABLE,
            "external_ip": C.REACHABLE,
            "dns": C.REACHABLE,
            "https": C.REACHABLE,
        }

        cases = [
            ("clock", {"timing_trusted": False}),
            ("route", {"route_trusted": False}),
            ("stale", {"monitoring_stale": True}),
        ]

        for name, override in cases:
            with self.subTest(name=name):
                evidence = RoundEvidence(**healthy, **override)
                self.assertEqual(
                    classify_round(evidence),
                    S.MONITORING_UNKNOWN,
                )


if __name__ == "__main__":
    unittest.main()
