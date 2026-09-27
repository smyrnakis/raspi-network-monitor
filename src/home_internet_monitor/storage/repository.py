"""Transactional repository for monitoring history and recoverable state."""

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from home_internet_monitor.domain import (
    ComponentStatus,
    IncidentEnded,
    IncidentEndReason,
    IncidentOpened,
    MonitoringState,
    OpenIncident,
    ProbeOutcome,
    RoundEvidence,
    RoundStatus,
    StableStatusChanged,
    StateMachineResult,
    interrupt_for_gap,
)


_ID_NAMESPACE = uuid.UUID("6e1796fa-a5ca-4e10-9752-2918d67ab482")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_METADATA_JSON_LENGTH = 4096


@dataclass(frozen=True)
class ProbeTargetRecord:
    target_id: str
    kind: str
    label: str
    endpoint: str
    enabled: bool = True
    timeout_ms: int = 3000


@dataclass(frozen=True)
class ProbeSampleRecord:
    target_id: str
    outcome: ProbeOutcome
    latency_ms: Optional[float] = None
    error_class: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoundRecord:
    round_id: str
    site_id: str
    observed_at: datetime
    status: RoundStatus
    evidence: RoundEvidence
    boot_id: Optional[str] = None
    process_id: Optional[str] = None


class GapReason(str, Enum):
    HOST_REBOOT = "host_reboot"
    PROCESS_RESTART = "process_restart"
    STALE_HEARTBEAT = "stale_heartbeat"
    CLOCK_UNCERTAIN = "clock_uncertain"


@dataclass(frozen=True)
class MonitoringGapRecord:
    gap_id: str
    site_id: str
    started_at: datetime
    reason: GapReason
    previous_boot_id: Optional[str]
    current_boot_id: str
    previous_process_id: Optional[str]
    current_process_id: str


@dataclass(frozen=True)
class RetentionResult:
    deleted_rounds: int
    deleted_incidents: int
    aggregated_hours: int
    deleted_latency_aggregates: int


