"""Bounded concurrent execution and evidence aggregation for one probe round."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict, Mapping, Optional, Sequence, Tuple

from home_internet_monitor.domain.classifier import classify_round
from home_internet_monitor.domain.models import ComponentStatus, ProbeOutcome, RoundEvidence

from .models import CompletedRound, ProbeResult, ProbeTarget, TargetKind
from .probes import ProbeAdapter, default_adapters
from .routing import LinuxRouteInspector


class RoundExecutor:
    def __init__(
        self,
        targets: Sequence[ProbeTarget],
        route_inspector: LinuxRouteInspector,
        adapters: Optional[Mapping[TargetKind, ProbeAdapter]] = None,
        max_concurrency: int = 4,
        round_timeout_seconds: float = 8.0,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._targets = tuple(target for target in targets if target.enabled)
        self._routes = route_inspector
        self._adapters = dict(adapters or default_adapters())
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._round_timeout = round_timeout_seconds

    async def execute(self, observed_at: Optional[datetime] = None) -> CompletedRound:
        timestamp = observed_at or datetime.now(timezone.utc)
        tasks = {
            asyncio.create_task(self._run_target(target)): target
            for target in self._targets
        }
        done, pending = await asyncio.wait(tasks, timeout=self._round_timeout)
        results: Dict[str, ProbeResult] = {}
        for task in done:
            target = tasks[task]
            try:
                results[target.target_id] = task.result()
            except Exception:
                results[target.target_id] = _unknown(target, "probe_runtime_error")
        for task in pending:
            target = tasks[task]
            task.cancel()
            results[target.target_id] = _unknown(target, "round_timeout")
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        ordered = tuple(results[target.target_id] for target in self._targets)
        evidence = _aggregate(ordered)
        return CompletedRound(
            observed_at=timestamp,
            evidence=evidence,
            status=classify_round(evidence),
            results=ordered,
        )

    async def _run_target(self, target: ProbeTarget) -> ProbeResult:
        async with self._semaphore:
            resolved = target
            if target.kind is TargetKind.GATEWAY and target.endpoint == "auto":
                decision = await self._routes.default_gateway(target.timeout_seconds)
                if not decision.accepted or not decision.gateway:
                    return _unknown(target, decision.reason or "gateway_lookup_failed")
                resolved = replace(target, endpoint=decision.gateway)

            if target.require_native_route:
                decision = await self._routes.inspect(
                    resolved.endpoint, resolved.timeout_seconds
                )
                if not decision.accepted:
                    return ProbeResult(
                        target_id=target.target_id,
                        kind=target.kind,
                        outcome=ProbeOutcome.UNKNOWN,
                        error_class=decision.reason or "route_rejected",
                        metadata={
                            "route_trusted": False,
                            "interface": decision.interface,
                            "gateway": decision.gateway,
                        },
                    )
            adapter = self._adapters.get(target.kind)
            if adapter is None:
                return _unknown(target, "adapter_unavailable")
            try:
                result = await asyncio.wait_for(
                    adapter.probe(resolved), timeout=resolved.timeout_seconds + 1.0
                )
            except asyncio.TimeoutError:
                return _unknown(target, "adapter_timeout")
            return replace(result, target_id=target.target_id, kind=target.kind)


def _aggregate(results: Sequence[ProbeResult]) -> RoundEvidence:
    by_kind = {
        kind: tuple(result for result in results if result.kind is kind)
        for kind in TargetKind
    }
    route_trusted = all(
        result.metadata.get("route_trusted", True) is not False for result in results
    )
    return RoundEvidence(
        gateway=_component(by_kind[TargetKind.GATEWAY], minimum_failures=1),
        external_ip=_component(by_kind[TargetKind.EXTERNAL_IP], minimum_failures=2),
        dns=_component(by_kind[TargetKind.DNS], minimum_failures=1),
        https=_component(by_kind[TargetKind.HTTPS], minimum_failures=1),
        route_trusted=route_trusted,
        timing_trusted=True,
    )


def _component(
    results: Sequence[ProbeResult], minimum_failures: int
) -> ComponentStatus:
    if not results:
        return ComponentStatus.NOT_APPLICABLE
    if any(result.outcome is ProbeOutcome.SUCCESS for result in results):
        return ComponentStatus.REACHABLE
    failures = sum(result.outcome is ProbeOutcome.FAILURE for result in results)
    if failures == len(results) and failures >= minimum_failures:
        return ComponentStatus.UNREACHABLE
    return ComponentStatus.UNKNOWN


def _unknown(target: ProbeTarget, error_class: str) -> ProbeResult:
    return ProbeResult(
        target_id=target.target_id,
        kind=target.kind,
        outcome=ProbeOutcome.UNKNOWN,
        error_class=error_class,
    )
