"""Runnable monitor worker and Linux runtime identity."""

import argparse
import asyncio
import logging
import os
import signal
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

from home_internet_monitor.storage import MonitoringRepository, connect_database, migrate

from .config import AppConfig, load_config
from .clock import ClockTrustMonitor
from .rounds import RoundExecutor
from .routing import LinuxRouteInspector
from .scheduler import NonOverlappingScheduler
from .service import MonitorService
from .settings import RuntimeSettingsStore

LOGGER = logging.getLogger(__name__)
_RETENTION_INTERVAL = timedelta(hours=24)


def read_boot_id(path: Path = Path("/proc/sys/kernel/random/boot_id")) -> str:
    value = path.read_text(encoding="ascii").strip()
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise RuntimeError("Linux boot ID is invalid") from error


def new_process_id() -> str:
    return f"{os.getpid()}-{uuid.uuid4()}"


class SettingsRestartRequested(RuntimeError):
    pass


async def run_worker(
    config: AppConfig,
    *,
    once: bool = False,
    settings_store: Optional[RuntimeSettingsStore] = None,
) -> None:
    config.storage.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(config.storage.database_path)
    try:
        migrate(connection)
        repository = MonitoringRepository(connection)
        routes = LinuxRouteInspector(
            forbidden_interface_prefixes=config.route.forbidden_interface_prefixes,
            required_interface=config.route.required_interface,
        )
        executor = RoundExecutor(
            config.probes,
            routes,
            max_concurrency=config.monitor.max_concurrency,
            round_timeout_seconds=config.monitor.round_timeout_seconds,
        )
        service = MonitorService(
            config,
            repository,
            executor,
            boot_id=read_boot_id(),
            process_id=new_process_id(),
        )
        prepared_at = datetime.now(timezone.utc)
        service.prepare(prepared_at)
        _apply_retention(repository, config, prepared_at)
        next_retention_at = prepared_at + _RETENTION_INTERVAL
        clock = ClockTrustMonitor()
        last_clock_reason = None

        async def execute_round() -> object:
            nonlocal next_retention_at, last_clock_reason
            assessment = clock.assess()
            if assessment.reason != last_clock_reason and not assessment.trusted:
                LOGGER.warning("clock is untrusted reason=%s", assessment.reason)
            elif last_clock_reason is not None and assessment.trusted:
                LOGGER.info("clock trust restored")
            last_clock_reason = assessment.reason
            stored = await service.run_round(timing_trusted=assessment.trusted)
            LOGGER.info(
                "round status=%s samples=%d inserted=%s",
                stored.completed.status.value,
                len(stored.completed.results),
                stored.inserted,
            )
            now = datetime.now(timezone.utc)
            if now >= next_retention_at:
                _apply_retention(repository, config, now)
                next_retention_at = now + _RETENTION_INTERVAL
            if settings_store is not None and settings_store.consume_restart_request():
                raise SettingsRestartRequested("validated settings update requested")
            return stored

        if once:
            LOGGER.info("running one monitoring round site=%s", config.site.site_id)
            await execute_round()
            return

        stop = asyncio.Event()
        _install_signal_handlers(stop)
        scheduler = NonOverlappingScheduler(execute_round, config.monitor.interval_seconds)
        LOGGER.info("monitor started site=%s", config.site.site_id)
        await scheduler.run_forever(stop)
        LOGGER.info("monitor stopped")
    finally:
        connection.close()


def main(arguments: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the network monitor worker")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one monitoring round, persist it, and exit",
    )
    parsed = parser.parse_args(arguments)
    logging.basicConfig(
        level=getattr(logging, parsed.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    base_config = load_config(parsed.config)
    settings = RuntimeSettingsStore(base_config)
    config = settings.load_config()
    if settings.consume_restart_request():
        LOGGER.info("applying pending settings update at startup")
    try:
        asyncio.run(run_worker(config, once=parsed.once, settings_store=settings))
    except SettingsRestartRequested:
        LOGGER.info("settings changed; requesting systemd restart")
        return 75
    return 0


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows development only
            signal.signal(
                signum,
                lambda _signum, _frame: loop.call_soon_threadsafe(stop.set),
            )


def _apply_retention(
    repository: MonitoringRepository,
    config: AppConfig,
    now: datetime,
) -> None:
    try:
        result = repository.apply_retention(
            config.site.site_id,
            now,
            raw_samples_days=config.retention.raw_samples_days,
            incidents_days=config.retention.incidents_days,
            latency_aggregates_days=config.retention.latency_aggregates_days,
        )
        LOGGER.info(
            "retention complete deleted_rounds=%d deleted_incidents=%d "
            "aggregated_hours=%d deleted_latency_aggregates=%d",
            result.deleted_rounds,
            result.deleted_incidents,
            result.aggregated_hours,
            result.deleted_latency_aggregates,
        )
    except Exception:
        LOGGER.exception("retention cleanup failed; monitoring will continue")
