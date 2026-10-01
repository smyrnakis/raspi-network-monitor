"""Read-only monitoring queries independent of the HTTP framework."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from home_internet_monitor.domain.models import RoundStatus

_CLASSIFIED_STATUSES = (
    RoundStatus.ONLINE.value,
    RoundStatus.INTERNET_DOWN.value,
    RoundStatus.GATEWAY_UNREACHABLE.value,
    RoundStatus.DNS_FAILURE.value,
    RoundStatus.PARTIAL_CONNECTIVITY.value,
)

_GAP_REASONS = {
    "host_reboot",
    "process_restart",
    "stale_heartbeat",
    "clock_uncertain",
}
_HISTORY_CATEGORIES = {status.value for status in RoundStatus} | _GAP_REASONS
_INCIDENT_EPISODES_CTE = """
RECURSIVE incident_chains(incident_id, episode_id) AS (
    SELECT incident_id, incident_id
    FROM incidents
    WHERE previous_incident_id IS NULL
    UNION ALL
    SELECT child.incident_id, parent.episode_id
    FROM incidents child
    JOIN incident_chains parent
      ON child.previous_incident_id = parent.incident_id
),
incident_episodes AS (
    SELECT first.site_id, chain.episode_id,
           MIN(first.observed_start_ms) AS observed_start_ms,
           CASE WHEN MAX(first.observed_end_ms IS NULL) = 1
                THEN NULL ELSE MAX(first.observed_end_ms) END AS observed_end_ms,
           MIN(first.confirmed_start_ms) AS confirmed_start_ms,
           CASE WHEN MAX(first.confirmed_end_ms IS NULL) = 1
                THEN NULL ELSE MAX(first.confirmed_end_ms) END AS confirmed_end_ms,
           (SELECT latest.status
              FROM incidents latest
              JOIN incident_chains latest_chain
                ON latest_chain.incident_id = latest.incident_id
             WHERE latest_chain.episode_id = chain.episode_id
             ORDER BY latest.observed_start_ms DESC LIMIT 1) AS status,
           (SELECT latest.lifecycle
              FROM incidents latest
              JOIN incident_chains latest_chain
                ON latest_chain.incident_id = latest.incident_id
             WHERE latest_chain.episode_id = chain.episode_id
             ORDER BY latest.observed_start_ms DESC LIMIT 1) AS lifecycle,
           (SELECT latest.end_reason
              FROM incidents latest
              JOIN incident_chains latest_chain
                ON latest_chain.incident_id = latest.incident_id
             WHERE latest_chain.episode_id = chain.episode_id
             ORDER BY latest.observed_start_ms DESC LIMIT 1) AS end_reason,
           GROUP_CONCAT(first.status, ',') AS phase_categories,
           COUNT(*) AS phase_count
    FROM incidents first
    JOIN incident_chains chain ON chain.incident_id = first.incident_id
    GROUP BY first.site_id, chain.episode_id
)
"""

_HISTORY_CTE = f"""
{_INCIDENT_EPISODES_CTE},
history AS (
    SELECT site_id, 'incident' AS event_type, episode_id AS event_id,
           status AS category, lifecycle AS state,
           observed_start_ms AS start_ms, observed_end_ms AS end_ms,
           confirmed_start_ms, confirmed_end_ms, end_reason,
           NULL AS previous_incident_id, NULL AS failure_summary_json,
           phase_categories, phase_count
    FROM incident_episodes
    UNION ALL
    SELECT site_id, 'gap' AS event_type, gap_id AS event_id,
           reason AS category,
           CASE WHEN end_ms IS NULL THEN 'open' ELSE 'closed' END AS state,
           start_ms, end_ms, NULL AS confirmed_start_ms,
           NULL AS confirmed_end_ms, NULL AS end_reason,
           NULL AS previous_incident_id, NULL AS failure_summary_json,
           reason AS phase_categories, 1 AS phase_count
    FROM monitoring_gaps
)
"""


class MonitoringQueries:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def health(
        self, site_id: str, now: datetime, stale_after_seconds: float
    ) -> Dict[str, Any]:
        _require_utc(now)
        schema_row = self._connection.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()
        runtime = self._connection.execute(
            "SELECT * FROM monitor_runtime WHERE site_id = ?", (site_id,)
        ).fetchone()
        heartbeat = _optional_datetime(runtime["last_heartbeat_ms"]) if runtime else None
        heartbeat_age = (
            max(0.0, (now - heartbeat).total_seconds()) if heartbeat else None
        )
        healthy = heartbeat_age is not None and heartbeat_age <= stale_after_seconds
        return {
            "status": "healthy" if healthy else "degraded",
            "database": "readable",
            "schema_version": schema_row["version"] if schema_row else None,
            "last_heartbeat": _iso(heartbeat),
            "heartbeat_age_seconds": heartbeat_age,
        }

    def current_status(self, site_id: str) -> Dict[str, Any]:
        site = self._connection.execute(
            "SELECT site_id, display_name, timezone FROM sites WHERE site_id = ?",
            (site_id,),
        ).fetchone()
        if site is None:
            raise KeyError(site_id)
        state = self._connection.execute(
            "SELECT * FROM monitor_state WHERE site_id = ?", (site_id,)
        ).fetchone()
        latest = self._connection.execute(
            """
            SELECT observed_at_ms, status, gateway_status, external_ip_status,
                   dns_status, https_status, timing_trusted, route_trusted
            FROM probe_rounds WHERE site_id = ?
            ORDER BY observed_at_ms DESC LIMIT 1
            """,
            (site_id,),
        ).fetchone()
        incident = self._connection.execute(
            """
            SELECT incident_id, status, lifecycle, observed_start_ms,
                   confirmed_start_ms
            FROM incidents WHERE site_id = ? AND lifecycle = 'open'
            """,
            (site_id,),
        ).fetchone()
        return {
            "site": dict(site),
            "stable_status": (
                state["stable_status"] if state else RoundStatus.MONITORING_UNKNOWN.value
            ),
            "pending_status": state["pending_status"] if state else None,
            "pending_count": state["pending_count"] if state else 0,
            "pending_started_at": _row_iso(state, "pending_started_at_ms"),
            "last_observed_at": _row_iso(state, "last_observed_at_ms"),
            "last_observed_status": state["last_observed_status"] if state else None,
            "latest_round": _round_dict(latest),
            "open_incident": _incident_dict(incident),
        }

    def incidents(
        self,
        site_id: str,
        limit: int = 100,
        offset: int = 0,
        *,
        minimum_duration_seconds: int = 0,
    ) -> List[Dict[str, Any]]:
        _validate_page(limit, offset)
        if minimum_duration_seconds < 0:
            raise ValueError("minimum_duration_seconds cannot be negative")
        rows = self._connection.execute(
            f"""
            WITH {_INCIDENT_EPISODES_CTE}
            SELECT episode_id AS incident_id, status, lifecycle,
                   observed_start_ms, confirmed_start_ms,
                   observed_end_ms, confirmed_end_ms, end_reason,
                   NULL AS previous_incident_id, '' AS notes,
                   phase_categories, phase_count
            FROM incident_episodes WHERE site_id = ?
              AND (observed_end_ms IS NULL
                   OR observed_end_ms - observed_start_ms >= ?)
            ORDER BY observed_start_ms DESC LIMIT ? OFFSET ?
            """,
            (site_id, minimum_duration_seconds * 1000, limit, offset),
        ).fetchall()
        incidents = [_incident_dict(row) for row in rows]
        for incident in incidents:
            phases = self._incident_phases(incident["incident_id"])
            incident["categories"] = [phase["category"] for phase in phases]
            incident["phase_categories"] = ",".join(incident["categories"])
        return incidents

    def gaps(
        self,
        site_id: str,
        limit: int = 100,
        offset: int = 0,
        *,
        minimum_duration_seconds: int = 0,
    ) -> List[Dict[str, Any]]:
        _validate_page(limit, offset)
        if minimum_duration_seconds < 0:
            raise ValueError("minimum_duration_seconds must be non-negative")
        rows = self._connection.execute(
            """
            SELECT gap_id, start_ms, end_ms, reason
            FROM monitoring_gaps WHERE site_id = ?
              AND (end_ms IS NULL OR end_ms - start_ms >= ?)
            ORDER BY start_ms DESC LIMIT ? OFFSET ?
            """,
            (site_id, minimum_duration_seconds * 1000, limit, offset),
        ).fetchall()
        return [
            {
                "gap_id": row["gap_id"],
                "start": _iso(_datetime(row["start_ms"])),
                "end": _iso(_optional_datetime(row["end_ms"])),
                "reason": row["reason"],
            }
            for row in rows
        ]

    def incidents_for_export(self, site_id: str) -> List[Dict[str, Any]]:
        rows = self._connection.execute(
            f"""
            WITH {_INCIDENT_EPISODES_CTE}
            SELECT episode_id AS incident_id, status, lifecycle,
                   observed_start_ms, confirmed_start_ms,
                   observed_end_ms, confirmed_end_ms, end_reason,
                   NULL AS previous_incident_id, '' AS notes,
                   phase_categories, phase_count
            FROM incident_episodes WHERE site_id = ?
            ORDER BY observed_start_ms DESC
            """,
            (site_id,),
        ).fetchall()
        incidents = [_incident_dict(row) for row in rows]
        for incident in incidents:
            phases = self._incident_phases(incident["incident_id"])
            incident["categories"] = [phase["category"] for phase in phases]
            incident["phase_categories"] = ",".join(incident["categories"])
        return incidents

    def history(
        self,
        site_id: str,
        limit: int = 20,
        offset: int = 0,
        *,
        event_type: Optional[str] = None,
        category: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Return a filtered, combined incident and monitoring-gap page."""

        _validate_page(limit, offset)
        where, parameters = _history_filters(
            site_id,
            event_type=event_type,
            category=category,
            start=start,
            end=end,
        )
        predicate = " AND ".join(where)
        total = self._connection.execute(
            f"WITH {_HISTORY_CTE} SELECT COUNT(*) FROM history WHERE {predicate}",
            parameters,
        ).fetchone()[0]
        rows = self._connection.execute(
            f"""
            WITH {_HISTORY_CTE}
            SELECT event_type, event_id, category, state, start_ms, end_ms,
                   confirmed_start_ms, confirmed_end_ms, end_reason,
                   previous_incident_id, failure_summary_json,
                   phase_categories, phase_count
            FROM history
            WHERE {predicate}
            ORDER BY start_ms DESC, event_type, event_id
            LIMIT ? OFFSET ?
            """,
            (*parameters, limit, offset),
        ).fetchall()
        return {
            "items": [self._history_dict_with_fallback(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def history_for_export(
        self,
        site_id: str,
        *,
        event_type: Optional[str] = None,
        category: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        where, parameters = _history_filters(
            site_id,
            event_type=event_type,
            category=category,
            start=start,
            end=end,
        )
        rows = self._connection.execute(
            f"""
            WITH {_HISTORY_CTE}
            SELECT event_type, event_id, category, state, start_ms, end_ms,
                   confirmed_start_ms, confirmed_end_ms, end_reason,
                   previous_incident_id, failure_summary_json,
                   phase_categories, phase_count
            FROM history
            WHERE {' AND '.join(where)}
            ORDER BY start_ms DESC, event_type, event_id
            """,
            parameters,
        ).fetchall()
        return [self._history_dict_with_fallback(row) for row in rows]

    def history_item(
        self, site_id: str, event_type: str, event_id: str
    ) -> Optional[Dict[str, Any]]:
        if event_type not in {"incident", "gap"}:
            raise ValueError("event_type must be incident or gap")
        resolved_id = event_id
        if event_type == "incident":
            resolved = self._connection.execute(
                f"""
                WITH {_INCIDENT_EPISODES_CTE}
                SELECT episode_id FROM incident_chains WHERE incident_id = ?
                """,
                (event_id,),
            ).fetchone()
            if resolved is None:
                return None
            resolved_id = resolved["episode_id"]
        row = self._connection.execute(
            f"""
            WITH {_HISTORY_CTE}
            SELECT event_type, event_id, category, state, start_ms, end_ms,
                   confirmed_start_ms, confirmed_end_ms, end_reason,
                   previous_incident_id, failure_summary_json,
                   phase_categories, phase_count
            FROM history
            WHERE site_id = ? AND event_type = ? AND event_id = ?
            """,
            (site_id, event_type, resolved_id),
        ).fetchone()
        return self._history_dict_with_fallback(row) if row is not None else None

    def _history_dict_with_fallback(self, row: sqlite3.Row) -> Dict[str, Any]:
        result = _history_dict(row)
        if result["event_type"] != "incident":
            return result
        phases = self._incident_phases(result["event_id"])
        result["phases"] = phases
        result["categories"] = [phase["category"] for phase in phases]
        result["phase_categories"] = ",".join(result["categories"])
        known_failures = [
            failed_test
            for phase in phases
            if phase["failed_tests"] is not None
            for failed_test in phase["failed_tests"]
        ]
        result["failed_tests"] = (
            _unique_failures(known_failures)
            if any(phase["failed_tests"] is not None for phase in phases)
            else None
        )
        return result

    def _incident_phases(self, episode_id: str) -> List[Dict[str, Any]]:
        rows = self._connection.execute(
            """
            WITH RECURSIVE phase_ids(incident_id) AS (
                SELECT incident_id FROM incidents WHERE incident_id = ?
                UNION ALL
                SELECT child.incident_id
                FROM incidents child
                JOIN phase_ids parent
                  ON child.previous_incident_id = parent.incident_id
            )
            SELECT i.incident_id, i.status, i.lifecycle,
                   i.observed_start_ms, i.confirmed_start_ms,
                   i.observed_end_ms, i.confirmed_end_ms, i.end_reason,
                   i.failure_summary_json
            FROM incidents i
            JOIN phase_ids ON phase_ids.incident_id = i.incident_id
            ORDER BY i.observed_start_ms
            """,
            (episode_id,),
        ).fetchall()
        phases = []
        for row in rows:
            failures = _parse_failures(row["failure_summary_json"])
            if failures is None:
                samples = self._connection.execute(
                    """
                    SELECT target_id, outcome, error_class
                    FROM probe_samples
                    WHERE round_id = (
                        SELECT round_id FROM probe_rounds
                        WHERE site_id = (
                            SELECT site_id FROM incidents WHERE incident_id = ?
                        ) AND observed_at_ms = ?
                    )
                    ORDER BY target_id
                    """,
                    (row["incident_id"], row["confirmed_start_ms"]),
                ).fetchall()
                if samples:
                    failures = [
                        {
                            "target_id": sample["target_id"],
                            "outcome": sample["outcome"],
                            "error_class": sample["error_class"],
                        }
                        for sample in samples
                        if sample["outcome"] != "success"
                    ]
            if failures is not None:
                self._add_target_labels(failures)
            start = _datetime(row["observed_start_ms"])
            end = _optional_datetime(row["observed_end_ms"])
            phases.append(
                {
                    "incident_id": row["incident_id"],
                    "category": row["status"],
                    "state": row["lifecycle"],
                    "start": _iso(start),
                    "end": _iso(end),
                    "duration_seconds": (end - start).total_seconds() if end else None,
                    "confirmed_start": _iso(_datetime(row["confirmed_start_ms"])),
                    "confirmed_end": _iso(
                        _optional_datetime(row["confirmed_end_ms"])
                    ),
                    "end_reason": row["end_reason"],
                    "failed_tests": failures,
                }
            )
        return phases

    def _add_target_labels(self, failures: List[Dict[str, Any]]) -> None:
        for failed_test in failures:
            target = self._connection.execute(
                "SELECT label FROM probe_targets WHERE target_id = ?",
                (failed_test.get("target_id"),),
            ).fetchone()
            failed_test["label"] = (
                target["label"] if target is not None else failed_test.get("target_id")
            )

    def availability(
        self,
        site_id: str,
        start: datetime,
        end: datetime,
    ) -> Dict[str, Any]:
        _require_utc(start)
        _require_utc(end)
        if end <= start:
            raise ValueError("end must be after start")
        start_ms = _epoch_ms(start)
        end_ms = _epoch_ms(end)
        rows = self._connection.execute(
            """
            SELECT status, start_ms, end_ms
            FROM status_intervals
            WHERE site_id = ? AND start_ms < ?
              AND (end_ms IS NULL OR end_ms > ?)
            """,
            (site_id, end_ms, start_ms),
        ).fetchall()
        duration_by_status = {status.value: 0.0 for status in RoundStatus}
        for row in rows:
            clipped_start = max(start_ms, row["start_ms"])
            clipped_end = min(end_ms, row["end_ms"] or end_ms)
            if clipped_end > clipped_start:
                duration_by_status[row["status"]] += (
                    clipped_end - clipped_start
                ) / 1000.0

        window_seconds = (end_ms - start_ms) / 1000.0
        classified = sum(duration_by_status[key] for key in _CLASSIFIED_STATUSES)
        online = duration_by_status[RoundStatus.ONLINE.value]
        incident_row = self._connection.execute(
            f"""
            WITH {_INCIDENT_EPISODES_CTE}
            SELECT COUNT(*) AS incident_count
            FROM incident_episodes
            WHERE site_id = ?
              AND confirmed_start_ms >= ?
              AND confirmed_start_ms < ?
            """,
            (site_id, start_ms, end_ms),
        ).fetchone()
        incident_count = incident_row["incident_count"]
        return {
            "start": _iso(start),
            "end": _iso(end),
            "window_seconds": window_seconds,
            "classified_seconds": classified,
            "unknown_seconds": max(0.0, window_seconds - classified),
            "availability": online / classified if classified else None,
            "coverage": classified / window_seconds,
            "incident_count": incident_count,
            "mtbf_seconds": online / incident_count if incident_count else None,
            "duration_by_status_seconds": duration_by_status,
        }

    def timeline(
        self,
        site_id: str,
        start: datetime,
        end: datetime,
        max_segments: int = 5000,
    ) -> Dict[str, Any]:
        _require_utc(start)
        _require_utc(end)
        if end <= start:
            raise ValueError("end must be after start")
        if max_segments < 1:
            raise ValueError("max_segments must be positive")
        start_ms = _epoch_ms(start)
        end_ms = _epoch_ms(end)
        rows = self._connection.execute(
            """
            SELECT status, start_ms, end_ms
            FROM status_intervals
            WHERE site_id = ? AND start_ms < ?
              AND (end_ms IS NULL OR end_ms > ?)
            ORDER BY start_ms ASC
            LIMIT ?
            """,
            (site_id, end_ms, start_ms, max_segments + 1),
        ).fetchall()
        if len(rows) > max_segments:
            raise ValueError("selected window contains too many status transitions")
        segments = []
        for row in rows:
            clipped_start = max(start_ms, row["start_ms"])
            clipped_end = min(end_ms, row["end_ms"] or end_ms)
            if clipped_end <= clipped_start:
                continue
            segments.append(
                {
                    "status": row["status"],
                    "start": _iso(_datetime(clipped_start)),
                    "end": _iso(_datetime(clipped_end)),
                    "duration_seconds": (clipped_end - clipped_start) / 1000.0,
                }
            )
        return {
            "start": _iso(start),
            "end": _iso(end),
            "segments": segments,
        }

    def latency(
        self,
        site_id: str,
        start: datetime,
        end: datetime,
        bucket_seconds: int,
    ) -> Dict[str, Any]:
        """Return bounded, server-side latency buckets for ICMP probes."""

        _require_utc(start)
        _require_utc(end)
        if end <= start:
            raise ValueError("end must be after start")
        if not 60 <= bucket_seconds <= 86_400:
            raise ValueError("bucket_seconds must be between 60 and 86400")
        window_seconds = (end - start).total_seconds()
        if window_seconds / bucket_seconds > 2016:
            raise ValueError("selected latency window contains too many buckets")
        start_ms = _epoch_ms(start)
        end_ms = _epoch_ms(end)
        bucket_ms = bucket_seconds * 1000
        targets = self._connection.execute(
            """
            SELECT target_id, label, kind
            FROM probe_targets
            WHERE site_id = ? AND enabled = 1
              AND kind IN ('gateway', 'external_ip')
            ORDER BY kind, target_id
            """,
            (site_id,),
        ).fetchall()
        rows = self._connection.execute(
            """
            SELECT s.target_id,
                   ((r.observed_at_ms - ?) / ?) * ? + ? AS bucket_start_ms,
                   SUM(CASE WHEN s.outcome = 'success' THEN 1 ELSE 0 END)
                       AS success_count,
                   SUM(CASE WHEN s.outcome = 'failure' THEN 1 ELSE 0 END)
                       AS failure_count,
                   COUNT(CASE WHEN s.outcome = 'success' THEN s.latency_ms END)
                       AS latency_count,
                   AVG(CASE WHEN s.outcome = 'success' THEN s.latency_ms END)
                       AS latency_avg_ms,
                   MIN(CASE WHEN s.outcome = 'success' THEN s.latency_ms END)
                       AS latency_min_ms,
                   MAX(CASE WHEN s.outcome = 'success' THEN s.latency_ms END)
                       AS latency_max_ms
            FROM probe_rounds r
            JOIN probe_samples s ON s.round_id = r.round_id
            JOIN probe_targets t ON t.target_id = s.target_id
            WHERE r.site_id = ? AND r.observed_at_ms >= ?
              AND r.observed_at_ms < ?
              AND t.kind IN ('gateway', 'external_ip')
            GROUP BY s.target_id, bucket_start_ms
            ORDER BY s.target_id, bucket_start_ms
            """,
            (
                start_ms,
                bucket_ms,
                bucket_ms,
                start_ms,
                site_id,
                start_ms,
                end_ms,
            ),
        ).fetchall()
        points_by_target: Dict[str, List[Dict[str, Any]]] = {
            row["target_id"]: [] for row in targets
        }
        for row in rows:
            points_by_target.setdefault(row["target_id"], []).append(
                {
                    "start": _iso(_datetime(row["bucket_start_ms"])),
                    "success_count": row["success_count"],
                    "failure_count": row["failure_count"],
                    "latency_count": row["latency_count"],
                    "avg_ms": row["latency_avg_ms"],
                    "min_ms": row["latency_min_ms"],
                    "max_ms": row["latency_max_ms"],
                }
            )
        incident_rows = self._connection.execute(
            f"""
            WITH {_INCIDENT_EPISODES_CTE}
            SELECT episode_id, status, observed_start_ms, observed_end_ms
            FROM incident_episodes
            WHERE site_id = ? AND observed_start_ms < ?
              AND (observed_end_ms IS NULL OR observed_end_ms > ?)
            ORDER BY observed_start_ms
            LIMIT 501
            """,
            (site_id, end_ms, start_ms),
        ).fetchall()
        if len(incident_rows) > 500:
            raise ValueError("selected latency window contains too many incidents")
        return {
            "start": _iso(start),
            "end": _iso(end),
            "bucket_seconds": bucket_seconds,
            "incidents": [
                {
                    "incident_id": row["episode_id"],
                    "status": row["status"],
                    "start": _iso(_datetime(row["observed_start_ms"])),
                    "end": _iso(_optional_datetime(row["observed_end_ms"])),
                }
                for row in incident_rows
            ],
            "series": [
                {
                    "target_id": target["target_id"],
                    "label": target["label"],
                    "kind": target["kind"],
                    "points": points_by_target.get(target["target_id"], []),
                }
                for target in targets
            ],
        }


def default_window(now: datetime) -> tuple:
    _require_utc(now)
    return now - timedelta(days=30), now


def _round_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        "observed_at": _iso(_datetime(row["observed_at_ms"])),
        "status": row["status"],
        "components": {
            "gateway": row["gateway_status"],
            "external_ip": row["external_ip_status"],
            "dns": row["dns_status"],
            "https": row["https_status"],
        },
        "timing_trusted": bool(row["timing_trusted"]),
        "route_trusted": bool(row["route_trusted"]),
    }


def _incident_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    result = dict(row)
    for source, target in (
        ("observed_start_ms", "observed_start"),
        ("confirmed_start_ms", "confirmed_start"),
        ("observed_end_ms", "observed_end"),
        ("confirmed_end_ms", "confirmed_end"),
    ):
        if source in result:
            result[target] = _iso(_optional_datetime(result.pop(source)))
    if "phase_categories" in result:
        result["phase_categories"] = result["phase_categories"]
        result["categories"] = result["phase_categories"].split(",")
    return result


def _validate_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if offset < 0:
        raise ValueError("offset cannot be negative")


def _history_filters(
    site_id: str,
    *,
    event_type: Optional[str],
    category: Optional[str],
    start: Optional[datetime],
    end: Optional[datetime],
) -> tuple:
    if event_type not in {None, "incident", "gap"}:
        raise ValueError("event_type must be incident or gap")
    if category is not None and category not in _HISTORY_CATEGORIES:
        raise ValueError("unknown history category")
    if start is not None:
        _require_utc(start)
    if end is not None:
        _require_utc(end)
    if start is not None and end is not None and end <= start:
        raise ValueError("end must be after start")

    where = ["site_id = ?"]
    parameters: List[Any] = [site_id]
    if event_type is not None:
        where.append("event_type = ?")
        parameters.append(event_type)
    if category is not None:
        where.append("instr(',' || phase_categories || ',', ',' || ? || ',') > 0")
        parameters.append(category)
    if start is not None:
        where.append("(end_ms IS NULL OR end_ms >= ?)")
        parameters.append(_epoch_ms(start))
    if end is not None:
        where.append("start_ms < ?")
        parameters.append(_epoch_ms(end))
    return where, parameters


def _history_dict(row: sqlite3.Row) -> Dict[str, Any]:
    start = _datetime(row["start_ms"])
    end = _optional_datetime(row["end_ms"])
    failed_tests = _parse_failures(row["failure_summary_json"])
    return {
        "event_type": row["event_type"],
        "event_id": row["event_id"],
        "category": row["category"],
        "state": row["state"],
        "start": _iso(start),
        "end": _iso(end),
        "duration_seconds": (end - start).total_seconds() if end else None,
        "confirmed_start": _iso(_optional_datetime(row["confirmed_start_ms"])),
        "confirmed_end": _iso(_optional_datetime(row["confirmed_end_ms"])),
        "end_reason": row["end_reason"],
        "previous_incident_id": row["previous_incident_id"],
        "failed_tests": failed_tests,
        "phase_categories": row["phase_categories"],
        "categories": row["phase_categories"].split(","),
        "phase_count": row["phase_count"],
    }


def _parse_failures(value: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        return None
    return parsed


def _unique_failures(failures: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unique = {}
    for failed_test in failures:
        key = (
            failed_test.get("target_id"),
            failed_test.get("outcome"),
            failed_test.get("error_class"),
        )
        unique[key] = failed_test
    return list(unique.values())


def _row_iso(row: Optional[sqlite3.Row], key: str) -> Optional[str]:
    return _iso(_optional_datetime(row[key])) if row else None


def _datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def _optional_datetime(value: Optional[int]) -> Optional[datetime]:
    return _datetime(value) if value is not None else None


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
