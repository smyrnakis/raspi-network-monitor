"""Deterministic classification of normalized probe evidence."""

from .models import ComponentStatus, RoundEvidence, RoundStatus


def classify_round(evidence: RoundEvidence) -> RoundStatus:
    """Classify one round without applying confirmation hysteresis.

    The order implements the precedence defined in
    ``docs/monitoring-semantics.md``. It intentionally prefers unknown or
    partial states over a confident outage when evidence is incomplete or
    contradictory.
    """

    if (
        not evidence.timing_trusted
        or not evidence.route_trusted
        or evidence.monitoring_stale
    ):
        return RoundStatus.MONITORING_UNKNOWN

    gateway = evidence.gateway
    external_ip = evidence.external_ip
    dns = evidence.dns
    https = evidence.https

    if gateway is ComponentStatus.UNREACHABLE:
        if (
            external_ip is ComponentStatus.REACHABLE
            or https is ComponentStatus.REACHABLE
        ):
            return RoundStatus.PARTIAL_CONNECTIVITY

        if (
            external_ip is ComponentStatus.UNREACHABLE
            or https is ComponentStatus.UNREACHABLE
        ):
            return RoundStatus.GATEWAY_UNREACHABLE

        return RoundStatus.MONITORING_UNKNOWN

    if (
        gateway is ComponentStatus.REACHABLE
        and external_ip is ComponentStatus.UNREACHABLE
        and https is ComponentStatus.UNREACHABLE
    ):
        return RoundStatus.INTERNET_DOWN

    if (
        external_ip is ComponentStatus.REACHABLE
        and dns is ComponentStatus.UNREACHABLE
    ):
        return RoundStatus.DNS_FAILURE

    gateway_is_healthy = gateway in {
        ComponentStatus.REACHABLE,
        ComponentStatus.NOT_APPLICABLE,
    }
    if (
        gateway_is_healthy
        and external_ip is ComponentStatus.REACHABLE
        and dns is ComponentStatus.REACHABLE
        and https is ComponentStatus.REACHABLE
    ):
        return RoundStatus.ONLINE

    if (
        external_ip is ComponentStatus.REACHABLE
        or https is ComponentStatus.REACHABLE
    ):
        return RoundStatus.PARTIAL_CONNECTIVITY

    return RoundStatus.MONITORING_UNKNOWN
