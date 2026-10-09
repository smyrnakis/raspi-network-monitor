"""Integration tests for SQLite migrations and monitoring persistence."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from home_internet_monitor.domain import (
    ComponentStatus as C,
    MonitoringState,
    ProbeOutcome,
    RoundEvidence,
    RoundStatus as S,
    apply_observation,
)
from home_internet_monitor.storage import (
    GapReason,
    MonitoringRepository,
    ProbeSampleRecord,
    ProbeTargetRecord,
    RoundRecord,
    connect_database,
    migrate,
)


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
SITE_ID = "test-site"
TARGET_ID = "external-primary"


def at(seconds: int) -> datetime:
    return START + timedelta(seconds=seconds)


def evidence_for(status: S) -> RoundEvidence:
    if status is S.ONLINE:
        return RoundEvidence(C.REACHABLE, C.REACHABLE, C.REACHABLE, C.REACHABLE)
    if status is S.INTERNET_DOWN:
        return RoundEvidence(C.REACHABLE, C.UNREACHABLE, C.UNKNOWN, C.UNREACHABLE)
    if status is S.DNS_FAILURE:
        return RoundEvidence(C.REACHABLE, C.REACHABLE, C.UNREACHABLE, C.UNKNOWN)
    if status is S.PARTIAL_CONNECTIVITY:
        return RoundEvidence(C.REACHABLE, C.UNREACHABLE, C.REACHABLE, C.REACHABLE)
    if status is S.GATEWAY_UNREACHABLE:
        return RoundEvidence(C.UNREACHABLE, C.UNREACHABLE, C.UNKNOWN, C.UNREACHABLE)
    return RoundEvidence(C.UNKNOWN, C.UNKNOWN, C.UNKNOWN, C.UNKNOWN)


class RepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "monitor.db"
        self.connection = connect_database(self.database_path)
        migrate(self.connection)
        self.repository = MonitoringRepository(self.connection)
        self.repository.ensure_site(SITE_ID, "Test site", "UTC", at(0))
        self.repository.upsert_target(
            SITE_ID,
            ProbeTargetRecord(
                target_id=TARGET_ID,
                kind="external_ip",
                label="Primary external target",
                endpoint="test.invalid",
            ),
            at(0),
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_directory.cleanup()

    def record(
        self,
        state: MonitoringState,
        status: S,
        seconds: int,
        *,
        metadata: Optional[dict[str, object]] = None,
        runtime: Optional[Tuple[str, str]] = None,
    ) -> tuple[MonitoringState, bool]:
        observed_at = at(seconds)
        result = apply_observation(state, status, observed_at)
        record = RoundRecord(
            round_id=f"round-{seconds}",
            site_id=SITE_ID,
            observed_at=observed_at,
            status=status,
            evidence=evidence_for(status),
            boot_id=runtime[0] if runtime else None,
            process_id=runtime[1] if runtime else None,
        )
        stored = self.repository.record_round(
            record,
            (
                ProbeSampleRecord(
                    target_id=TARGET_ID,
                    outcome=(
                        ProbeOutcome.SUCCESS
                        if status is S.ONLINE
                        else ProbeOutcome.FAILURE
                    ),
                    latency_ms=12.5 if status is S.ONLINE else None,
                    error_class=None if status is S.ONLINE else "timeout",
                    metadata=metadata or {},
                ),
            ),
            result,
        )
        return result.state, stored


class MigrationTests(RepositoryTestCase):
    def test_migration_is_idempotent_and_enables_connection_policy(self) -> None:
        migrate(self.connection)

        versions = self.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        self.assertEqual([row[0] for row in versions], [1, 2, 3, 4, 5])
        self.assertTrue(
            {
                "sites",
                "probe_targets",
                "probe_rounds",
                "probe_samples",
                "status_intervals",
                "incidents",
                "monitor_state",
                "monitor_runtime",
                "monitoring_gaps",
                "latency_hourly",
            }.issubset(tables)
        )
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_keys").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute("PRAGMA journal_mode").fetchone()[0],
            "wal",
        )


class PersistenceTests(RepositoryTestCase):
    def test_pending_state_survives_connection_restart(self) -> None:
        state, stored = self.record(MonitoringState(), S.ONLINE, 0)
        self.assertTrue(stored)
        self.assertEqual(state.pending_status, S.ONLINE)
        self.assertEqual(state.pending_count, 1)

        self.connection.close()
        self.connection = connect_database(self.database_path)
        self.repository = MonitoringRepository(self.connection)

        self.assertEqual(self.repository.load_state(SITE_ID), state)

    def test_incident_and_intervals_survive_restart_and_recovery(self) -> None:
        state = MonitoringState()
        for seconds, status in (
            (0, S.ONLINE),
            (10, S.ONLINE),
            (20, S.INTERNET_DOWN),
            (30, S.INTERNET_DOWN),
            (40, S.INTERNET_DOWN),
        ):
            state, stored = self.record(state, status, seconds)
            self.assertTrue(stored)

        self.assertEqual(state.stable_status, S.INTERNET_DOWN)
        self.assertIsNotNone(state.open_incident)

        self.connection.close()
        self.connection = connect_database(self.database_path)
        self.repository = MonitoringRepository(self.connection)
        state = self.repository.load_state(SITE_ID)
        self.assertEqual(state.stable_status, S.INTERNET_DOWN)
        self.assertIsNotNone(state.open_incident)

        state, _ = self.record(state, S.ONLINE, 50)
        state, _ = self.record(state, S.ONLINE, 60)
        self.assertEqual(state.stable_status, S.ONLINE)
        self.assertIsNone(state.open_incident)

        incident = self.connection.execute(
            "SELECT * FROM incidents WHERE site_id = ?",
            (SITE_ID,),
        ).fetchone()
        self.assertEqual(incident["lifecycle"], "closed")
        self.assertEqual(incident["status"], S.INTERNET_DOWN.value)
        self.assertEqual(incident["observed_start_ms"], 20_000 + int(START.timestamp() * 1000))
        self.assertEqual(incident["confirmed_start_ms"], 40_000 + int(START.timestamp() * 1000))
        self.assertEqual(incident["observed_end_ms"], 50_000 + int(START.timestamp() * 1000))
        self.assertEqual(incident["confirmed_end_ms"], 60_000 + int(START.timestamp() * 1000))
        self.assertEqual(incident["end_reason"], "recovered")
        self.assertEqual(
            json.loads(incident["failure_summary_json"]),
            [
                {
                    "error_class": "timeout",
                    "outcome": "failure",
                    "target_id": TARGET_ID,
                }
            ],
        )

        intervals = self.connection.execute(
            """
            SELECT status, start_ms, end_ms
            FROM status_intervals
            WHERE site_id = ? AND end_ms > start_ms
            ORDER BY start_ms
            """,
            (SITE_ID,),
        ).fetchall()
        self.assertEqual(
            [row["status"] for row in intervals],
            [S.ONLINE.value, S.INTERNET_DOWN.value],
        )
        self.assertEqual(intervals[0]["end_ms"], intervals[1]["start_ms"])

        open_interval = self.connection.execute(
            """
            SELECT status, start_ms FROM status_intervals
            WHERE site_id = ? AND end_ms IS NULL
            """,
            (SITE_ID,),
        ).fetchone()
        self.assertEqual(open_interval["status"], S.ONLINE.value)
        self.assertEqual(
            open_interval["start_ms"],
            50_000 + int(START.timestamp() * 1000),
        )

    def test_duplicate_round_does_not_replay_samples_or_state(self) -> None:
        state = MonitoringState()
        observed_at = at(0)
        result = apply_observation(state, S.ONLINE, observed_at)
        record = RoundRecord(
            round_id="same-round",
            site_id=SITE_ID,
            observed_at=observed_at,
            status=S.ONLINE,
            evidence=evidence_for(S.ONLINE),
        )
        samples = (
            ProbeSampleRecord(
                target_id=TARGET_ID,
                outcome=ProbeOutcome.SUCCESS,
                latency_ms=5.0,
            ),
        )

        self.assertTrue(self.repository.record_round(record, samples, result))
        self.assertFalse(self.repository.record_round(record, samples, result))

        round_count = self.connection.execute(
            "SELECT COUNT(*) FROM probe_rounds"
        ).fetchone()[0]
        sample_count = self.connection.execute(
            "SELECT COUNT(*) FROM probe_samples"
        ).fetchone()[0]
        self.assertEqual(round_count, 1)
        self.assertEqual(sample_count, 1)

    def test_duplicate_round_id_with_different_data_is_rejected(self) -> None:
        initial = MonitoringState()
        first_result = apply_observation(initial, S.ONLINE, at(0))
        first_record = RoundRecord(
            round_id="reused-round-id",
            site_id=SITE_ID,
            observed_at=at(0),
            status=S.ONLINE,
            evidence=evidence_for(S.ONLINE),
        )
        self.assertTrue(self.repository.record_round(first_record, (), first_result))

        second_result = apply_observation(first_result.state, S.ONLINE, at(10))
        second_record = RoundRecord(
            round_id="reused-round-id",
            site_id=SITE_ID,
            observed_at=at(10),
            status=S.ONLINE,
            evidence=evidence_for(S.ONLINE),
        )
        with self.assertRaisesRegex(ValueError, "different data"):
            self.repository.record_round(second_record, (), second_result)

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM probe_rounds").fetchone()[0],
            1,
        )

    def test_failed_sample_validation_rolls_back_whole_round(self) -> None:
        state = MonitoringState()
        with self.assertRaisesRegex(ValueError, "metadata is too large"):
            self.record(
                state,
                S.ONLINE,
                0,
                metadata={"detail": "x" * 5000},
            )

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM probe_rounds").fetchone()[0],
            0,
        )
        self.assertEqual(self.repository.load_state(SITE_ID), MonitoringState())


class RetentionTests(RepositoryTestCase):
    def test_cleanup_expires_raw_rounds_and_completed_incidents_only(self) -> None:
        state = MonitoringState()
        state, _ = self.record(state, S.ONLINE, 0)
        state, _ = self.record(state, S.ONLINE, 80 * 86_400)

        def milliseconds(seconds: int) -> int:
            return int(at(seconds).timestamp() * 1000)

        day1 = milliseconds(86_400)
        day2 = milliseconds(2 * 86_400)
        day80 = milliseconds(80 * 86_400)
        day81 = milliseconds(81 * 86_400)

        self.connection.execute(
            """
            INSERT INTO incidents(
                incident_id, site_id, status, lifecycle,
                observed_start_ms, confirmed_start_ms,
                observed_end_ms, confirmed_end_ms, end_reason,
                previous_incident_id, notes, created_at_ms, updated_at_ms
            ) VALUES
                ('old', ?, 'internet_down', 'closed', ?, ?, ?, ?,
                 'recovered', NULL, '', ?, ?),
                ('recent', ?, 'internet_down', 'closed', ?, ?, ?, ?,
                 'recovered', 'old', '', ?, ?),
                ('open', ?, 'internet_down', 'open', ?, ?, NULL, NULL,
                 NULL, NULL, '', ?, ?),
                ('annotated', ?, 'dns_failure', 'closed', ?, ?, ?, ?,
                 'recovered', NULL, 'keep this note', ?, ?)
            """,
            (
                SITE_ID,
                day1,
                day1,
                day2,
                day2,
                day1,
                day2,
                SITE_ID,
                day80,
                day80,
                day81,
                day81,
                day80,
                day81,
                SITE_ID,
                day1,
                day1,
                day1,
                day1,
                SITE_ID,
                day1,
                day1,
                day2,
                day2,
                day1,
                day2,
            ),
        )
        self.connection.commit()

        result = self.repository.apply_retention(
            SITE_ID,
            at(100 * 86_400),
            raw_samples_days=30,
            incidents_days=30,
            latency_aggregates_days=548,
        )

        self.assertEqual(result.deleted_rounds, 1)
        self.assertEqual(result.deleted_incidents, 1)
        self.assertEqual(result.aggregated_hours, 1)
        aggregate = self.connection.execute(
            """
            SELECT success_count, failure_count, latency_count,
                   latency_sum_ms, latency_min_ms, latency_max_ms
            FROM latency_hourly
            """
        ).fetchone()
        self.assertEqual((1, 0, 1), tuple(aggregate[:3]))
        self.assertEqual((12.5, 12.5, 12.5), tuple(aggregate[3:]))
        self.assertEqual(
            [row[0] for row in self.connection.execute(
                "SELECT round_id FROM probe_rounds ORDER BY observed_at_ms"
            )],
            [f"round-{80 * 86_400}"],
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM probe_samples").fetchone()[0],
            1,
        )
        incidents = self.connection.execute(
            """
            SELECT incident_id, lifecycle, previous_incident_id
            FROM incidents ORDER BY incident_id
            """
        ).fetchall()
        self.assertEqual(
            [(row[0], row[1], row[2]) for row in incidents],
            [
                ("annotated", "closed", None),
                ("open", "open", None),
                ("recent", "closed", None),
            ],
        )
        self.assertGreater(
            self.connection.execute("SELECT COUNT(*) FROM status_intervals").fetchone()[0],
            0,
        )

    def test_cleanup_rejects_non_positive_periods_without_deleting(self) -> None:
        state, _ = self.record(MonitoringState(), S.ONLINE, 0)
        self.assertIsNotNone(state)

        with self.assertRaisesRegex(ValueError, "retention periods must be positive"):
            self.repository.apply_retention(
                SITE_ID,
                at(100),
                raw_samples_days=0,
                incidents_days=30,
                latency_aggregates_days=548,
            )

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM probe_rounds").fetchone()[0],
            1,
        )

    def test_indefinite_incident_retention_keeps_completed_incidents(self) -> None:
        state = MonitoringState()
        state, _ = self.record(state, S.INTERNET_DOWN, 0)
        state, _ = self.record(state, S.INTERNET_DOWN, 10)
        state, _ = self.record(state, S.INTERNET_DOWN, 20)
        state, _ = self.record(state, S.ONLINE, 30)
        state, _ = self.record(state, S.ONLINE, 40)

        result = self.repository.apply_retention(
            SITE_ID,
            at(100 * 86_400),
            raw_samples_days=7,
            incidents_days=None,
            latency_aggregates_days=548,
        )

        self.assertEqual(0, result.deleted_incidents)
        self.assertEqual(
            1,
            self.connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0],
        )

    def test_latency_aggregate_excludes_failure_wait_time(self) -> None:
        self.record(MonitoringState(), S.INTERNET_DOWN, 0)
        self.connection.execute(
            "UPDATE probe_samples SET latency_ms = 5000 WHERE round_id = 'round-0'"
        )
        self.connection.commit()

        self.repository.apply_retention(
            SITE_ID,
            at(10 * 86_400),
            raw_samples_days=7,
            incidents_days=None,
            latency_aggregates_days=548,
        )

        row = self.connection.execute(
            """
            SELECT failure_count, latency_count, latency_sum_ms,
                   latency_min_ms, latency_max_ms
            FROM latency_hourly
            """
        ).fetchone()
        self.assertEqual((1, 0, 0.0, None, None), tuple(row))


class RuntimeGapTests(RepositoryTestCase):
    def test_process_restart_opens_and_first_round_closes_gap(self) -> None:
        self.assertIsNone(
            self.repository.start_runtime(
                SITE_ID,
                "boot-a",
                "process-a",
                at(0),
            )
        )
        state = MonitoringState()
        state, _ = self.record(
            state,
            S.ONLINE,
            0,
            runtime=("boot-a", "process-a"),
        )
        state, _ = self.record(
            state,
            S.ONLINE,
            10,
            runtime=("boot-a", "process-a"),
        )

        gap = self.repository.start_runtime(
            SITE_ID,
            "boot-a",
            "process-b",
            at(20),
        )
        self.assertIsNotNone(gap)
        self.assertEqual(gap.reason, GapReason.PROCESS_RESTART)
        self.assertEqual(gap.started_at, at(10))

        state = self.repository.load_state(SITE_ID)
        self.assertEqual(state.stable_status, S.MONITORING_UNKNOWN)
        state, _ = self.record(
            state,
            S.ONLINE,
            20,
            runtime=("boot-a", "process-b"),
        )

        stored_gap = self.connection.execute(
            "SELECT * FROM monitoring_gaps WHERE gap_id = ?",
            (gap.gap_id,),
        ).fetchone()
        self.assertEqual(
            stored_gap["end_ms"],
            int(START.timestamp() * 1000) + 20_000,
        )
        self.assertEqual(state.stable_status, S.MONITORING_UNKNOWN)
        self.assertEqual(state.pending_status, S.ONLINE)

    def test_boot_change_is_recorded_as_host_reboot(self) -> None:
        self.repository.start_runtime(
            SITE_ID,
            "boot-a",
            "process-a",
            at(0),
        )
        gap = self.repository.start_runtime(
            SITE_ID,
            "boot-b",
            "process-b",
            at(10),
        )

        self.assertIsNotNone(gap)
        self.assertEqual(gap.reason, GapReason.HOST_REBOOT)
        self.assertEqual(gap.previous_boot_id, "boot-a")
        self.assertEqual(gap.current_boot_id, "boot-b")

    def test_clock_gap_interrupts_state_and_closes_on_trusted_round(self) -> None:
        self.repository.start_runtime(SITE_ID, "boot-a", "process-a", at(0))
        state = MonitoringState()
        state, _ = self.record(
            state,
            S.ONLINE,
            0,
            runtime=("boot-a", "process-a"),
        )
        state, _ = self.record(
            state,
            S.ONLINE,
            10,
            runtime=("boot-a", "process-a"),
        )

        gap = self.repository.open_clock_gap(
            SITE_ID,
            "boot-a",
            "process-a",
            at(20),
        )
        self.assertEqual(GapReason.CLOCK_UNCERTAIN, gap.reason)
        self.assertEqual(S.MONITORING_UNKNOWN, self.repository.load_state(SITE_ID).stable_status)

        state = self.repository.load_state(SITE_ID)
        state, _ = self.record(
            state,
            S.ONLINE,
            30,
            runtime=("boot-a", "process-a"),
        )
        stored = self.connection.execute(
            "SELECT end_ms FROM monitoring_gaps WHERE gap_id = ?",
            (gap.gap_id,),
        ).fetchone()
        self.assertEqual(milliseconds := int(at(30).timestamp() * 1000), stored[0])
        self.assertGreater(milliseconds, int(gap.started_at.timestamp() * 1000))

    def test_backward_clock_on_restart_opens_gap_at_last_observation(self) -> None:
        self.repository.start_runtime(SITE_ID, "boot-a", "process-a", at(0))
        state = MonitoringState()
        state, _ = self.record(
            state,
            S.ONLINE,
            10,
            runtime=("boot-a", "process-a"),
        )

        gap = self.repository.start_runtime(
            SITE_ID,
            "boot-a",
            "process-b",
            at(5),
        )

        self.assertIsNotNone(gap)
        self.assertEqual(GapReason.CLOCK_UNCERTAIN, gap.reason)
        self.assertEqual(at(10), gap.started_at)

    def test_restart_interrupts_open_incident_at_last_round(self) -> None:
        self.repository.start_runtime(
            SITE_ID,
            "boot-a",
            "process-a",
            at(0),
        )
        state = MonitoringState()
        for seconds, status in (
            (0, S.ONLINE),
            (10, S.ONLINE),
            (20, S.INTERNET_DOWN),
            (30, S.INTERNET_DOWN),
            (40, S.INTERNET_DOWN),
        ):
            state, _ = self.record(
                state,
                status,
                seconds,
                runtime=("boot-a", "process-a"),
            )

        gap = self.repository.start_runtime(
            SITE_ID,
            "boot-a",
            "process-b",
            at(50),
        )
        self.assertEqual(gap.started_at, at(40))

        incident = self.connection.execute(
            "SELECT * FROM incidents WHERE site_id = ?",
            (SITE_ID,),
        ).fetchone()
        self.assertEqual(incident["lifecycle"], "interrupted")
        self.assertEqual(incident["end_reason"], "monitoring_unknown")
        self.assertEqual(
            incident["observed_end_ms"],
            int(START.timestamp() * 1000) + 40_000,
        )
        self.assertIsNone(incident["confirmed_end_ms"])

    def test_heartbeat_must_match_active_runtime(self) -> None:
        self.repository.start_runtime(
            SITE_ID,
            "boot-a",
            "process-a",
            at(0),
        )
        self.repository.record_heartbeat(
            SITE_ID,
            "boot-a",
            "process-a",
            at(5),
        )

        with self.assertRaisesRegex(RuntimeError, "active runtime"):
            self.repository.record_heartbeat(
                SITE_ID,
                "boot-a",
                "wrong-process",
                at(6),
            )

    def test_stale_heartbeat_opens_gap_without_process_change(self) -> None:
        self.repository.start_runtime(
            SITE_ID,
            "boot-a",
            "process-a",
            at(0),
        )
        state = MonitoringState()
        state, _ = self.record(
            state,
            S.ONLINE,
            0,
            runtime=("boot-a", "process-a"),
        )
        state, _ = self.record(
            state,
            S.ONLINE,
            10,
            runtime=("boot-a", "process-a"),
        )

        self.assertIsNone(
            self.repository.open_stale_gap(
                SITE_ID,
                "boot-a",
                "process-a",
                at(40),
                stale_after_seconds=30,
            )
        )
        gap = self.repository.open_stale_gap(
            SITE_ID,
            "boot-a",
            "process-a",
            at(41),
            stale_after_seconds=30,
        )

        self.assertIsNotNone(gap)
        self.assertEqual(gap.reason, GapReason.STALE_HEARTBEAT)
        self.assertEqual(gap.started_at, at(10))
        self.assertEqual(
            self.repository.load_state(SITE_ID).stable_status,
            S.MONITORING_UNKNOWN,
        )

    def test_stale_threshold_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            self.repository.open_stale_gap(
                SITE_ID,
                "boot-a",
                "process-a",
                at(0),
                stale_after_seconds=0,
            )

    def test_missing_target_rolls_back_whole_round(self) -> None:
        result = apply_observation(MonitoringState(), S.ONLINE, at(0))
        record = RoundRecord(
            round_id="missing-target-round",
            site_id=SITE_ID,
            observed_at=at(0),
            status=S.ONLINE,
            evidence=evidence_for(S.ONLINE),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.record_round(
                record,
                (
                    ProbeSampleRecord(
                        target_id="missing",
                        outcome=ProbeOutcome.SUCCESS,
                    ),
                ),
                result,
            )

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM probe_rounds").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
