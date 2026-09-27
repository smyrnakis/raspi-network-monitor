"""Linux route checks that keep VPN paths from corrupting internet evidence."""

import asyncio
import socket
from typing import Awaitable, Callable, Optional, Sequence
from urllib.parse import urlparse

from .commands import CommandResult, run_command
from .models import RouteDecision
from .probes import CommandRunner

HostResolver = Callable[[str], Awaitable[str]]


class LinuxRouteInspector:
    def __init__(
        self,
        forbidden_interface_prefixes: Sequence[str] = ("tun", "tap", "wg"),
        required_interface: Optional[str] = None,
        runner: CommandRunner = run_command,
        resolver: Optional[HostResolver] = None,
    ) -> None:
        self._forbidden_prefixes = tuple(forbidden_interface_prefixes)
        self._required_interface = required_interface
        self._runner = runner
        self._resolver = resolver or _resolve_ipv4

    async def inspect(self, endpoint: str, timeout_seconds: float) -> RouteDecision:
        host = _endpoint_host(endpoint)
        try:
            destination = await asyncio.wait_for(
                self._resolver(host), timeout=timeout_seconds
            )
            result = await self._runner(
                ["ip", "-4", "route", "get", destination], timeout_seconds
            )
        except asyncio.TimeoutError:
            return RouteDecision(False, reason="route_check_timeout")
        except (OSError, socket.gaierror, ValueError):
            return RouteDecision(False, reason="route_check_failed")

        if result.returncode != 0:
            return RouteDecision(False, reason="route_lookup_failed")
        interface = _token_after(result.stdout, "dev")
        gateway = _token_after(result.stdout, "via")
        if not interface:
            return RouteDecision(False, reason="missing_interface")
        if self._required_interface and interface != self._required_interface:
            return RouteDecision(
                False, interface, gateway, "unexpected_interface"
            )
        if any(interface.startswith(prefix) for prefix in self._forbidden_prefixes):
            return RouteDecision(False, interface, gateway, "vpn_interface")
        return RouteDecision(True, interface, gateway)

    async def default_gateway(self, timeout_seconds: float) -> RouteDecision:
        try:
            result = await self._runner(
                ["ip", "-4", "route", "show", "default"], timeout_seconds
            )
        except asyncio.TimeoutError:
            return RouteDecision(False, reason="gateway_lookup_timeout")
        except OSError:
            return RouteDecision(False, reason="gateway_lookup_failed")
        if result.returncode != 0:
            return RouteDecision(False, reason="gateway_lookup_failed")
        first_line = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
        gateway = _token_after(first_line, "via")
        interface = _token_after(first_line, "dev")
        if not gateway or not interface:
            return RouteDecision(False, interface, gateway, "missing_default_route")
        return RouteDecision(True, interface, gateway)


def _endpoint_host(endpoint: str) -> str:
    if "://" not in endpoint:
        return endpoint
    host = urlparse(endpoint).hostname
    if not host:
        raise ValueError("endpoint has no hostname")
    return host


async def _resolve_ipv4(host: str) -> str:
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        pass
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(
        host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
    )
    if not records:
        raise socket.gaierror("no IPv4 address")
    return records[0][4][0]


def _token_after(output: str, token: str) -> Optional[str]:
    words = output.split()
    try:
        return words[words.index(token) + 1]
    except (ValueError, IndexError):
        return None
