"""Dependency-light ICMP, DNS and HTTPS probes."""

import asyncio
import math
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Awaitable, Callable, Dict, Optional, Protocol, Sequence, Tuple

from home_internet_monitor.domain.models import ProbeOutcome

from .commands import CommandResult, run_command
from .models import ProbeResult, ProbeTarget, TargetKind

CommandRunner = Callable[[Sequence[str], float], Awaitable[CommandResult]]
Resolver = Callable[[str], Awaitable[Sequence[str]]]
HttpRequester = Callable[[str, float], Tuple[int, str]]


class ProbeAdapter(Protocol):
    async def probe(self, target: ProbeTarget) -> ProbeResult:
        ...


class IcmpProbe:
    def __init__(self, runner: CommandRunner = run_command) -> None:
        self._runner = runner

    async def probe(self, target: ProbeTarget) -> ProbeResult:
        started = time.perf_counter()
        timeout = max(1, math.ceil(target.timeout_seconds))
        try:
            command = await self._runner(
                ["ping", "-n", "-c", "1", "-W", str(timeout), target.endpoint],
                target.timeout_seconds + 1.0,
            )
        except asyncio.TimeoutError:
            return _failure(target, "timeout", started)
        except FileNotFoundError:
            return _unknown(target, "ping_unavailable")
        except OSError:
            return _unknown(target, "probe_runtime_error")

        latency = _ping_latency_ms(command.stdout)
        if command.returncode == 0:
            return ProbeResult(
                target_id=target.target_id,
                kind=target.kind,
                outcome=ProbeOutcome.SUCCESS,
                latency_ms=latency or _elapsed_ms(started),
            )
        return _failure(target, "unreachable", started)


class DnsProbe:
    def __init__(self, resolver: Optional[Resolver] = None) -> None:
        self._resolver = resolver or _resolve

    async def probe(self, target: ProbeTarget) -> ProbeResult:
        started = time.perf_counter()
        try:
            addresses = await asyncio.wait_for(
                self._resolver(target.endpoint), timeout=target.timeout_seconds
            )
        except asyncio.TimeoutError:
            return _failure(target, "timeout", started)
        except socket.gaierror:
            return _failure(target, "resolution_failed", started)
        except OSError:
            return _unknown(target, "probe_runtime_error")

        if not addresses:
            return _failure(target, "empty_answer", started)
        return ProbeResult(
            target_id=target.target_id,
            kind=target.kind,
            outcome=ProbeOutcome.SUCCESS,
            latency_ms=_elapsed_ms(started),
            metadata={"address_count": len(addresses)},
        )


class HttpsProbe:
    def __init__(self, requester: Optional[HttpRequester] = None) -> None:
        self._requester = requester or _request_https

    async def probe(self, target: ProbeTarget) -> ProbeResult:
        started = time.perf_counter()
        try:
            status, final_url = await asyncio.wait_for(
                asyncio.to_thread(
                    self._requester, target.endpoint, target.timeout_seconds
                ),
                timeout=target.timeout_seconds + 0.5,
            )
        except asyncio.TimeoutError:
            return _failure(target, "timeout", started)
        except urllib.error.HTTPError as error:
            status, final_url = error.code, error.geturl()
        except (urllib.error.URLError, TimeoutError, OSError):
            return _failure(target, "request_failed", started)
        except ValueError:
            return _unknown(target, "invalid_url")

        expected = target.expected_status
        if expected is not None and status != expected:
            return ProbeResult(
                target_id=target.target_id,
                kind=target.kind,
                outcome=ProbeOutcome.FAILURE,
                latency_ms=_elapsed_ms(started),
                error_class="unexpected_status",
                metadata={"status": status, "final_url": final_url},
            )
        return ProbeResult(
            target_id=target.target_id,
            kind=target.kind,
            outcome=ProbeOutcome.SUCCESS,
            latency_ms=_elapsed_ms(started),
            metadata={"status": status, "final_url": final_url},
        )


def default_adapters() -> Dict[TargetKind, ProbeAdapter]:
    icmp = IcmpProbe()
    return {
        TargetKind.GATEWAY: icmp,
        TargetKind.EXTERNAL_IP: icmp,
        TargetKind.DNS: DnsProbe(),
        TargetKind.HTTPS: HttpsProbe(),
    }


async def _resolve(hostname: str) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(
        hostname, None, family=socket.AF_INET, type=socket.SOCK_STREAM
    )
    return tuple(sorted({record[4][0] for record in records}))


def _request_https(url: str, timeout_seconds: float) -> Tuple[int, str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "raspi-network-monitor/0.1"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        response.read(1)
        return response.getcode(), response.geturl()


def _ping_latency_ms(output: str) -> Optional[float]:
    match = re.search(r"time[=<]([0-9.]+)\s*ms", output)
    return float(match.group(1)) if match else None


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _failure(target: ProbeTarget, error_class: str, started: float) -> ProbeResult:
    return ProbeResult(
        target_id=target.target_id,
        kind=target.kind,
        outcome=ProbeOutcome.FAILURE,
        latency_ms=_elapsed_ms(started),
        error_class=error_class,
    )


def _unknown(target: ProbeTarget, error_class: str) -> ProbeResult:
    return ProbeResult(
        target_id=target.target_id,
        kind=target.kind,
        outcome=ProbeOutcome.UNKNOWN,
        error_class=error_class,
    )
