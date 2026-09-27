"""Pure confirmation and recovery state machine."""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Tuple

from .models import (
    DomainEvent,
    IncidentEnded,
    IncidentEndReason,
    IncidentOpened,
    MonitoringState,
    OpenIncident,
    RoundStatus,
    StableStatusChanged,
    StateMachineResult,
)


def apply_observation(
    state: MonitoringState,
    status: RoundStatus,
    observed_at: datetime,
    *,
    failure_threshold: int = 3,
    recovery_threshold: int = 2,
) -> StateMachineResult:
    """Apply one classified round to monitoring state.

    Replaying the same timestamp and status is a no-op. Timestamps must be
    strictly increasing otherwise, which lets persistence safely retry a round
    without duplicating incident events.
    """

    _validate_thresholds(failure_threshold, recovery_threshold)
    _validate_timestamp(observed_at)

    if state.last_observed_at is not None:
        if observed_at < state.last_observed_at:
            raise ValueError("observations must be processed in timestamp order")
        if observed_at == state.last_observed_at:
            if status is not state.last_observed_status:
                raise ValueError("one timestamp cannot have two round statuses")
            return StateMachineResult(state=state)

    if status is RoundStatus.MONITORING_UNKNOWN:
        return _apply_unknown(state, observed_at)

    if status is state.stable_status:
        return StateMachineResult(
            state=_with_last_observation(_clear_pending(state), status, observed_at)
        )

    pending_state = _advance_pending(state, status, observed_at)
    target = pending_state.pending_status
    if target is None:
        raise AssertionError("pending target must exist")

    threshold = (
        recovery_threshold
        if target is RoundStatus.ONLINE
        else failure_threshold
    )
    if pending_state.pending_count < threshold:
        return StateMachineResult(
            state=_with_last_observation(pending_state, status, observed_at)
        )

    if target is state.stable_status:
        return StateMachineResult(
            state=_with_last_observation(_clear_pending(state), status, observed_at)
        )

    return _confirm_transition(
        pending_state,
        target,
        status,
        observed_at,
    )


def interrupt_for_gap(
    state: MonitoringState,
    gap_started_at: datetime,
) -> StateMachineResult:
    """Interrupt confirmed state when monitoring continuity is lost."""

    _validate_timestamp(gap_started_at)
    if (
        state.last_observed_at is not None
        and gap_started_at < state.last_observed_at
    ):
        raise ValueError("gap cannot start before the last observation")

    if state.stable_status is RoundStatus.MONITORING_UNKNOWN:
        return StateMachineResult(state=_clear_pending(state))

    status_event = StableStatusChanged(
        previous=state.stable_status,
        current=RoundStatus.MONITORING_UNKNOWN,
        observed_at=gap_started_at,
        confirmed_at=gap_started_at,
    )
    events: Tuple[DomainEvent, ...]
    if state.open_incident is None:
        events = (status_event,)
    else:
        events = (
            IncidentEnded(
                incident=state.open_incident,
                observed_end=gap_started_at,
                confirmed_end=None,
                reason=IncidentEndReason.MONITORING_UNKNOWN,
            ),
            status_event,
        )

    new_state = MonitoringState(
        stable_status=RoundStatus.MONITORING_UNKNOWN,
        last_observed_at=state.last_observed_at,
        last_observed_status=state.last_observed_status,
    )
    return StateMachineResult(state=new_state, events=events)


def _apply_unknown(
    state: MonitoringState,
    observed_at: datetime,
) -> StateMachineResult:
    if state.stable_status is RoundStatus.MONITORING_UNKNOWN:
        return StateMachineResult(
            state=_with_last_observation(
                _clear_pending(state),
                RoundStatus.MONITORING_UNKNOWN,
                observed_at,
            )
        )

    events: Tuple[DomainEvent, ...]
    status_event = StableStatusChanged(
        previous=state.stable_status,
        current=RoundStatus.MONITORING_UNKNOWN,
        observed_at=observed_at,
        confirmed_at=observed_at,
    )
    if state.open_incident is None:
        events = (status_event,)
    else:
        events = (
            IncidentEnded(
                incident=state.open_incident,
                observed_end=observed_at,
                confirmed_end=None,
                reason=IncidentEndReason.MONITORING_UNKNOWN,
            ),
            status_event,
        )

    new_state = MonitoringState(
        stable_status=RoundStatus.MONITORING_UNKNOWN,
        last_observed_at=observed_at,
        last_observed_status=RoundStatus.MONITORING_UNKNOWN,
    )
    return StateMachineResult(state=new_state, events=events)


def _advance_pending(
    state: MonitoringState,
    status: RoundStatus,
    observed_at: datetime,
) -> MonitoringState:
    if state.pending_status is None:
        return replace(
            state,
            pending_status=status,
            pending_count=1,
            pending_started_at=observed_at,
        )

    pending = state.pending_status
    if pending is status:
        return replace(state, pending_count=state.pending_count + 1)

    pending_is_online = pending is RoundStatus.ONLINE
    status_is_online = status is RoundStatus.ONLINE
    if pending_is_online or status_is_online:
        return replace(
            state,
            pending_status=status,
            pending_count=1,
            pending_started_at=observed_at,
        )

    return replace(
        state,
        pending_status=RoundStatus.PARTIAL_CONNECTIVITY,
        pending_count=state.pending_count + 1,
    )


def _confirm_transition(
    state: MonitoringState,
    target: RoundStatus,
    observed_status: RoundStatus,
    confirmed_at: datetime,
) -> StateMachineResult:
    observed_at = state.pending_started_at
    if observed_at is None:
        raise AssertionError("confirmed transition requires an observed start")

    previous = state.stable_status
    events = []

    if state.open_incident is not None:
        reason = (
            IncidentEndReason.RECOVERED
            if target is RoundStatus.ONLINE
            else IncidentEndReason.CATEGORY_TRANSITION
        )
        events.append(
            IncidentEnded(
                incident=state.open_incident,
                observed_end=observed_at,
                confirmed_end=confirmed_at,
                reason=reason,
            )
        )

    events.append(
        StableStatusChanged(
            previous=previous,
            current=target,
            observed_at=observed_at,
            confirmed_at=confirmed_at,
        )
    )

    open_incident = None
    if target not in {RoundStatus.ONLINE, RoundStatus.MONITORING_UNKNOWN}:
        open_incident = OpenIncident(
            status=target,
            observed_start=observed_at,
            confirmed_start=confirmed_at,
        )
        events.append(IncidentOpened(incident=open_incident))

    new_state = MonitoringState(
        stable_status=target,
        open_incident=open_incident,
        last_observed_at=confirmed_at,
        last_observed_status=observed_status,
    )
    return StateMachineResult(state=new_state, events=tuple(events))


def _clear_pending(state: MonitoringState) -> MonitoringState:
    return replace(
        state,
        pending_status=None,
        pending_count=0,
        pending_started_at=None,
    )


def _with_last_observation(
    state: MonitoringState,
    status: RoundStatus,
    observed_at: datetime,
) -> MonitoringState:
    return replace(
        state,
        last_observed_at=observed_at,
        last_observed_status=status,
    )


def _validate_thresholds(
    failure_threshold: int,
    recovery_threshold: int,
) -> None:
    if failure_threshold < 1:
        raise ValueError("failure_threshold must be at least 1")
    if recovery_threshold < 1:
        raise ValueError("recovery_threshold must be at least 1")


def _validate_timestamp(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError("observed_at must use UTC")