class MonitoringRepository:
    """Persist complete rounds and state-machine events atomically."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def ensure_site(
        self,
        site_id: str,
        display_name: str,
        timezone_name: str,
        now: datetime,
    ) -> None:
        timestamp = _to_epoch_ms(now)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO sites(
                    site_id, display_name, timezone, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(site_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    timezone = excluded.timezone,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (site_id, display_name, timezone_name, timestamp, timestamp),
            )

    def upsert_target(
        self,
        site_id: str,
        target: ProbeTargetRecord,
        now: datetime,
    ) -> None:
        if target.timeout_ms < 1:
            raise ValueError("target timeout_ms must be positive")
        timestamp = _to_epoch_ms(now)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO probe_targets(
                    target_id, site_id, kind, label, endpoint, enabled,
                    timeout_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    kind = excluded.kind,
                    label = excluded.label,
                    endpoint = excluded.endpoint,
                    enabled = excluded.enabled,
                    timeout_ms = excluded.timeout_ms,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    target.target_id,
                    site_id,
                    target.kind,
                    target.label,
                    target.endpoint,
                    int(target.enabled),
                    target.timeout_ms,
                    timestamp,
                    timestamp,
                ),
            )

    def apply_retention(
        self,
        site_id: str,
        now: datetime,
        *,
        raw_samples_days: int,
        incidents_days: Optional[int],
        latency_aggregates_days: int,
    ) -> RetentionResult:
        """Remove expired raw rounds and completed incidents atomically.

        Status intervals and monitoring gaps are compact history and remain
        available for long-range availability reporting. Open incidents and
        current monitor state are never removed.
        """

        if raw_samples_days < 1 or latency_aggregates_days < 1:
            raise ValueError("retention periods must be positive")
        if incidents_days is not None and incidents_days < 1:
            raise ValueError("incident retention must be positive or None")
        if latency_aggregates_days <= raw_samples_days:
            raise ValueError("latency aggregate retention must exceed raw retention")
        raw_cutoff_ms = _to_epoch_ms(now - timedelta(days=raw_samples_days))
        aggregate_cutoff_ms = _to_epoch_ms(
            now - timedelta(days=latency_aggregates_days)
        )
        incident_cutoff_ms = (
            _to_epoch_ms(now - timedelta(days=incidents_days))
            if incidents_days is not None
            else None
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            aggregated_hours = self._connection.execute(
                """
                INSERT INTO latency_hourly(
                    site_id, target_id, hour_start_ms, success_count,
                    failure_count, latency_count, latency_sum_ms,
                    latency_min_ms, latency_max_ms
                )
                SELECT r.site_id, s.target_id,
                       (r.observed_at_ms / 3600000) * 3600000,
                       SUM(CASE WHEN s.outcome = 'success' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN s.outcome = 'failure' THEN 1 ELSE 0 END),
                       COUNT(CASE WHEN s.outcome = 'success' THEN s.latency_ms END),
                       COALESCE(SUM(CASE WHEN s.outcome = 'success'
                                        THEN s.latency_ms END), 0),
                       MIN(CASE WHEN s.outcome = 'success' THEN s.latency_ms END),
                       MAX(CASE WHEN s.outcome = 'success' THEN s.latency_ms END)
                FROM probe_rounds r
                JOIN probe_samples s ON s.round_id = r.round_id
                JOIN probe_targets t ON t.target_id = s.target_id
                WHERE r.site_id = ? AND r.observed_at_ms < ?
                  AND t.kind IN ('gateway', 'external_ip')
                GROUP BY r.site_id, s.target_id,
                         (r.observed_at_ms / 3600000) * 3600000
                ON CONFLICT(site_id, target_id, hour_start_ms) DO UPDATE SET
                    success_count = excluded.success_count,
                    failure_count = excluded.failure_count,
                    latency_count = excluded.latency_count,
                    latency_sum_ms = excluded.latency_sum_ms,
                    latency_min_ms = excluded.latency_min_ms,
                    latency_max_ms = excluded.latency_max_ms
                """,
                (site_id, raw_cutoff_ms),
            ).rowcount
            deleted_rounds = self._connection.execute(
                """
                DELETE FROM probe_rounds
                WHERE site_id = ? AND observed_at_ms < ?
                """,
                (site_id, raw_cutoff_ms),
            ).rowcount
            deleted_incidents = 0
            if incident_cutoff_ms is not None:
                self._connection.execute(
                    """
                    UPDATE incidents SET previous_incident_id = NULL
                    WHERE site_id = ? AND previous_incident_id IN (
                        SELECT incident_id FROM incidents
                        WHERE site_id = ?
                          AND lifecycle != 'open'
                          AND observed_end_ms IS NOT NULL
                          AND observed_end_ms < ?
                          AND notes = ''
                    )
                    """,
                    (site_id, site_id, incident_cutoff_ms),
                )
                deleted_incidents = self._connection.execute(
                    """
                    DELETE FROM incidents
                    WHERE site_id = ?
                      AND lifecycle != 'open'
                      AND observed_end_ms IS NOT NULL
                      AND observed_end_ms < ?
                      AND notes = ''
                    """,
                    (site_id, incident_cutoff_ms),
                ).rowcount
            deleted_aggregates = self._connection.execute(
                """
                DELETE FROM latency_hourly
                WHERE site_id = ? AND hour_start_ms < ?
                """,
                (site_id, aggregate_cutoff_ms),
            ).rowcount
            self._connection.commit()
            return RetentionResult(
                deleted_rounds,
                deleted_incidents,
                aggregated_hours,
                deleted_aggregates,
            )
        except Exception:
            self._connection.rollback()
            raise

    def load_state(self, site_id: str) -> MonitoringState:
        row = self._connection.execute(
            "SELECT * FROM monitor_state WHERE site_id = ?",
            (site_id,),
        ).fetchone()
        if row is None:
            return MonitoringState()

        open_incident = None
        if row["open_incident_id"] is not None:
            incident_row = self._connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?",
                (row["open_incident_id"],),
            ).fetchone()
            if incident_row is None or incident_row["lifecycle"] != "open":
                raise RuntimeError("persisted monitor state references no open incident")
            open_incident = OpenIncident(
                status=RoundStatus(incident_row["status"]),
                observed_start=_from_epoch_ms(incident_row["observed_start_ms"]),
                confirmed_start=_from_epoch_ms(incident_row["confirmed_start_ms"]),
            )

        return MonitoringState(
            stable_status=RoundStatus(row["stable_status"]),
            pending_status=(
                RoundStatus(row["pending_status"])
                if row["pending_status"] is not None
                else None
            ),
            pending_count=row["pending_count"],
            pending_started_at=_optional_from_epoch_ms(
                row["pending_started_at_ms"]
            ),
            open_incident=open_incident,
            last_observed_at=_optional_from_epoch_ms(row["last_observed_at_ms"]),
            last_observed_status=(
                RoundStatus(row["last_observed_status"])
                if row["last_observed_status"] is not None
                else None
            ),
        )

    def start_runtime(
        self,
        site_id: str,
        boot_id: str,
        process_id: str,
        started_at: datetime,
    ) -> Optional[MonitoringGapRecord]:
        """Register a monitor process and open a gap after a prior runtime."""

        started_at_ms = _to_epoch_ms(started_at)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            previous = self._connection.execute(
                "SELECT * FROM monitor_runtime WHERE site_id = ?",
                (site_id,),
            ).fetchone()
            open_gap_row = self._connection.execute(
                """
                SELECT * FROM monitoring_gaps
                WHERE site_id = ? AND end_ms IS NULL
                """,
                (site_id,),
            ).fetchone()

            gap = None
            runtime_changed = previous is not None and (
                previous["boot_id"] != boot_id
                or previous["process_id"] != process_id
            )
            if open_gap_row is not None:
                gap = _gap_from_row(open_gap_row)
            elif runtime_changed:
                state = self.load_state(site_id)
                prior_boundary_ms = _optional_to_epoch_ms(
                    state.last_observed_at
                )
                if prior_boundary_ms is None:
                    prior_boundary_ms = previous["last_round_at_ms"]
                if prior_boundary_ms is None:
                    prior_boundary_ms = previous["last_heartbeat_ms"]
                reason = (
                    GapReason.HOST_REBOOT
                    if previous["boot_id"] != boot_id
                    else GapReason.PROCESS_RESTART
                )
                if prior_boundary_ms > started_at_ms:
                    reason = GapReason.CLOCK_UNCERTAIN

                gap = MonitoringGapRecord(
                    gap_id=_gap_id(site_id, prior_boundary_ms),
                    site_id=site_id,
                    started_at=_from_epoch_ms(prior_boundary_ms),
                    reason=reason,
                    previous_boot_id=previous["boot_id"],
                    current_boot_id=boot_id,
                    previous_process_id=previous["process_id"],
                    current_process_id=process_id,
                )
                self._connection.execute(
                    """
                    INSERT INTO monitoring_gaps(
                        gap_id, site_id, start_ms, reason,
                        previous_boot_id, current_boot_id,
                        previous_process_id, current_process_id, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        gap.gap_id,
                        site_id,
                        prior_boundary_ms,
                        reason.value,
                        gap.previous_boot_id,
                        boot_id,
                        gap.previous_process_id,
                        process_id,
                        started_at_ms,
                    ),
                )

                interruption = interrupt_for_gap(state, gap.started_at)
                if interruption.events:
                    self._apply_events(site_id, interruption.events)
                self._save_state(site_id, interruption.state, started_at_ms)

            if previous is None:
                self._connection.execute(
                    """
                    INSERT INTO monitor_runtime(
                        site_id, boot_id, process_id, started_at_ms,
                        last_heartbeat_ms, last_round_at_ms
                    ) VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (site_id, boot_id, process_id, started_at_ms, started_at_ms),
                )
            elif runtime_changed:
                self._connection.execute(
                    """
                    UPDATE monitor_runtime SET
                        boot_id = ?, process_id = ?, started_at_ms = ?,
                        last_heartbeat_ms = ?, last_round_at_ms = NULL
                    WHERE site_id = ?
                    """,
                    (boot_id, process_id, started_at_ms, started_at_ms, site_id),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE monitor_runtime SET last_heartbeat_ms = ?
                    WHERE site_id = ?
                    """,
                    (started_at_ms, site_id),
                )

            self._connection.commit()
            return gap
        except Exception:
            self._connection.rollback()
            raise

    def record_heartbeat(
        self,
        site_id: str,
        boot_id: str,
        process_id: str,
        observed_at: datetime,
    ) -> None:
        observed_at_ms = _to_epoch_ms(observed_at)
        with self._connection:
            updated = self._connection.execute(
                """
                UPDATE monitor_runtime SET last_heartbeat_ms = ?
                WHERE site_id = ? AND boot_id = ? AND process_id = ?
                """,
                (observed_at_ms, site_id, boot_id, process_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError("heartbeat does not match the active runtime")

    def open_clock_gap(
        self,
        site_id: str,
        boot_id: str,
        process_id: str,
        detected_at: datetime,
    ) -> MonitoringGapRecord:
        """Open one clock-uncertainty gap without inventing a time boundary."""

        detected_at_ms = _to_epoch_ms(detected_at)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            runtime = self._connection.execute(
                "SELECT * FROM monitor_runtime WHERE site_id = ?",
                (site_id,),
            ).fetchone()
            if runtime is None or (
                runtime["boot_id"] != boot_id
                or runtime["process_id"] != process_id
            ):
                raise RuntimeError("clock check does not match the active runtime")
            existing = self._connection.execute(
                """
                SELECT * FROM monitoring_gaps
                WHERE site_id = ? AND end_ms IS NULL
                """,
                (site_id,),
            ).fetchone()
            if existing is not None:
                self._connection.commit()
                return _gap_from_row(existing)

            state = self.load_state(site_id)
            start_ms = detected_at_ms
            if state.last_observed_at is not None:
                start_ms = max(start_ms, _to_epoch_ms(state.last_observed_at))
            gap = MonitoringGapRecord(
                gap_id=_gap_id(site_id, start_ms),
                site_id=site_id,
                started_at=_from_epoch_ms(start_ms),
                reason=GapReason.CLOCK_UNCERTAIN,
                previous_boot_id=boot_id,
                current_boot_id=boot_id,
                previous_process_id=process_id,
                current_process_id=process_id,
            )
            self._connection.execute(
                """
                INSERT INTO monitoring_gaps(
                    gap_id, site_id, start_ms, reason,
                    previous_boot_id, current_boot_id,
                    previous_process_id, current_process_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gap.gap_id,
                    site_id,
                    start_ms,
                    gap.reason.value,
                    boot_id,
                    boot_id,
                    process_id,
                    process_id,
                    detected_at_ms,
                ),
            )
            interruption = interrupt_for_gap(state, gap.started_at)
            if interruption.events:
                self._apply_events(site_id, interruption.events)
            self._save_state(site_id, interruption.state, detected_at_ms)
            self._connection.commit()
            return gap
        except Exception:
            self._connection.rollback()
            raise

    def open_stale_gap(
        self,
        site_id: str,
        boot_id: str,
        process_id: str,
        detected_at: datetime,
        *,
        stale_after_seconds: int,
    ) -> Optional[MonitoringGapRecord]:
        """Open a gap when the active runtime heartbeat is stale."""

        if stale_after_seconds < 1:
            raise ValueError("stale_after_seconds must be positive")
        detected_at_ms = _to_epoch_ms(detected_at)
        stale_after_ms = stale_after_seconds * 1000

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            runtime = self._connection.execute(
                "SELECT * FROM monitor_runtime WHERE site_id = ?",
                (site_id,),
            ).fetchone()
            if runtime is None or (
                runtime["boot_id"] != boot_id
                or runtime["process_id"] != process_id
            ):
                raise RuntimeError("stale check does not match the active runtime")

            existing = self._connection.execute(
                """
                SELECT * FROM monitoring_gaps
                WHERE site_id = ? AND end_ms IS NULL
                """,
                (site_id,),
            ).fetchone()
            if existing is not None:
                self._connection.commit()
                return _gap_from_row(existing)

            if detected_at_ms - runtime["last_heartbeat_ms"] <= stale_after_ms:
                self._connection.commit()
                return None

            state = self.load_state(site_id)
            start_ms = _optional_to_epoch_ms(state.last_observed_at)
            if start_ms is None:
                start_ms = runtime["last_round_at_ms"]
            if start_ms is None:
                start_ms = runtime["last_heartbeat_ms"]

            reason = GapReason.STALE_HEARTBEAT
            if start_ms > detected_at_ms:
                start_ms = detected_at_ms
                reason = GapReason.CLOCK_UNCERTAIN

            gap = MonitoringGapRecord(
                gap_id=_gap_id(site_id, start_ms),
                site_id=site_id,
                started_at=_from_epoch_ms(start_ms),
                reason=reason,
                previous_boot_id=boot_id,
                current_boot_id=boot_id,
                previous_process_id=process_id,
                current_process_id=process_id,
            )
            self._connection.execute(
                """
                INSERT INTO monitoring_gaps(
                    gap_id, site_id, start_ms, reason,
                    previous_boot_id, current_boot_id,
                    previous_process_id, current_process_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gap.gap_id,
                    site_id,
                    start_ms,
                    reason.value,
                    boot_id,
                    boot_id,
                    process_id,
                    process_id,
                    detected_at_ms,
                ),
            )
            interruption = interrupt_for_gap(state, gap.started_at)
            if interruption.events:
                self._apply_events(site_id, interruption.events)
            self._save_state(site_id, interruption.state, detected_at_ms)
            self._connection.commit()
            return gap
        except Exception:
            self._connection.rollback()
            raise

    def record_round(
        self,
        record: RoundRecord,
        samples: Sequence[ProbeSampleRecord],
        result: StateMachineResult,
    ) -> bool:
        """Store a round and its resulting state once.

        Returns ``False`` when ``round_id`` was already stored. No events or
        state updates are replayed for a duplicate.
        """

        _validate_round_result(record, result)
        observed_at_ms = _to_epoch_ms(record.observed_at)

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            inserted = self._connection.execute(
                """
                INSERT INTO probe_rounds(
                    round_id, site_id, observed_at_ms, status,
                    gateway_status, external_ip_status, dns_status, https_status,
                    timing_trusted, route_trusted, boot_id, process_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(round_id) DO NOTHING
                """,
                (
                    record.round_id,
                    record.site_id,
                    observed_at_ms,
                    record.status.value,
                    record.evidence.gateway.value,
                    record.evidence.external_ip.value,
                    record.evidence.dns.value,
                    record.evidence.https.value,
                    int(record.evidence.timing_trusted),
                    int(record.evidence.route_trusted),
                    record.boot_id,
                    record.process_id,
                ),
            ).rowcount
            if inserted == 0:
                existing = self._connection.execute(
                    """
                    SELECT site_id, observed_at_ms, status
                    FROM probe_rounds WHERE round_id = ?
                    """,
                    (record.round_id,),
                ).fetchone()
                if existing is None or (
                    existing["site_id"] != record.site_id
                    or existing["observed_at_ms"] != observed_at_ms
                    or existing["status"] != record.status.value
                ):
                    raise ValueError("round_id is already used by different data")
                self._connection.rollback()
                return False

            for sample in samples:
                self._insert_sample(record.round_id, sample)

            self._ensure_initial_interval(record, result)
            self._apply_events(
                record.site_id,
                result.events,
                failure_summary_json=_failure_summary_json(samples),
            )
            self._save_state(record.site_id, result.state, observed_at_ms)
            if record.evidence.timing_trusted:
                self._connection.execute(
                    """
                    UPDATE monitoring_gaps SET end_ms = ?
                    WHERE site_id = ? AND end_ms IS NULL
                    """,
                    (observed_at_ms, record.site_id),
                )
            self._update_runtime_for_round(record, observed_at_ms)
            self._connection.commit()
            return True
        except Exception:
            self._connection.rollback()
            raise

    def _update_runtime_for_round(
        self,
        record: RoundRecord,
        observed_at_ms: int,
    ) -> None:
        if record.boot_id is None and record.process_id is None:
            return
        if record.boot_id is None or record.process_id is None:
            raise ValueError("round runtime identity requires boot_id and process_id")

        updated = self._connection.execute(
            """
            UPDATE monitor_runtime SET
                last_heartbeat_ms = ?, last_round_at_ms = ?
            WHERE site_id = ? AND boot_id = ? AND process_id = ?
            """,
            (
                observed_at_ms,
                observed_at_ms,
                record.site_id,
                record.boot_id,
                record.process_id,
            ),
        ).rowcount
        if updated != 1:
            raise RuntimeError("round does not match the active runtime")

    def _insert_sample(
        self,
        round_id: str,
        sample: ProbeSampleRecord,
    ) -> None:
        if sample.latency_ms is not None and sample.latency_ms < 0:
            raise ValueError("sample latency_ms cannot be negative")
        metadata_json = json.dumps(
            sample.metadata,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(metadata_json) > _MAX_METADATA_JSON_LENGTH:
            raise ValueError("sample metadata is too large")

        self._connection.execute(
            """
            INSERT INTO probe_samples(
                round_id, target_id, outcome, latency_ms, error_class,
                metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                round_id,
                sample.target_id,
                sample.outcome.value,
                sample.latency_ms,
                sample.error_class,
                metadata_json,
            ),
        )

    def _ensure_initial_interval(
        self,
        record: RoundRecord,
        result: StateMachineResult,
    ) -> None:
        existing = self._connection.execute(
            """
            SELECT 1 FROM status_intervals
            WHERE site_id = ? AND end_ms IS NULL
            """,
            (record.site_id,),
        ).fetchone()
        if existing is not None:
            return

        changed = next(
            (
                event
                for event in result.events
                if isinstance(event, StableStatusChanged)
            ),
            None,
        )
        status = changed.previous if changed is not None else result.state.stable_status
        start = changed.observed_at if changed is not None else record.observed_at
        start_ms = _to_epoch_ms(start)
        self._connection.execute(
            """
            INSERT INTO status_intervals(
                interval_id, site_id, status, start_ms, confirmed_at_ms
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                _interval_id(record.site_id, status, start_ms),
                record.site_id,
                status.value,
                start_ms,
                start_ms,
            ),
        )

    def _apply_events(
        self,
        site_id: str,
        events: Sequence[object],
        *,
        failure_summary_json: Optional[str] = None,
    ) -> None:
        previous_incident_id = None
        for event in events:
            if isinstance(event, IncidentEnded):
                incident_id = _incident_id(site_id, event.incident)
                lifecycle = (
                    "interrupted"
                    if event.reason is IncidentEndReason.MONITORING_UNKNOWN
                    else "closed"
                )
                updated = self._connection.execute(
                    """
                    UPDATE incidents SET
                        lifecycle = ?, observed_end_ms = ?, confirmed_end_ms = ?,
                        end_reason = ?, updated_at_ms = ?
                    WHERE incident_id = ? AND lifecycle = 'open'
                    """,
                    (
                        lifecycle,
                        _to_epoch_ms(event.observed_end),
                        _optional_to_epoch_ms(event.confirmed_end),
                        event.reason.value,
                        _to_epoch_ms(event.confirmed_end or event.observed_end),
                        incident_id,
                    ),
                ).rowcount
                if updated != 1:
                    raise RuntimeError("incident end event has no matching open incident")
                previous_incident_id = incident_id
            elif isinstance(event, StableStatusChanged):
                observed_at_ms = _to_epoch_ms(event.observed_at)
                updated = self._connection.execute(
                    """
                    UPDATE status_intervals SET end_ms = ?
                    WHERE site_id = ? AND end_ms IS NULL
                    """,
                    (observed_at_ms, site_id),
                ).rowcount
                if updated != 1:
                    raise RuntimeError("status transition requires one open interval")
                self._connection.execute(
                    """
                    INSERT INTO status_intervals(
                        interval_id, site_id, status, start_ms, confirmed_at_ms
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        _interval_id(site_id, event.current, observed_at_ms),
                        site_id,
                        event.current.value,
                        observed_at_ms,
                        _to_epoch_ms(event.confirmed_at),
                    ),
                )
            elif isinstance(event, IncidentOpened):
                incident_id = _incident_id(site_id, event.incident)
                now_ms = _to_epoch_ms(event.incident.confirmed_start)
                self._connection.execute(
                    """
                    INSERT INTO incidents(
                        incident_id, site_id, status, lifecycle,
                        observed_start_ms, confirmed_start_ms,
                        previous_incident_id, created_at_ms, updated_at_ms,
                        failure_summary_json
                    ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        incident_id,
                        site_id,
                        event.incident.status.value,
                        _to_epoch_ms(event.incident.observed_start),
                        now_ms,
                        previous_incident_id,
                        now_ms,
                        now_ms,
                        failure_summary_json,
                    ),
                )
            else:
                raise TypeError("unsupported state-machine event")

    def _save_state(
        self,
        site_id: str,
        state: MonitoringState,
        updated_at_ms: int,
    ) -> None:
        open_incident_id = (
            _incident_id(site_id, state.open_incident)
            if state.open_incident is not None
            else None
        )
        self._connection.execute(
            """
            INSERT INTO monitor_state(
                site_id, stable_status, pending_status, pending_count,
                pending_started_at_ms, open_incident_id,
                last_observed_at_ms, last_observed_status, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(site_id) DO UPDATE SET
                stable_status = excluded.stable_status,
                pending_status = excluded.pending_status,
                pending_count = excluded.pending_count,
                pending_started_at_ms = excluded.pending_started_at_ms,
                open_incident_id = excluded.open_incident_id,
                last_observed_at_ms = excluded.last_observed_at_ms,
                last_observed_status = excluded.last_observed_status,
                updated_at_ms = excluded.updated_at_ms
            """,
            (
                site_id,
                state.stable_status.value,
                state.pending_status.value if state.pending_status else None,
                state.pending_count,
                _optional_to_epoch_ms(state.pending_started_at),
                open_incident_id,
                _optional_to_epoch_ms(state.last_observed_at),
                (
                    state.last_observed_status.value
                    if state.last_observed_status
                    else None
                ),
                updated_at_ms,
            ),
        )


def _validate_round_result(
    record: RoundRecord,
    result: StateMachineResult,
) -> None:
    if result.state.last_observed_at != record.observed_at:
        raise ValueError("state-machine result timestamp does not match round")
    if result.state.last_observed_status is not record.status:
        raise ValueError("state-machine result status does not match round")


def _failure_summary_json(samples: Sequence[ProbeSampleRecord]) -> str:
    failures = [
        {
            "target_id": sample.target_id,
            "outcome": sample.outcome.value,
            "error_class": sample.error_class,
        }
        for sample in samples
        if sample.outcome is not ProbeOutcome.SUCCESS
    ]
    failures.sort(key=lambda item: item["target_id"])
    return json.dumps(
        failures,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _incident_id(site_id: str, incident: OpenIncident) -> str:
    value = (
        f"incident:{site_id}:{incident.status.value}:"
        f"{_to_epoch_ms(incident.observed_start)}"
    )
    return str(uuid.uuid5(_ID_NAMESPACE, value))


def _interval_id(site_id: str, status: RoundStatus, start_ms: int) -> str:
    return str(
        uuid.uuid5(
            _ID_NAMESPACE,
            f"interval:{site_id}:{status.value}:{start_ms}",
        )
    )


def _gap_id(site_id: str, start_ms: int) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"gap:{site_id}:{start_ms}"))


def _gap_from_row(row: sqlite3.Row) -> MonitoringGapRecord:
    return MonitoringGapRecord(
        gap_id=row["gap_id"],
        site_id=row["site_id"],
        started_at=_from_epoch_ms(row["start_ms"]),
        reason=GapReason(row["reason"]),
        previous_boot_id=row["previous_boot_id"],
        current_boot_id=row["current_boot_id"],
        previous_process_id=row["previous_process_id"],
        current_process_id=row["current_process_id"],
    )


def _to_epoch_ms(value: datetime) -> int:
    _validate_utc(value)
    delta = value - _EPOCH
    return (
        delta.days * 86_400_000
        + delta.seconds * 1000
        + delta.microseconds // 1000
    )


def _optional_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    return _to_epoch_ms(value) if value is not None else None


def _from_epoch_ms(value: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=value)


def _optional_from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    return _from_epoch_ms(value) if value is not None else None


def _validate_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
