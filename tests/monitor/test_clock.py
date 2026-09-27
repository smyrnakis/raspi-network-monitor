import unittest
from datetime import datetime, timedelta, timezone

from home_internet_monitor.monitor.clock import ClockTrustMonitor


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ClockTrustTests(unittest.TestCase):
    def test_unsynchronized_clock_is_untrusted(self):
        clock = ClockTrustMonitor(sync_probe=lambda: False)
        assessment = clock.assess(BASE, 100.0)
        self.assertFalse(assessment.trusted)
        self.assertEqual("clock_not_synchronized", assessment.reason)

    def test_matching_wall_and_monotonic_deltas_are_trusted(self):
        clock = ClockTrustMonitor(sync_probe=lambda: True)
        self.assertTrue(clock.assess(BASE, 100.0).trusted)
        self.assertTrue(clock.assess(BASE + timedelta(seconds=10), 110.0).trusted)

    def test_wall_clock_jump_is_untrusted_for_that_assessment(self):
        clock = ClockTrustMonitor(sync_probe=lambda: True)
        clock.assess(BASE, 100.0)
        assessment = clock.assess(BASE + timedelta(seconds=30), 110.0)
        self.assertFalse(assessment.trusted)
        self.assertEqual("clock_jump", assessment.reason)

    def test_unknown_sync_state_does_not_create_false_alarm(self):
        clock = ClockTrustMonitor(sync_probe=lambda: None)
        self.assertTrue(clock.assess(BASE, 100.0).trusted)


if __name__ == "__main__":
    unittest.main()
