"""Persistence for independent service monitors."""

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from home_internet_monitor.monitor.config import ServiceMonitorConfig


_ID_NAMESPACE = uuid.UUID("9c8299f7-e2df-49af-80c0-07e46b8abf38")
_STATUSES = {"up", "degraded", "down", "unknown"}


class ServiceMonitorRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def prepare(
        self, site_id: str, monitors: tuple[ServiceMonitorConfig, ...], now: datetime
    ) -> None:
        timestamp = _epoch_ms(now)
        with self._connection:
            for monitor in monitors:
                self._connection.execute(
                    """
                    INSERT INTO service_monitors(
                        monitor_id, site_id, kind, label, endpoint,
                        interval_seconds, timeout_seconds, display_mode,
                        enabled, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(monitor_id) DO UPDATE SET
                        site_id = excluded.site_id,
                        kind = excluded.kind,
                        label = excluded.label,
                        endpoint = excluded.endpoint,
                        interval_seconds = excluded.interval_seconds,
                        timeout_seconds = excluded.timeout_seconds,
                        display_mode = excluded.display_mode,
                        enabled = excluded.enabled,
                        updated_at_ms = excluded.updated_at_ms
                    """,
                    (
                        monitor.monitor_id,
                        site_id,
                        monitor.kind,
                        monitor.label,
                        monitor.endpoint,
                        monitor.interval_seconds,
                        monitor.timeout_seconds,
                        monitor.dashboard,
                        int(monitor.enabled),
                        timestamp,
                        timestamp,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO service_monitor_state(
                        monitor_id, stable_status, stable_since_ms,
                        pending_count, updated_at_ms
                    ) VALUES (?, 'unknown', ?, 0, ?)
                    """,
                    (monitor.monitor_id, timestamp, timestamp),
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO service_status_intervals(
                        interval_id, monitor_id, status, start_ms
                    ) VALUES (?, ?, 'unknown', ?)
                    """,
                    (_interval_id(monitor.monitor_id, "unknown", timestamp), monitor.monitor_id, timestamp),
                )

    def last_checked(self, monitor_id: str) -> Optional[datetime]:
        row = self._connection.execute(
            "SELECT last_checked_ms FROM service_monitor_state WHERE monitor_id = ?",
            (monitor_id,),
        ).fetchone()
        if row is None or row["last_checked_ms"] is None:
            return None
        return _datetime(row["last_checked_ms"])

    def record_observation(
        self,
        monitor: ServiceMonitorConfig,
        observed_at: datetime,
        status: str,
        *,
        latency_ms: Optional[float] = None,
        error_class: Optional[str] = None,
    ) -> None:
        if status not in _STATUSES:
            raise ValueError("invalid service status")
        timestamp = _epoch_ms(observed_at)
        with self._connection:
            state = self._connection.execute(
                "SELECT * FROM service_monitor_state WHERE monitor_id = ?",
                (monitor.monitor_id,),
            ).fetchone()
            if state is None:
                raise KeyError(monitor.monitor_id)
            if monitor.endpoint is not None:
                self._connection.execute(
                    """
                    INSERT INTO service_ping_samples(monitor_id, observed_at_ms, status, latency_ms)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(monitor_id, observed_at_ms) DO NOTHING
                    """,
                    (monitor.monitor_id, timestamp, status, latency_ms),
                )
            stable = state["stable_status"]
            stable_since_ms = state["stable_since_ms"]
            pending_status = state["pending_status"]
            pending_count = state["pending_count"]
            pending_started_ms = state["pending_started_ms"]

            if status == stable:
                pending_status = None
                pending_count = 0
                pending_started_ms = None
            else:
                if pending_status == status:
                    pending_count += 1
                else:
                    pending_status = status
                    pending_count = 1
                    pending_started_ms = timestamp
                threshold = (
                    1
                    if status == "unknown"
                    else monitor.recovery_threshold
                    if status == "up"
                    else monitor.failure_threshold
                )
                if pending_count >= threshold:
                    transition_at = pending_started_ms or timestamp
                    self._confirm_transition(
                        monitor.monitor_id,
                        stable,
                        status,
                        transition_at,
                        timestamp,
                        error_class,
                    )
                    stable = status
                    stable_since_ms = transition_at
                    pending_status = None
                    pending_count = 0
                    pending_started_ms = None

            self._connection.execute(
                """
                UPDATE service_monitor_state SET
                    stable_status = ?, stable_since_ms = ?,
                    pending_status = ?, pending_count = ?, pending_started_ms = ?,
                    last_checked_ms = ?, last_latency_ms = ?,
                    last_error_class = ?, updated_at_ms = ?
                WHERE monitor_id = ?
                """,
                (
                    stable,
                    stable_since_ms,
                    pending_status,
                    pending_count,
                    pending_started_ms,
                    timestamp,
                    latency_ms,
                    error_class,
                    timestamp,
                    monitor.monitor_id,
                ),
            )

    def _confirm_transition(
        self,
        monitor_id: str,
        previous: str,
        current: str,
        observed_start_ms: int,
        confirmed_at_ms: int,
        error_class: Optional[str],
    ) -> None:
        self._connection.execute(
            """
            UPDATE service_status_intervals SET end_ms = ?
            WHERE monitor_id = ? AND end_ms IS NULL
            """,
            (observed_start_ms, monitor_id),
        )
        self._connection.execute(
            """
            INSERT INTO service_status_intervals(
                interval_id, monitor_id, status, start_ms
            ) VALUES (?, ?, ?, ?)
            """,
            (
                _interval_id(monitor_id, current, observed_start_ms),
                monitor_id,
                current,
                observed_start_ms,
            ),
        )
        if previous in {"down", "degraded"}:
            self._connection.execute(
                """
                UPDATE service_incidents SET
                    lifecycle = ?, observed_end_ms = ?, confirmed_end_ms = ?,
                    end_reason = ?
                WHERE monitor_id = ? AND lifecycle = 'open'
                """,
                (
                    "closed" if current == "up" else "interrupted",
                    observed_start_ms,
                    confirmed_at_ms,
                    "recovered" if current == "up" else "status_changed",
                    monitor_id,
                ),
            )
        if current in {"down", "degraded"}:
            self._connection.execute(
                """
                INSERT INTO service_incidents(
                    incident_id, monitor_id, status, lifecycle,
                    observed_start_ms, confirmed_start_ms, error_class
                ) VALUES (?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    _incident_id(monitor_id, current, observed_start_ms),
                    monitor_id,
                    current,
                    observed_start_ms,
                    confirmed_at_ms,
                    error_class,
                ),
            )


def _interval_id(monitor_id: str, status: str, start_ms: int) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"interval:{monitor_id}:{status}:{start_ms}"))


def _incident_id(monitor_id: str, status: str, start_ms: int) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"incident:{monitor_id}:{status}:{start_ms}"))


def _epoch_ms(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
