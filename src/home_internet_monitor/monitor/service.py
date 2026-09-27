"""Application service joining probes, domain rules and SQLite persistence."""

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional

from home_internet_monitor.domain.state_machine import apply_observation
from home_internet_monitor.domain.classifier import classify_round
from home_internet_monitor.storage.repository import (
    MonitoringRepository,
    ProbeSampleRecord,
    ProbeTargetRecord,
    RoundRecord,
)

from .config import AppConfig
from .models import CompletedRound
from .rounds import RoundExecutor

_ROUND_NAMESPACE = uuid.UUID("12077abc-62b6-48bd-9f7a-a4adcc33dfee")


@dataclass(frozen=True)
class StoredRound:
    round_id: str
    completed: CompletedRound
    inserted: bool


class MonitorService:
    def __init__(
        self,
        config: AppConfig,
        repository: MonitoringRepository,
        executor: RoundExecutor,
        boot_id: Optional[str] = None,
        process_id: Optional[str] = None,
    ) -> None:
        if (boot_id is None) != (process_id is None):
            raise ValueError("boot_id and process_id must be supplied together")
        self._config = config
        self._repository = repository
        self._executor = executor
        self._boot_id = boot_id
        self._process_id = process_id

    def prepare(self, now: Optional[datetime] = None) -> None:
        timestamp = now or datetime.now(timezone.utc)
        site = self._config.site
        self._repository.ensure_site(
            site.site_id, site.display_name, site.timezone, timestamp
        )
        for target in self._config.probes:
            self._repository.upsert_target(
                site.site_id,
                ProbeTargetRecord(
                    target_id=target.target_id,
                    kind=target.kind.value,
                    label=target.target_id.replace("_", " ").title(),
                    endpoint=target.endpoint,
                    enabled=target.enabled,
                    timeout_ms=round(target.timeout_seconds * 1000),
                ),
                timestamp,
            )
        if self._boot_id is not None and self._process_id is not None:
            self._repository.start_runtime(
                site.site_id,
                self._boot_id,
                self._process_id,
                timestamp,
            )

    async def run_round(
        self,
        observed_at: Optional[datetime] = None,
        *,
        timing_trusted: bool = True,
    ) -> StoredRound:
        completed = await self._executor.execute(observed_at)
        site_id = self._config.site.site_id
        prior_state = self._repository.load_state(site_id)
        if not timing_trusted or (
            prior_state.last_observed_at is not None
            and completed.observed_at < prior_state.last_observed_at
        ):
            evidence = replace(completed.evidence, timing_trusted=False)
            completed = replace(
                completed,
                evidence=evidence,
                status=classify_round(evidence),
            )
            if self._boot_id is not None and self._process_id is not None:
                self._repository.open_clock_gap(
                    site_id,
                    self._boot_id,
                    self._process_id,
                    completed.observed_at,
                )
                prior_state = self._repository.load_state(site_id)
            if (
                prior_state.last_observed_at is not None
                and completed.observed_at < prior_state.last_observed_at
            ):
                return StoredRound(
                    _round_id(site_id, completed.observed_at),
                    completed,
                    False,
                )
        transition = apply_observation(
            prior_state,
            completed.status,
            completed.observed_at,
            failure_threshold=self._config.monitor.failure_threshold,
            recovery_threshold=self._config.monitor.recovery_threshold,
        )
        round_id = _round_id(site_id, completed.observed_at)
        inserted = self._repository.record_round(
            RoundRecord(
                round_id=round_id,
                site_id=site_id,
                observed_at=completed.observed_at,
                status=completed.status,
                evidence=completed.evidence,
                boot_id=self._boot_id,
                process_id=self._process_id,
            ),
            tuple(
                ProbeSampleRecord(
                    target_id=result.target_id,
                    outcome=result.outcome,
                    latency_ms=result.latency_ms,
                    error_class=result.error_class,
                    metadata=result.metadata,
                )
                for result in completed.results
            ),
            transition,
        )
        return StoredRound(round_id, completed, inserted)


def _round_id(site_id: str, observed_at: datetime) -> str:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("round timestamp must be timezone-aware")
    normalized = observed_at.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return str(uuid.uuid5(_ROUND_NAMESPACE, f"{site_id}:{normalized}"))
