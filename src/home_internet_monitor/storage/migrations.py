"""Ordered SQLite schema migrations."""

import sqlite3
from typing import Sequence, Tuple


Migration = Tuple[int, Sequence[str]]


MIGRATIONS: Sequence[Migration] = (
    (
        1,
        (
            """
            CREATE TABLE sites (
                site_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                timezone TEXT NOT NULL,
                settings_revision INTEGER NOT NULL DEFAULT 1,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE probe_targets (
                target_id TEXT PRIMARY KEY,
                site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (
                    kind IN ('gateway', 'external_ip', 'dns', 'https')
                ),
                label TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                timeout_ms INTEGER NOT NULL CHECK (timeout_ms > 0),
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE probe_rounds (
                round_id TEXT PRIMARY KEY,
                site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
                observed_at_ms INTEGER NOT NULL,
                status TEXT NOT NULL,
                gateway_status TEXT NOT NULL,
                external_ip_status TEXT NOT NULL,
                dns_status TEXT NOT NULL,
                https_status TEXT NOT NULL,
                timing_trusted INTEGER NOT NULL CHECK (timing_trusted IN (0, 1)),
                route_trusted INTEGER NOT NULL CHECK (route_trusted IN (0, 1)),
                boot_id TEXT,
                process_id TEXT,
                UNIQUE (site_id, observed_at_ms)
            )
            """,
            """
            CREATE INDEX probe_rounds_site_time
            ON probe_rounds(site_id, observed_at_ms DESC)
            """,
            """
            CREATE TABLE probe_samples (
                sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id TEXT NOT NULL REFERENCES probe_rounds(round_id) ON DELETE CASCADE,
                target_id TEXT NOT NULL REFERENCES probe_targets(target_id),
                outcome TEXT NOT NULL CHECK (
                    outcome IN ('success', 'failure', 'unknown')
                ),
                latency_ms REAL CHECK (latency_ms IS NULL OR latency_ms >= 0),
                error_class TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE (round_id, target_id)
            )
            """,
            """
            CREATE INDEX probe_samples_target_round
            ON probe_samples(target_id, round_id)
            """,
            """
            CREATE TABLE status_intervals (
                interval_id TEXT PRIMARY KEY,
                site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                start_ms INTEGER NOT NULL,
                confirmed_at_ms INTEGER NOT NULL,
                end_ms INTEGER,
                CHECK (end_ms IS NULL OR end_ms >= start_ms)
            )
            """,
            """
            CREATE UNIQUE INDEX status_intervals_one_open
            ON status_intervals(site_id) WHERE end_ms IS NULL
            """,
            """
            CREATE INDEX status_intervals_site_start
            ON status_intervals(site_id, start_ms DESC)
            """,
            """
            CREATE TABLE incidents (
                incident_id TEXT PRIMARY KEY,
                site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                lifecycle TEXT NOT NULL CHECK (
                    lifecycle IN ('open', 'closed', 'interrupted')
                ),
                observed_start_ms INTEGER NOT NULL,
                confirmed_start_ms INTEGER NOT NULL,
                observed_end_ms INTEGER,
                confirmed_end_ms INTEGER,
                end_reason TEXT,
                previous_incident_id TEXT REFERENCES incidents(incident_id),
                notes TEXT NOT NULL DEFAULT '',
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                CHECK (
                    observed_end_ms IS NULL
                    OR observed_end_ms >= observed_start_ms
                )
            )
            """,
            """
            CREATE UNIQUE INDEX incidents_one_open
            ON incidents(site_id) WHERE lifecycle = 'open'
            """,
            """
            CREATE INDEX incidents_site_start
            ON incidents(site_id, observed_start_ms DESC)
            """,
            """
            CREATE TABLE monitor_state (
                site_id TEXT PRIMARY KEY REFERENCES sites(site_id) ON DELETE CASCADE,
                stable_status TEXT NOT NULL,
                pending_status TEXT,
                pending_count INTEGER NOT NULL CHECK (pending_count >= 0),
                pending_started_at_ms INTEGER,
                open_incident_id TEXT REFERENCES incidents(incident_id),
                last_observed_at_ms INTEGER,
                last_observed_status TEXT,
                updated_at_ms INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE monitor_runtime (
                site_id TEXT PRIMARY KEY REFERENCES sites(site_id) ON DELETE CASCADE,
                boot_id TEXT NOT NULL,
                process_id TEXT NOT NULL,
                started_at_ms INTEGER NOT NULL,
                last_heartbeat_ms INTEGER NOT NULL,
                last_round_at_ms INTEGER
            )
            """,
            """
            CREATE TABLE monitoring_gaps (
                gap_id TEXT PRIMARY KEY,
                site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER,
                reason TEXT NOT NULL CHECK (
                    reason IN (
                        'host_reboot', 'process_restart', 'stale_heartbeat',
                        'clock_uncertain'
                    )
                ),
                previous_boot_id TEXT,
                current_boot_id TEXT NOT NULL,
                previous_process_id TEXT,
                current_process_id TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                CHECK (end_ms IS NULL OR end_ms >= start_ms)
            )
            """,
            """
            CREATE UNIQUE INDEX monitoring_gaps_one_open
            ON monitoring_gaps(site_id) WHERE end_ms IS NULL
            """,
            """
            CREATE INDEX monitoring_gaps_site_start
            ON monitoring_gaps(site_id, start_ms DESC)
            """,
        ),
    ),
    (
        2,
        (
            """
            CREATE TABLE latency_hourly (
                site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
                target_id TEXT NOT NULL REFERENCES probe_targets(target_id) ON DELETE CASCADE,
                hour_start_ms INTEGER NOT NULL,
                success_count INTEGER NOT NULL CHECK (success_count >= 0),
                failure_count INTEGER NOT NULL CHECK (failure_count >= 0),
                latency_count INTEGER NOT NULL CHECK (latency_count >= 0),
                latency_sum_ms REAL NOT NULL CHECK (latency_sum_ms >= 0),
                latency_min_ms REAL,
                latency_max_ms REAL,
                PRIMARY KEY (site_id, target_id, hour_start_ms),
                CHECK (latency_min_ms IS NULL OR latency_min_ms >= 0),
                CHECK (latency_max_ms IS NULL OR latency_max_ms >= 0)
            )
            """,
            """
            CREATE INDEX latency_hourly_site_time
            ON latency_hourly(site_id, hour_start_ms DESC)
            """,
        ),
    ),
    (
        3,
        (
            """
            ALTER TABLE incidents ADD COLUMN failure_summary_json TEXT
            """,
        ),
    ),
    (
        4,
        (
            """
            CREATE TABLE service_monitors (
                monitor_id TEXT PRIMARY KEY,
                site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                label TEXT NOT NULL,
                endpoint TEXT,
                interval_seconds REAL NOT NULL CHECK (interval_seconds > 0),
                timeout_seconds REAL NOT NULL CHECK (timeout_seconds > 0),
                display_mode TEXT NOT NULL CHECK (
                    display_mode IN ('hidden', 'compact', 'detailed')
                ),
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE service_monitor_state (
                monitor_id TEXT PRIMARY KEY REFERENCES service_monitors(monitor_id)
                    ON DELETE CASCADE,
                stable_status TEXT NOT NULL CHECK (
                    stable_status IN ('up', 'degraded', 'down', 'unknown')
                ),
                stable_since_ms INTEGER NOT NULL,
                pending_status TEXT CHECK (
                    pending_status IS NULL OR
                    pending_status IN ('up', 'degraded', 'down', 'unknown')
                ),
                pending_count INTEGER NOT NULL CHECK (pending_count >= 0),
                pending_started_ms INTEGER,
                last_checked_ms INTEGER,
                last_latency_ms REAL,
                last_error_class TEXT,
                updated_at_ms INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE service_status_intervals (
                interval_id TEXT PRIMARY KEY,
                monitor_id TEXT NOT NULL REFERENCES service_monitors(monitor_id)
                    ON DELETE CASCADE,
                status TEXT NOT NULL CHECK (
                    status IN ('up', 'degraded', 'down', 'unknown')
                ),
                start_ms INTEGER NOT NULL,
                end_ms INTEGER,
                CHECK (end_ms IS NULL OR end_ms >= start_ms)
            )
            """,
            """
            CREATE UNIQUE INDEX service_intervals_one_open
            ON service_status_intervals(monitor_id) WHERE end_ms IS NULL
            """,
            """
            CREATE INDEX service_intervals_monitor_start
            ON service_status_intervals(monitor_id, start_ms DESC)
            """,
            """
            CREATE TABLE service_incidents (
                incident_id TEXT PRIMARY KEY,
                monitor_id TEXT NOT NULL REFERENCES service_monitors(monitor_id)
                    ON DELETE CASCADE,
                status TEXT NOT NULL CHECK (status IN ('degraded', 'down')),
                lifecycle TEXT NOT NULL CHECK (
                    lifecycle IN ('open', 'closed', 'interrupted')
                ),
                observed_start_ms INTEGER NOT NULL,
                confirmed_start_ms INTEGER NOT NULL,
                observed_end_ms INTEGER,
                confirmed_end_ms INTEGER,
                end_reason TEXT,
                error_class TEXT
            )
            """,
            """
            CREATE UNIQUE INDEX service_incidents_one_open
            ON service_incidents(monitor_id) WHERE lifecycle = 'open'
            """,
            """
            CREATE INDEX service_incidents_monitor_start
            ON service_incidents(monitor_id, observed_start_ms DESC)
            """,
        ),
    ),
    (
        5,
        (
            """
            CREATE TABLE service_ping_samples (
                monitor_id TEXT NOT NULL REFERENCES service_monitors(monitor_id) ON DELETE CASCADE,
                observed_at_ms INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('up', 'degraded', 'down', 'unknown')),
                latency_ms REAL CHECK (latency_ms IS NULL OR latency_ms >= 0),
                PRIMARY KEY (monitor_id, observed_at_ms)
            ) WITHOUT ROWID
            """,
        ),
    ),
)


def migrate(connection: sqlite3.Connection) -> None:
    """Apply every unapplied migration transactionally and idempotently."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()

    for version, statements in MIGRATIONS:
        try:
            connection.execute("BEGIN IMMEDIATE")
            already_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (version,),
            ).fetchone()
            if already_applied is not None:
                connection.commit()
                continue

            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (version,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
