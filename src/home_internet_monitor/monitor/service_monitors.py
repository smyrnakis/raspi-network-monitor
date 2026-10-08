"""Independent service checks that never affect native-WAN classification."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from home_internet_monitor.domain.models import ProbeOutcome
from home_internet_monitor.storage import ServiceMonitorRepository

from .config import ServiceMonitorConfig
from .models import ProbeTarget, TargetKind
from .probes import IcmpProbe


@dataclass(frozen=True)
class ServiceObservation:
    status: str
    latency_ms: Optional[float] = None
    error_class: Optional[str] = None


class OpenVpnClientCheck:
    def __init__(self, icmp: Optional[IcmpProbe] = None) -> None:
        self._icmp = icmp or IcmpProbe()

    async def check(self, monitor: ServiceMonitorConfig) -> ServiceObservation:
        session_present: Optional[bool] = None
        status_error: Optional[str] = None
        if monitor.status_file is not None and monitor.client_name is not None:
            try:
                session_present = await asyncio.to_thread(
                    _client_is_connected, monitor.status_file, monitor.client_name
                )
            except PermissionError:
                status_error = "status_permission_denied"
            except FileNotFoundError:
                status_error = "status_file_missing"
            except (OSError, ValueError):
                status_error = "status_unavailable"

        reachability: Optional[ProbeOutcome] = None
        latency_ms: Optional[float] = None
        ping_error: Optional[str] = None
        if monitor.endpoint is not None:
            result = await self._icmp.probe(
                ProbeTarget(
                    target_id=monitor.monitor_id,
                    kind=TargetKind.EXTERNAL_IP,
                    endpoint=monitor.endpoint,
                    timeout_seconds=monitor.timeout_seconds,
                    require_native_route=False,
                )
            )
            reachability = result.outcome
            latency_ms = result.latency_ms
            ping_error = result.error_class

        if session_present is False:
            return ServiceObservation("down", latency_ms, "client_session_absent")
        if session_present is True:
            if reachability is ProbeOutcome.FAILURE:
                return ServiceObservation("degraded", latency_ms, ping_error)
            if reachability is ProbeOutcome.UNKNOWN:
                return ServiceObservation("degraded", latency_ms, ping_error)
            return ServiceObservation("up", latency_ms)
        if reachability is ProbeOutcome.SUCCESS:
            return ServiceObservation("up", latency_ms, status_error)
        if reachability is ProbeOutcome.FAILURE and monitor.status_file is None:
            return ServiceObservation("down", latency_ms, ping_error)
        return ServiceObservation("unknown", latency_ms, status_error or ping_error)


class ServiceMonitorRunner:
    def __init__(
        self,
        site_id: str,
        monitors: tuple[ServiceMonitorConfig, ...],
        repository: ServiceMonitorRepository,
        checker: Optional[OpenVpnClientCheck] = None,
    ) -> None:
        self._site_id = site_id
        self._monitors = tuple(monitor for monitor in monitors if monitor.enabled)
        self._repository = repository
        self._checker = checker or OpenVpnClientCheck()

    def prepare(self, now: datetime) -> None:
        self._repository.prepare(self._site_id, self._monitors, now)

    async def run_due(self, now: Optional[datetime] = None) -> int:
        observed_at = now or datetime.now(timezone.utc)
        due = []
        for monitor in self._monitors:
            last_checked = self._repository.last_checked(monitor.monitor_id)
            if last_checked is None or observed_at >= last_checked + timedelta(
                seconds=monitor.interval_seconds
            ):
                due.append(monitor)
        if not due:
            return 0
        observations = await asyncio.gather(
            *(self._checker.check(monitor) for monitor in due)
        )
        for monitor, observation in zip(due, observations):
            self._repository.record_observation(
                monitor,
                observed_at,
                observation.status,
                latency_ms=observation.latency_ms,
                error_class=observation.error_class,
            )
        return len(due)


def _client_is_connected(status_file: Path, client_name: str) -> bool:
    content = status_file.read_text(encoding="utf-8", errors="replace")
    saw_status_record = False
    for raw_line in content.splitlines():
        if raw_line.startswith("CLIENT_LIST,"):
            saw_status_record = True
            fields = raw_line.split(",")
        elif raw_line.startswith("CLIENT_LIST\t"):
            saw_status_record = True
            fields = raw_line.split("\t")
        else:
            continue
        if len(fields) > 1 and fields[1] == client_name:
            return True
    if "OpenVPN CLIENT LIST" not in content and not saw_status_record:
        raise ValueError("unrecognized OpenVPN status format")
    return False
