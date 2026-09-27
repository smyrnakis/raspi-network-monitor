"""Models shared by probe adapters and the monitoring worker."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

from home_internet_monitor.domain.models import ProbeOutcome, RoundEvidence, RoundStatus


class TargetKind(str, Enum):
    GATEWAY = "gateway"
    EXTERNAL_IP = "external_ip"
    DNS = "dns"
    HTTPS = "https"


@dataclass(frozen=True)
class ProbeTarget:
    target_id: str
    kind: TargetKind
    endpoint: str
    timeout_seconds: float = 3.0
    enabled: bool = True
    expected_status: Optional[int] = None
    require_native_route: bool = False


@dataclass(frozen=True)
class ProbeResult:
    target_id: str
    kind: TargetKind
    outcome: ProbeOutcome
    latency_ms: Optional[float] = None
    error_class: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteDecision:
    accepted: bool
    interface: Optional[str] = None
    gateway: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class CompletedRound:
    observed_at: datetime
    evidence: RoundEvidence
    status: RoundStatus
    results: Tuple[ProbeResult, ...]
