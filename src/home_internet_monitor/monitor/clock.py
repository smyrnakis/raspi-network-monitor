"""Wall-clock trust checks for reliable monitoring timestamps."""

import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional


@dataclass(frozen=True)
class ClockAssessment:
    trusted: bool
    reason: Optional[str] = None


class ClockTrustMonitor:
    def __init__(
        self,
        *,
        jump_tolerance_seconds: float = 2.0,
        sync_check_interval_seconds: float = 60.0,
        sync_probe: Callable[[], Optional[bool]] = None,
    ) -> None:
        if jump_tolerance_seconds <= 0 or sync_check_interval_seconds <= 0:
            raise ValueError("clock intervals must be positive")
        self._jump_tolerance = jump_tolerance_seconds
        self._sync_check_interval = sync_check_interval_seconds
        self._sync_probe = sync_probe or system_clock_synchronized
        self._last_wall: Optional[datetime] = None
        self._last_monotonic: Optional[float] = None
        self._last_sync_check: Optional[float] = None
        self._synchronized: Optional[bool] = None

    def assess(
        self,
        wall_time: Optional[datetime] = None,
        monotonic_time: Optional[float] = None,
    ) -> ClockAssessment:
        wall = wall_time or datetime.now(timezone.utc)
        monotonic = monotonic_time if monotonic_time is not None else time.monotonic()
        if wall.tzinfo is None or wall.utcoffset() is None:
            raise ValueError("wall_time must be timezone-aware")
        wall = wall.astimezone(timezone.utc)

        if (
            self._last_sync_check is None
            or monotonic - self._last_sync_check >= self._sync_check_interval
        ):
            self._synchronized = self._sync_probe()
            self._last_sync_check = monotonic

        reason = None
        if self._synchronized is False:
            reason = "clock_not_synchronized"
        if self._last_wall is not None and self._last_monotonic is not None:
            wall_delta = (wall - self._last_wall).total_seconds()
            monotonic_delta = monotonic - self._last_monotonic
            if abs(wall_delta - monotonic_delta) > self._jump_tolerance:
                reason = "clock_jump"

        self._last_wall = wall
        self._last_monotonic = monotonic
        return ClockAssessment(reason is None, reason)


def system_clock_synchronized() -> Optional[bool]:
    """Return timedatectl's NTP state, or None when it cannot be determined."""

    try:
        completed = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip().lower()
    if value == "yes":
        return True
    if value == "no":
        return False
    return None
