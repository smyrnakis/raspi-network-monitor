"""Sequence tests for confirmation, recovery, and incident transitions."""

import unittest
from datetime import datetime, timedelta, timezone

from home_internet_monitor.domain import (
    IncidentEnded,
    IncidentEndReason,
    IncidentOpened,
    MonitoringState,
    RoundStatus as S,
    StableStatusChanged,
    apply_observation,
    interrupt_for_gap,
)


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def at(seconds: int) -> datetime:
    return START + timedelta(seconds=seconds)


def apply_sequence(
    statuses: list[S],
    *,
    initial: MonitoringState = MonitoringState(),
    start_seconds: int = 0,
) -> tuple[MonitoringState, list[object]]:
    state = initial
    events: list[object] = []
    for index, status in enumerate(statuses):
        result = apply_observation(
            state,
            status,
            at(start_seconds + index * 10),
        )
        state = result.state
        events.extend(result.events)
    return state, events


class StateMachineTests(unittest.TestCase):
    def online_state(self) -> MonitoringState:
        state, events = apply_sequence([S.ONLINE, S.ONLINE])
        self.assertEqual(state.stable_status, S.ONLINE)
        self.assertEqual(len(events), 1)
        return state

    def confirmed_incident_state(self) -> MonitoringState:
        state = self.online_state()
        state, events = apply_sequence(
            [S.INTERNET_DOWN, S.INTERNET_DOWN, S.INTERNET_DOWN],
            initial=state,
            start_seconds=20,
        )
        self.assertIsInstance(events[-1], IncidentOpened)
        return state

    def test_failure_opens_once_after_three_rounds(self) -> None:
        state = self.online_state()

        first = apply_observation(state, S.INTERNET_DOWN, at(20))
        second = apply_observation(first.state, S.INTERNET_DOWN, at(30))
        third = apply_observation(second.state, S.INTERNET_DOWN, at(40))

        self.assertEqual(first.events, ())
        self.assertEqual(second.events, ())
        self.assertEqual(third.state.stable_status, S.INTERNET_DOWN)
        self.assertEqual(len(third.events), 2)

        changed = third.events[0]
        opened = third.events[1]
        self.assertIsInstance(changed, StableStatusChanged)
        self.assertIsInstance(opened, IncidentOpened)
        self.assertEqual(changed.observed_at, at(20))
        self.assertEqual(changed.confirmed_at, at(40))
        self.assertEqual(opened.incident.observed_start, at(20))
        self.assertEqual(opened.incident.confirmed_start, at(40))

        continuing = apply_observation(third.state, S.INTERNET_DOWN, at(50))
        self.assertEqual(continuing.events, ())
        self.assertEqual(continuing.state.open_incident, opened.incident)

    def test_mixed_failure_run_confirms_partial(self) -> None:
        state = self.online_state()
        state, events = apply_sequence(
            [S.INTERNET_DOWN, S.DNS_FAILURE, S.INTERNET_DOWN],
            initial=state,
            start_seconds=20,
        )

        self.assertEqual(state.stable_status, S.PARTIAL_CONNECTIVITY)
        self.assertEqual(state.open_incident.status, S.PARTIAL_CONNECTIVITY)
        self.assertEqual(state.open_incident.observed_start, at(20))
        self.assertEqual(len(events), 2)

    def test_two_online_rounds_close_at_first_recovery(self) -> None:
        state = self.confirmed_incident_state()

        first = apply_observation(state, S.ONLINE, at(50))
        second = apply_observation(first.state, S.ONLINE, at(60))

        self.assertEqual(first.events, ())
        self.assertEqual(second.state.stable_status, S.ONLINE)
        self.assertIsNone(second.state.open_incident)
        self.assertEqual(len(second.events), 2)

        ended = second.events[0]
        changed = second.events[1]
        self.assertIsInstance(ended, IncidentEnded)
        self.assertEqual(ended.observed_end, at(50))
        self.assertEqual(ended.confirmed_end, at(60))
        self.assertEqual(ended.reason, IncidentEndReason.RECOVERED)
        self.assertEqual(changed.observed_at, at(50))

    def test_failed_recovery_keeps_same_incident(self) -> None:
        state = self.confirmed_incident_state()
        incident = state.open_incident

        possible_recovery = apply_observation(state, S.ONLINE, at(50))
        failed_recovery = apply_observation(
            possible_recovery.state,
            S.INTERNET_DOWN,
            at(60),
        )

        self.assertEqual(possible_recovery.events, ())
        self.assertEqual(failed_recovery.events, ())
        self.assertEqual(failed_recovery.state.stable_status, S.INTERNET_DOWN)
        self.assertEqual(failed_recovery.state.open_incident, incident)
        self.assertEqual(failed_recovery.state.pending_count, 0)

    def test_category_transition_closes_and_opens_at_first_observation(self) -> None:
        state = self.confirmed_incident_state()
        old_incident = state.open_incident

        state, events = apply_sequence(
            [S.DNS_FAILURE, S.DNS_FAILURE, S.DNS_FAILURE],
            initial=state,
            start_seconds=50,
        )

        self.assertEqual(state.stable_status, S.DNS_FAILURE)
        self.assertEqual(len(events), 3)
        ended, changed, opened = events
        self.assertIsInstance(ended, IncidentEnded)
        self.assertEqual(ended.incident, old_incident)
        self.assertEqual(ended.observed_end, at(50))
        self.assertEqual(ended.confirmed_end, at(70))
        self.assertEqual(ended.reason, IncidentEndReason.CATEGORY_TRANSITION)
        self.assertIsInstance(changed, StableStatusChanged)
        self.assertIsInstance(opened, IncidentOpened)
        self.assertEqual(opened.incident.observed_start, at(50))
        self.assertEqual(opened.incident.confirmed_start, at(70))

    def test_unknown_interrupts_open_incident_immediately(self) -> None:
        state = self.confirmed_incident_state()
        result = apply_observation(state, S.MONITORING_UNKNOWN, at(50))

        self.assertEqual(result.state.stable_status, S.MONITORING_UNKNOWN)
        self.assertIsNone(result.state.open_incident)
        self.assertEqual(len(result.events), 2)
        ended, changed = result.events
        self.assertIsInstance(ended, IncidentEnded)
        self.assertEqual(ended.observed_end, at(50))
        self.assertIsNone(ended.confirmed_end)
        self.assertEqual(ended.reason, IncidentEndReason.MONITORING_UNKNOWN)
        self.assertIsInstance(changed, StableStatusChanged)

    def test_gap_interrupts_at_last_observation_boundary(self) -> None:
        state = self.confirmed_incident_state()
        result = interrupt_for_gap(state, at(40))

        self.assertEqual(result.state.stable_status, S.MONITORING_UNKNOWN)
        self.assertEqual(result.state.last_observed_at, at(40))
        self.assertEqual(result.state.last_observed_status, S.INTERNET_DOWN)
        self.assertIsNone(result.state.open_incident)
        self.assertEqual(len(result.events), 2)
        ended, changed = result.events
        self.assertIsInstance(ended, IncidentEnded)
        self.assertEqual(ended.observed_end, at(40))
        self.assertIsNone(ended.confirmed_end)
        self.assertEqual(ended.reason, IncidentEndReason.MONITORING_UNKNOWN)
        self.assertIsInstance(changed, StableStatusChanged)
        self.assertEqual(changed.observed_at, at(40))

    def test_gap_discards_unconfirmed_candidate(self) -> None:
        first = apply_observation(MonitoringState(), S.ONLINE, at(0))
        result = interrupt_for_gap(first.state, at(0))

        self.assertEqual(result.state.stable_status, S.MONITORING_UNKNOWN)
        self.assertIsNone(result.state.pending_status)
        self.assertEqual(result.state.pending_count, 0)
        self.assertEqual(result.events, ())

    def test_after_unknown_classification_starts_fresh(self) -> None:
        state = self.confirmed_incident_state()
        state = apply_observation(
            state,
            S.MONITORING_UNKNOWN,
            at(50),
        ).state

        first = apply_observation(state, S.DNS_FAILURE, at(60))
        second = apply_observation(first.state, S.DNS_FAILURE, at(70))
        third = apply_observation(second.state, S.DNS_FAILURE, at(80))

        self.assertEqual(first.state.stable_status, S.MONITORING_UNKNOWN)
        self.assertEqual(second.state.stable_status, S.MONITORING_UNKNOWN)
        self.assertEqual(third.state.stable_status, S.DNS_FAILURE)
        self.assertIsNotNone(third.state.open_incident)

    def test_duplicate_observation_is_idempotent(self) -> None:
        first = apply_observation(MonitoringState(), S.ONLINE, at(0))
        duplicate = apply_observation(first.state, S.ONLINE, at(0))

        self.assertEqual(duplicate.state, first.state)
        self.assertEqual(duplicate.events, ())

    def test_same_timestamp_with_different_status_is_rejected(self) -> None:
        first = apply_observation(MonitoringState(), S.ONLINE, at(0))

        with self.assertRaisesRegex(ValueError, "two round statuses"):
            apply_observation(first.state, S.DNS_FAILURE, at(0))

    def test_out_of_order_observation_is_rejected(self) -> None:
        first = apply_observation(MonitoringState(), S.ONLINE, at(10))

        with self.assertRaisesRegex(ValueError, "timestamp order"):
            apply_observation(first.state, S.ONLINE, at(0))

    def test_timestamp_must_be_utc(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware UTC"):
            apply_observation(MonitoringState(), S.ONLINE, datetime(2026, 1, 1))

        non_utc = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))
        with self.assertRaisesRegex(ValueError, "must use UTC"):
            apply_observation(MonitoringState(), S.ONLINE, non_utc)

    def test_thresholds_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "failure_threshold"):
            apply_observation(
                MonitoringState(),
                S.ONLINE,
                at(0),
                failure_threshold=0,
            )

        with self.assertRaisesRegex(ValueError, "recovery_threshold"):
            apply_observation(
                MonitoringState(),
                S.ONLINE,
                at(0),
                recovery_threshold=0,
            )


if __name__ == "__main__":
    unittest.main()
