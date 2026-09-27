"""Value objects shared by monitoring-domain operations."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple, Union


class ComponentStatus(str, Enum):
    """Normalized evidence for one connectivity component."""

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ProbeOutcome(str, Enum):
    """Outcome of one target probe before component aggregation."""

    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class RoundStatus(str, Enum):
    """Immediate classification of one completed probe round."""

    ONLINE = "online"
    INTERNET_DOWN = "internet_down"
    GATEWAY_UNREACHABLE = "gateway_unreachable"
    DNS_FAILURE = "dns_failure"
    PARTIAL_CONNECTIVITY = "partial_connectivity"
    MONITORING_UNKNOWN = "monitoring_unknown"


class IncidentEndReason(str, Enum):
    """Why a confirmed incident stopped being the active incident."""

    RECOVERED = "recovered"
    CATEGORY_TRANSITION = "category_transition"
    MONITORING_UNKNOWN = "monitoring_unknown"


@dataclass(frozen=True)
class RoundEvidence:
    """Normalized component evidence used to classify a completed round."""

    gateway: ComponentStatus
    external_ip: ComponentStatus
    dns: ComponentStatus
    https: ComponentStatus
    timing_trusted: bool = True
    route_trusted: bool = True
    monitoring_stale: bool = False


@dataclass(frozen=True)
class OpenIncident:
    """The incident currently owned by the state machine."""

    status: RoundStatus
    observed_start: datetime
    confirmed_start: datetime


@dataclass(frozen=True)
class MonitoringState:
    """Serializable state required to process the next classified round."""

    stable_status: RoundStatus = RoundStatus.MONITORING_UNKNOWN
    pending_status: Optional[RoundStatus] = None
    pending_count: int = 0
    pending_started_at: Optional[datetime] = None
    open_incident: Optional[OpenIncident] = None
    last_observed_at: Optional[datetime] = None
    last_observed_status: Optional[RoundStatus] = None


@dataclass(frozen=True)
class StableStatusChanged:
    """A confirmed change used to build non-overlapping status intervals."""

    previous: RoundStatus
    current: RoundStatus
    observed_at: datetime
    confirmed_at: datetime


@dataclass(frozen=True)
class IncidentOpened:
    """A confirmed incident-opening event."""

    incident: OpenIncident


@dataclass(frozen=True)
class IncidentEnded:
    """A confirmed or interrupted incident-ending event."""

    incident: OpenIncident
    observed_end: datetime
    confirmed_end: Optional[datetime]
    reason: IncidentEndReason


DomainEvent = Union[StableStatusChanged, IncidentOpened, IncidentEnded]


@dataclass(frozen=True)
class StateMachineResult:
    """New state plus persistence events produced by one observation."""

    state: MonitoringState
    events: Tuple[DomainEvent, ...] = ()
