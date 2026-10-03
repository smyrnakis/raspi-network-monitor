import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from home_internet_monitor.api.queries import MonitoringQueries
from home_internet_monitor.storage import connect_database, connect_readonly, migrate


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def epoch_ms(value):
    return int(value.timestamp() * 1000)


class QueryTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.path = Path(handle.name)
        connection = connect_database(self.path)
        migrate(connection)
        connection.execute(
            """
            INSERT INTO sites(site_id, display_name, timezone, created_at_ms, updated_at_ms)
            VALUES ('home', 'Home', 'UTC', ?, ?)
            """,
            (epoch_ms(BASE), epoch_ms(BASE)),
        )
        connection.commit()
        connection.close()
        self.connection = connect_readonly(self.path)
        self.queries = MonitoringQueries(self.connection)

    def tearDown(self):
        self.connection.close()
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                candidate.unlink()

    def write(self, statement, parameters=()):
        connection = connect_database(self.path)
        connection.execute(statement, parameters)
        connection.commit()
        connection.close()

    def test_readonly_connection_rejects_writes(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.connection.execute("DELETE FROM sites")

    def test_availability_clips_intervals_and_excludes_unknown(self):
        end = BASE + timedelta(hours=1)
        connection = connect_database(self.path)
        rows = (
            ("i1", "online", BASE, BASE + timedelta(minutes=30)),
            (
                "i2",
                "internet_down",
                BASE + timedelta(minutes=30),
                BASE + timedelta(minutes=45),
            ),
            ("i3", "monitoring_unknown", BASE + timedelta(minutes=45), None),
        )
        for interval_id, status, start, interval_end in rows:
            connection.execute(
                """
                INSERT INTO status_intervals(
                    interval_id, site_id, status, start_ms, confirmed_at_ms, end_ms
                ) VALUES (?, 'home', ?, ?, ?, ?)
                """,
                (
                    interval_id,
                    status,
                    epoch_ms(start),
                    epoch_ms(start),
                    epoch_ms(interval_end) if interval_end else None,
                ),
            )
        connection.commit()
        connection.close()

        result = self.queries.availability("home", BASE, end)

        self.assertEqual(2700, result["classified_seconds"])
        self.assertEqual(900, result["unknown_seconds"])
        self.assertAlmostEqual(2 / 3, result["availability"])
        self.assertEqual(0.75, result["coverage"])
        self.assertEqual(0, result["incident_count"])
        self.assertIsNone(result["mtbf_seconds"])

    def test_mtbf_counts_incident_episode_once_and_excludes_unknown_time(self):
        end = BASE + timedelta(hours=2)
        connection = connect_database(self.path)
        for interval_id, status, start, interval_end in (
            ("online", "online", BASE, BASE + timedelta(minutes=50)),
            (
                "down",
                "internet_down",
                BASE + timedelta(minutes=50),
                BASE + timedelta(hours=1),
            ),
            ("unknown", "monitoring_unknown", BASE + timedelta(hours=1), end),
        ):
            connection.execute(
                """
                INSERT INTO status_intervals(
                    interval_id, site_id, status, start_ms, confirmed_at_ms, end_ms
                ) VALUES (?, 'home', ?, ?, ?, ?)
                """,
                (
                    interval_id,
                    status,
                    epoch_ms(start),
                    epoch_ms(start),
                    epoch_ms(interval_end),
                ),
            )
        first_start = epoch_ms(BASE + timedelta(minutes=50))
        boundary = epoch_ms(BASE + timedelta(minutes=55))
        final_end = epoch_ms(BASE + timedelta(hours=1))
        connection.execute(
            """
            INSERT INTO incidents(
                incident_id, site_id, status, lifecycle,
                observed_start_ms, confirmed_start_ms,
                observed_end_ms, confirmed_end_ms, end_reason,
                previous_incident_id, notes, created_at_ms, updated_at_ms
            ) VALUES
                ('mtbf-phase-1', 'home', 'partial_connectivity', 'closed',
                 ?, ?, ?, ?, 'category_transition', NULL, '', ?, ?),
                ('mtbf-phase-2', 'home', 'internet_down', 'closed',
                 ?, ?, ?, ?, 'recovered', 'mtbf-phase-1', '', ?, ?)
            """,
            (
                first_start,
                first_start + 20_000,
                boundary,
                boundary + 20_000,
                first_start,
                boundary,
                boundary,
                boundary + 20_000,
                final_end,
                final_end + 20_000,
                boundary,
                final_end,
            ),
        )
        connection.commit()
        connection.close()

        result = self.queries.availability("home", BASE, end)

        self.assertEqual(1, result["incident_count"])
        self.assertEqual(3000, result["mtbf_seconds"])
        self.assertEqual(3600, result["classified_seconds"])
        self.assertEqual(3600, result["unknown_seconds"])

    def test_mtbf_excludes_incidents_below_configured_duration(self):
        end = BASE + timedelta(hours=1)
        connection = connect_database(self.path)
        connection.execute(
            """
            INSERT INTO status_intervals(
                interval_id, site_id, status, start_ms, confirmed_at_ms, end_ms
            ) VALUES ('online', 'home', 'online', ?, ?, ?)
            """,
            (epoch_ms(BASE), epoch_ms(BASE), epoch_ms(end)),
        )
        for incident_id, start, duration_seconds in (
            ("brief", BASE + timedelta(minutes=10), 30),
            ("qualifying", BASE + timedelta(minutes=20), 120),
        ):
            finish = start + timedelta(seconds=duration_seconds)
            connection.execute(
                """
                INSERT INTO incidents(
                    incident_id, site_id, status, lifecycle,
                    observed_start_ms, confirmed_start_ms,
                    observed_end_ms, confirmed_end_ms, end_reason,
                    previous_incident_id, notes, created_at_ms, updated_at_ms
                ) VALUES (?, 'home', 'internet_down', 'closed',
                          ?, ?, ?, ?, 'recovered', NULL, '', ?, ?)
                """,
                (
                    incident_id,
                    epoch_ms(start),
                    epoch_ms(start),
                    epoch_ms(finish),
                    epoch_ms(finish),
                    epoch_ms(start),
                    epoch_ms(finish),
                ),
            )
        connection.commit()
        connection.close()

        result = self.queries.availability(
            "home", BASE, end, minimum_incident_duration_seconds=60
        )

        self.assertEqual(1, result["incident_count"])
        self.assertEqual(3600, result["mtbf_seconds"])
        self.assertEqual(60, result["mtbf_minimum_incident_seconds"])

    def test_empty_window_returns_null_availability(self):
        result = self.queries.availability(
            "home", BASE, BASE + timedelta(hours=1)
        )
        self.assertIsNone(result["availability"])
        self.assertEqual(0, result["coverage"])
        self.assertEqual(3600, result["unknown_seconds"])
        self.assertEqual(0, result["incident_count"])
        self.assertIsNone(result["mtbf_seconds"])

    def test_timeline_clips_segments_to_selected_window(self):
        connection = connect_database(self.path)
        connection.execute(
            """
            INSERT INTO status_intervals(
                interval_id, site_id, status, start_ms, confirmed_at_ms, end_ms
            ) VALUES ('i1', 'home', 'online', ?, ?, ?)
            """,
            (
                epoch_ms(BASE),
                epoch_ms(BASE),
                epoch_ms(BASE + timedelta(hours=2)),
            ),
        )
        incident_start = epoch_ms(BASE + timedelta(minutes=40))
        incident_end = epoch_ms(BASE + timedelta(minutes=45))
        connection.execute(
            """
            INSERT INTO incidents(
                incident_id, site_id, status, lifecycle,
                observed_start_ms, confirmed_start_ms,
                observed_end_ms, confirmed_end_ms, end_reason,
                previous_incident_id, notes, created_at_ms, updated_at_ms
            ) VALUES ('timeline-short', 'home', 'internet_down', 'closed',
                      ?, ?, ?, ?, 'recovered', NULL, '', ?, ?)
            """,
            (
                incident_start,
                incident_start,
                incident_end,
                incident_end,
                incident_start,
                incident_end,
            ),
        )
        connection.commit()
        connection.close()

        result = self.queries.timeline(
            "home",
            BASE + timedelta(minutes=30),
            BASE + timedelta(minutes=90),
        )

        self.assertEqual(1, len(result["segments"]))
        self.assertEqual("online", result["segments"][0]["status"])
        self.assertEqual(3600, result["segments"][0]["duration_seconds"])
        self.assertEqual("2026-01-01T00:30:00Z", result["segments"][0]["start"])
        self.assertEqual("2026-01-01T01:30:00Z", result["segments"][0]["end"])
        self.assertEqual(1, len(result["incidents"]))
        self.assertEqual("timeline-short", result["incidents"][0]["incident_id"])
        self.assertEqual("internet_down", result["incidents"][0]["status"])
        self.assertEqual(["internet_down"], result["incidents"][0]["categories"])

    def test_latency_groups_ping_samples_into_requested_buckets(self):
        connection = connect_database(self.path)
        connection.execute(
            """
            INSERT INTO probe_targets(
                target_id, site_id, kind, label, endpoint, enabled,
                timeout_ms, created_at_ms, updated_at_ms
            ) VALUES ('ip1', 'home', 'external_ip', 'External one', '1.1.1.1',
                      1, 3000, ?, ?)
            """,
            (epoch_ms(BASE), epoch_ms(BASE)),
        )
        for index, (minutes, latency, outcome) in enumerate(
            ((1, 10.0, "success"), (4, 20.0, "success"), (6, 5000.0, "failure"))
        ):
            round_id = f"r{index}"
            connection.execute(
                """
                INSERT INTO probe_rounds(
                    round_id, site_id, observed_at_ms, status,
                    gateway_status, external_ip_status, dns_status, https_status,
                    timing_trusted, route_trusted
                ) VALUES (?, 'home', ?, 'online', 'reachable', 'reachable',
                          'reachable', 'reachable', 1, 1)
                """,
                (round_id, epoch_ms(BASE + timedelta(minutes=minutes))),
            )
            connection.execute(
                """
                INSERT INTO probe_samples(
                    round_id, target_id, outcome, latency_ms
                ) VALUES (?, 'ip1', ?, ?)
                """,
                (round_id, outcome, latency),
            )
        incident_start = epoch_ms(BASE + timedelta(minutes=2))
        incident_end = epoch_ms(BASE + timedelta(minutes=7))
        connection.execute(
            """
            INSERT INTO incidents(
                incident_id, site_id, status, lifecycle,
                observed_start_ms, confirmed_start_ms,
                observed_end_ms, confirmed_end_ms, end_reason,
                previous_incident_id, notes, created_at_ms, updated_at_ms
            ) VALUES ('latency-incident', 'home', 'internet_down', 'closed',
                      ?, ?, ?, ?, 'recovered', NULL, '', ?, ?)
            """,
            (
                incident_start,
                incident_start,
                incident_end,
                incident_end,
                incident_start,
                incident_end,
            ),
        )
        connection.commit()
        connection.close()

        result = self.queries.latency(
            "home", BASE, BASE + timedelta(minutes=10), 300
        )

        self.assertEqual(1, len(result["series"]))
        points = result["series"][0]["points"]
        self.assertEqual(2, len(points))
        self.assertEqual(15.0, points[0]["avg_ms"])
        self.assertEqual(2, points[0]["success_count"])
        self.assertEqual(1, points[1]["failure_count"])
        self.assertIsNone(points[1]["avg_ms"])
        self.assertEqual("latency-incident", result["incidents"][0]["incident_id"])
        self.assertEqual("internet_down", result["incidents"][0]["status"])

    def test_combined_history_filters_paginates_and_loads_one_item(self):
        connection = connect_database(self.path)
        for incident_id, status, start_hour in (
            ("incident-1", "internet_down", 1),
            ("incident-2", "dns_failure", 3),
        ):
            start = epoch_ms(BASE + timedelta(hours=start_hour))
            end = epoch_ms(BASE + timedelta(hours=start_hour, minutes=10))
            connection.execute(
                """
                INSERT INTO incidents(
                    incident_id, site_id, status, lifecycle,
                    observed_start_ms, confirmed_start_ms,
                    observed_end_ms, confirmed_end_ms, end_reason,
                    previous_incident_id, notes, created_at_ms, updated_at_ms
                ) VALUES (?, 'home', ?, 'closed', ?, ?, ?, ?, 'recovered',
                          NULL, '', ?, ?)
                """,
                (incident_id, status, start, start, end, end, start, end),
            )
        gap_start = epoch_ms(BASE + timedelta(hours=2))
        gap_end = epoch_ms(BASE + timedelta(hours=2, minutes=5))
        connection.execute(
            """
            INSERT INTO monitoring_gaps(
                gap_id, site_id, start_ms, end_ms, reason,
                current_boot_id, current_process_id, created_at_ms
            ) VALUES ('gap-1', 'home', ?, ?, 'process_restart',
                      'boot', 'process', ?)
            """,
            (gap_start, gap_end, gap_start),
        )
        connection.commit()
        connection.close()

        page = self.queries.history("home", limit=2)
        gaps = self.queries.history("home", event_type="gap")
        filtered = self.queries.history_for_export(
            "home", category="internet_down"
        )
        item = self.queries.history_item("home", "incident", "incident-1")

        self.assertEqual(3, page["total"])
        self.assertEqual(["incident-2", "gap-1"], [row["event_id"] for row in page["items"]])
        self.assertEqual(1, gaps["total"])
        self.assertEqual("process_restart", gaps["items"][0]["category"])
        self.assertEqual(["incident-1"], [row["event_id"] for row in filtered])
        self.assertEqual("closed", item["state"])
        self.assertEqual(600, item["duration_seconds"])

    def test_incident_minimum_duration_filters_before_pagination(self):
        connection = connect_database(self.path)
        for incident_id, start_minute, duration_seconds in (
            ("long", 1, 120),
            ("short", 4, 30),
        ):
            start = epoch_ms(BASE + timedelta(minutes=start_minute))
            end = start + duration_seconds * 1000
            connection.execute(
                """
                INSERT INTO incidents(
                    incident_id, site_id, status, lifecycle,
                    observed_start_ms, confirmed_start_ms,
                    observed_end_ms, confirmed_end_ms, end_reason,
                    previous_incident_id, notes, created_at_ms, updated_at_ms
                ) VALUES (?, 'home', 'internet_down', 'closed', ?, ?, ?, ?,
                          'recovered', NULL, '', ?, ?)
                """,
                (incident_id, start, start, end, end, start, end),
            )
        connection.commit()
        connection.close()

        visible = self.queries.incidents(
            "home", limit=1, minimum_duration_seconds=60
        )

        self.assertEqual(["long"], [incident["incident_id"] for incident in visible])

    def test_gap_minimum_duration_filters_before_pagination(self):
        connection = connect_database(self.path)
        for gap_id, start_minute, duration_seconds in (
            ("long", 1, 120),
            ("short", 4, 30),
        ):
            start = epoch_ms(BASE + timedelta(minutes=start_minute))
            end = start + duration_seconds * 1000
            connection.execute(
                """
                INSERT INTO monitoring_gaps(
                    gap_id, site_id, start_ms, end_ms, reason,
                    current_boot_id, current_process_id, created_at_ms
                ) VALUES (?, 'home', ?, ?, 'process_restart',
                          'boot', 'process', ?)
                """,
                (gap_id, start, end, start),
            )
        connection.commit()
        connection.close()

        visible = self.queries.gaps(
            "home", limit=1, minimum_duration_seconds=60
        )

        self.assertEqual(["long"], [gap["gap_id"] for gap in visible])

    def test_category_transition_is_one_incident_with_diagnostic_phases(self):
        connection = connect_database(self.path)
        first_start = epoch_ms(BASE + timedelta(minutes=1))
        boundary = epoch_ms(BASE + timedelta(minutes=1, seconds=30))
        final_end = epoch_ms(BASE + timedelta(minutes=2))
        connection.execute(
            """
            INSERT INTO incidents(
                incident_id, site_id, status, lifecycle,
                observed_start_ms, confirmed_start_ms,
                observed_end_ms, confirmed_end_ms, end_reason,
                previous_incident_id, notes, created_at_ms, updated_at_ms
            ) VALUES
                ('phase-1', 'home', 'partial_connectivity', 'closed',
                 ?, ?, ?, ?, 'category_transition', NULL, '', ?, ?),
                ('phase-2', 'home', 'internet_down', 'closed',
                 ?, ?, ?, ?, 'recovered', 'phase-1', '', ?, ?)
            """,
            (
                first_start,
                first_start + 20_000,
                boundary,
                boundary + 20_000,
                first_start,
                boundary,
                boundary,
                boundary + 20_000,
                final_end,
                final_end + 20_000,
                boundary,
                final_end,
            ),
        )
        connection.commit()
        connection.close()

        incidents = self.queries.incidents(
            "home", minimum_duration_seconds=60
        )
        history = self.queries.history("home")
        filtered = self.queries.history("home", category="partial_connectivity")
        selected = self.queries.history_item("home", "incident", "phase-2")
        latency = self.queries.latency(
            "home", BASE, BASE + timedelta(minutes=10), 300
        )

        self.assertEqual(1, len(incidents))
        self.assertEqual("phase-1", incidents[0]["incident_id"])
        self.assertEqual(2, incidents[0]["phase_count"])
        self.assertEqual(
            ["partial_connectivity", "internet_down"],
            incidents[0]["categories"],
        )
        self.assertEqual(1, history["total"])
        self.assertEqual(1, filtered["total"])
        self.assertEqual("phase-1", selected["event_id"])
        self.assertEqual(
            ["phase-1", "phase-2"],
            [phase["incident_id"] for phase in selected["phases"]],
        )
        self.assertEqual(60, selected["duration_seconds"])
        self.assertEqual(["phase-1"], [item["incident_id"] for item in latency["incidents"]])

    def test_history_exposes_saved_failed_tests_with_target_labels(self):
        connection = connect_database(self.path)
        connection.execute(
            """
            INSERT INTO probe_targets(
                target_id, site_id, kind, label, endpoint, enabled,
                timeout_ms, created_at_ms, updated_at_ms
            ) VALUES ('dns-primary', 'home', 'dns', 'DNS primary',
                      'example.com', 1, 3000, ?, ?)
            """,
            (epoch_ms(BASE), epoch_ms(BASE)),
        )
        connection.execute(
            """
            INSERT INTO incidents(
                incident_id, site_id, status, lifecycle,
                observed_start_ms, confirmed_start_ms,
                observed_end_ms, confirmed_end_ms, end_reason,
                previous_incident_id, notes, created_at_ms, updated_at_ms,
                failure_summary_json
            ) VALUES ('incident-failure', 'home', 'partial_connectivity',
                      'closed', ?, ?, ?, ?, 'recovered', NULL, '', ?, ?, ?)
            """,
            (
                epoch_ms(BASE),
                epoch_ms(BASE),
                epoch_ms(BASE + timedelta(minutes=2)),
                epoch_ms(BASE + timedelta(minutes=2)),
                epoch_ms(BASE),
                epoch_ms(BASE + timedelta(minutes=2)),
                '[{"target_id":"dns-primary","outcome":"failure",'
                '"error_class":"timeout"}]',
            ),
        )
        connection.commit()
        connection.close()

        item = self.queries.history_item("home", "incident", "incident-failure")

        self.assertEqual(
            [
                {
                    "target_id": "dns-primary",
                    "outcome": "failure",
                    "error_class": "timeout",
                    "label": "DNS primary",
                }
            ],
            item["failed_tests"],
        )

    def test_health_uses_runtime_heartbeat(self):
        self.write(
            """
            INSERT INTO monitor_runtime(
                site_id, boot_id, process_id, started_at_ms,
                last_heartbeat_ms, last_round_at_ms
            ) VALUES ('home', 'boot', 'process', ?, ?, NULL)
            """,
            (epoch_ms(BASE), epoch_ms(BASE)),
        )
        result = self.queries.health("home", BASE + timedelta(seconds=20), 30)
        self.assertEqual("healthy", result["status"])
        self.assertEqual(20, result["heartbeat_age_seconds"])

    def test_status_before_first_round_is_unknown(self):
        result = self.queries.current_status("home")
        self.assertEqual("monitoring_unknown", result["stable_status"])
        self.assertIsNone(result["latest_round"])

    def test_pagination_is_bounded(self):
        with self.assertRaises(ValueError):
            self.queries.incidents("home", limit=501)
        with self.assertRaises(ValueError):
            self.queries.gaps("home", offset=-1)


if __name__ == "__main__":
    unittest.main()
