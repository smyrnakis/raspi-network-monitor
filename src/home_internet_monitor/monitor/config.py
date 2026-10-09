"""TOML configuration loading with fail-fast validation."""

import re
from dataclasses import dataclass, replace
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse
from zoneinfo import available_timezones

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9 and 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from .models import ProbeTarget, TargetKind

_SITE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TIMELINE_WINDOWS = {"24h", "7d", "30d"}
_LATENCY_WINDOWS = {"1h", "24h", "7d"}
_SERVICE_DASHBOARD_MODES = {"hidden", "compact", "detailed"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SiteConfig:
    site_id: str
    display_name: str
    timezone: str


@dataclass(frozen=True)
class StorageConfig:
    database_path: Path


@dataclass(frozen=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True)
class MonitorConfig:
    interval_seconds: float = 10.0
    round_timeout_seconds: float = 8.0
    max_concurrency: int = 4
    failure_threshold: int = 3
    recovery_threshold: int = 2


@dataclass(frozen=True)
class RouteConfig:
    required_interface: Optional[str]
    forbidden_interface_prefixes: Tuple[str, ...]


@dataclass(frozen=True)
class RetentionConfig:
    raw_samples_days: int = 7
    incidents_days: Optional[int] = None
    latency_aggregates_days: int = 548


@dataclass(frozen=True)
class DashboardConfig:
    hide_short_incidents: bool = True
    mtbf_minimum_incident_minutes: int = 1
    default_timeline_window: str = "24h"
    default_latency_window: str = "1h"


@dataclass(frozen=True)
class ServiceMonitorConfig:
    monitor_id: str
    label: str
    kind: str
    endpoint: Optional[str]
    status_file: Optional[Path]
    client_name: Optional[str]
    interval_seconds: float = 20.0
    timeout_seconds: float = 3.0
    failure_threshold: int = 3
    recovery_threshold: int = 2
    dashboard: str = "compact"
    enabled: bool = True
    default_window: str = "7d"


@dataclass(frozen=True)
class AppConfig:
    site: SiteConfig
    storage: StorageConfig
    web: WebConfig
    monitor: MonitorConfig
    route: RouteConfig
    retention: RetentionConfig
    dashboard: DashboardConfig
    probes: Tuple[ProbeTarget, ...]
    service_monitors: Tuple[ServiceMonitorConfig, ...] = ()


def load_config(path: Path) -> AppConfig:
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read configuration: {error}") from error
    return parse_config(raw)


def editable_settings(config: AppConfig) -> dict[str, Any]:
    return {
        "monitor": {
            "interval_seconds": config.monitor.interval_seconds,
            "round_timeout_seconds": config.monitor.round_timeout_seconds,
            "failure_threshold": config.monitor.failure_threshold,
            "recovery_threshold": config.monitor.recovery_threshold,
        },
        "retention": {
            "raw_samples_days": config.retention.raw_samples_days,
            "incidents_days": config.retention.incidents_days,
            "latency_aggregates_days": config.retention.latency_aggregates_days,
        },
        "dashboard": {
            "hide_short_incidents": config.dashboard.hide_short_incidents,
            "mtbf_minimum_incident_minutes": (
                config.dashboard.mtbf_minimum_incident_minutes
            ),
            "default_timeline_window": config.dashboard.default_timeline_window,
            "default_latency_window": config.dashboard.default_latency_window,
        },
        "probes": [
            {
                "id": probe.target_id,
                "kind": probe.kind.value,
                "endpoint": probe.endpoint,
                "timeout_seconds": probe.timeout_seconds,
                "enabled": probe.enabled,
                "expected_status": probe.expected_status,
            }
            for probe in config.probes
        ],
        "service_monitors": [
            {
                "id": monitor.monitor_id,
                "label": monitor.label,
                "kind": monitor.kind,
                "endpoint": monitor.endpoint,
                "status_file": (
                    str(monitor.status_file) if monitor.status_file is not None else None
                ),
                "client_name": monitor.client_name,
                "interval_seconds": monitor.interval_seconds,
                "timeout_seconds": monitor.timeout_seconds,
                "failure_threshold": monitor.failure_threshold,
                "recovery_threshold": monitor.recovery_threshold,
                "dashboard": monitor.dashboard,
                "default_window": monitor.default_window,
                "enabled": monitor.enabled,
            }
            for monitor in config.service_monitors
        ],
    }


def apply_editable_settings(
    base: AppConfig,
    raw: Mapping[str, Any],
) -> AppConfig:
    monitor_raw = _table(raw, "monitor")
    monitor = replace(
        base.monitor,
        interval_seconds=_number(
            monitor_raw, "interval_seconds", base.monitor.interval_seconds
        ),
        round_timeout_seconds=_number(
            monitor_raw,
            "round_timeout_seconds",
            base.monitor.round_timeout_seconds,
        ),
        failure_threshold=_integer(
            monitor_raw, "failure_threshold", base.monitor.failure_threshold
        ),
        recovery_threshold=_integer(
            monitor_raw, "recovery_threshold", base.monitor.recovery_threshold
        ),
    )
    # Older runtime-settings files predate editable retention settings.
    retention_raw = _optional_table(raw, "retention")
    incidents_days = retention_raw.get(
        "incidents_days", base.retention.incidents_days
    )
    if incidents_days is not None and (
        isinstance(incidents_days, bool) or not isinstance(incidents_days, int)
    ):
        raise ConfigError("incidents_days must be an integer or null")
    retention = replace(
        base.retention,
        raw_samples_days=_integer(
            retention_raw,
            "raw_samples_days",
            base.retention.raw_samples_days,
        ),
        incidents_days=incidents_days,
        latency_aggregates_days=_integer(
            retention_raw,
            "latency_aggregates_days",
            base.retention.latency_aggregates_days,
        ),
    )
    dashboard_raw = _optional_table(raw, "dashboard")
    dashboard = replace(
        base.dashboard,
        hide_short_incidents=_boolean(
            dashboard_raw,
            "hide_short_incidents",
            base.dashboard.hide_short_incidents,
        ),
        mtbf_minimum_incident_minutes=_integer(
            dashboard_raw,
            "mtbf_minimum_incident_minutes",
            base.dashboard.mtbf_minimum_incident_minutes,
        ),
        default_timeline_window=_string(
            dashboard_raw,
            "default_timeline_window",
            base.dashboard.default_timeline_window,
        ),
        default_latency_window=_string(
            dashboard_raw,
            "default_latency_window",
            base.dashboard.default_latency_window,
        ),
    )
    rows = raw.get("probes")
    if not isinstance(rows, list):
        raise ConfigError("probes must be a list")
    by_id = {probe.target_id: probe for probe in base.probes}
    if len(rows) != len(by_id):
        raise ConfigError("settings must include every configured probe exactly once")
    probes = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ConfigError("each probe setting must be an object")
        target_id = _string(row, "id")
        if target_id in seen or target_id not in by_id:
            raise ConfigError("probe settings contain an unknown or duplicate id")
        seen.add(target_id)
        original = by_id[target_id]
        if row.get("kind") != original.kind.value:
            raise ConfigError("probe kind cannot be changed")
        expected_status = row.get("expected_status")
        if expected_status is not None and (
            isinstance(expected_status, bool) or not isinstance(expected_status, int)
        ):
            raise ConfigError("probe expected_status must be an integer or null")
        probes.append(
            replace(
                original,
                endpoint=_string(row, "endpoint"),
                timeout_seconds=_number(
                    row, "timeout_seconds", original.timeout_seconds
                ),
                enabled=_boolean(row, "enabled", original.enabled),
                expected_status=expected_status,
            )
        )

    # Older runtime-settings files predate editable service monitors.
    service_rows = raw.get("service_monitors")
    if service_rows is None:
        service_monitors = base.service_monitors
    else:
        if not isinstance(service_rows, list):
            raise ConfigError("service_monitors must be a list")
        services_by_id = {
            monitor.monitor_id: monitor for monitor in base.service_monitors
        }
        if len(service_rows) != len(services_by_id):
            raise ConfigError(
                "settings must include every configured service monitor exactly once"
            )
        service_monitors_list = []
        seen_services = set()
        for row in service_rows:
            if not isinstance(row, dict):
                raise ConfigError("each service monitor setting must be an object")
            monitor_id = _string(row, "id")
            if monitor_id in seen_services or monitor_id not in services_by_id:
                raise ConfigError(
                    "service monitor settings contain an unknown or duplicate id"
                )
            seen_services.add(monitor_id)
            original = services_by_id[monitor_id]
            if row.get("kind") != original.kind:
                raise ConfigError("service monitor kind cannot be changed")
            endpoint = _optional_string(row, "endpoint", original.endpoint)
            status_file = _optional_string(
                row,
                "status_file",
                str(original.status_file) if original.status_file is not None else None,
            )
            client_name = _optional_string(
                row, "client_name", original.client_name
            )
            service_monitors_list.append(
                replace(
                    original,
                    endpoint=endpoint,
                    status_file=Path(status_file) if status_file is not None else None,
                    client_name=client_name,
                    interval_seconds=_number(
                        row, "interval_seconds", original.interval_seconds
                    ),
                    timeout_seconds=_number(
                        row, "timeout_seconds", original.timeout_seconds
                    ),
                    failure_threshold=_integer(
                        row, "failure_threshold", original.failure_threshold
                    ),
                    recovery_threshold=_integer(
                        row, "recovery_threshold", original.recovery_threshold
                    ),
                    dashboard=_string(row, "dashboard", original.dashboard),
                    default_window=_string(row, "default_window", original.default_window),
                    enabled=_boolean(row, "enabled", original.enabled),
                )
            )
        service_monitors = tuple(service_monitors_list)
    config = replace(
        base,
        monitor=monitor,
        retention=retention,
        dashboard=dashboard,
        probes=tuple(probes),
        service_monitors=service_monitors,
    )
    _validate(config)
    return config


def parse_config(raw: Mapping[str, Any]) -> AppConfig:
    site_raw = _table(raw, "site")
    storage_raw = _table(raw, "storage")
    web_raw = _optional_table(raw, "web")
    monitor_raw = _optional_table(raw, "monitor")
    route_raw = _optional_table(raw, "route")
    retention_raw = _optional_table(raw, "retention")
    dashboard_raw = _optional_table(raw, "dashboard")

    site = SiteConfig(
        site_id=_string(site_raw, "id"),
        display_name=_string(site_raw, "display_name"),
        timezone=_string(site_raw, "timezone"),
    )
    database_path = Path(_string(storage_raw, "database_path"))
    web = WebConfig(
        host=_string(web_raw, "host", "127.0.0.1"),
        port=_integer(web_raw, "port", 8080),
    )
    monitor = MonitorConfig(
        interval_seconds=_number(monitor_raw, "interval_seconds", 10.0),
        round_timeout_seconds=_number(
            monitor_raw, "round_timeout_seconds", 8.0
        ),
        max_concurrency=_integer(monitor_raw, "max_concurrency", 4),
        failure_threshold=_integer(monitor_raw, "failure_threshold", 3),
        recovery_threshold=_integer(monitor_raw, "recovery_threshold", 2),
    )
    prefixes = route_raw.get("forbidden_interface_prefixes", ["tun", "tap", "wg"])
    if not isinstance(prefixes, list) or not all(
        isinstance(item, str) and item for item in prefixes
    ):
        raise ConfigError("route.forbidden_interface_prefixes must be strings")
    required_interface = route_raw.get("required_interface")
    if required_interface is not None and not isinstance(required_interface, str):
        raise ConfigError("route.required_interface must be a string")
    route = RouteConfig(required_interface, tuple(prefixes))
    retention = RetentionConfig(
        raw_samples_days=_integer(retention_raw, "raw_samples_days", 7),
        incidents_days=_optional_positive_integer(
            retention_raw, "incidents_days", None
        ),
        latency_aggregates_days=_integer(
            retention_raw, "latency_aggregates_days", 548
        ),
    )
    dashboard = DashboardConfig(
        hide_short_incidents=_boolean(
            dashboard_raw, "hide_short_incidents", True
        ),
        mtbf_minimum_incident_minutes=_integer(
            dashboard_raw, "mtbf_minimum_incident_minutes", 1
        ),
        default_timeline_window=_string(
            dashboard_raw, "default_timeline_window", "24h"
        ),
        default_latency_window=_string(
            dashboard_raw, "default_latency_window", "1h"
        ),
    )

    probe_rows = raw.get("probes")
    if not isinstance(probe_rows, list):
        raise ConfigError("at least one [[probes]] table is required")
    probes = tuple(_parse_probe(row) for row in probe_rows)
    service_rows = raw.get("service_monitors", [])
    if not isinstance(service_rows, list):
        raise ConfigError("service_monitors must be a list")
    service_monitors = tuple(_parse_service_monitor(row) for row in service_rows)
    config = AppConfig(
        site,
        StorageConfig(database_path),
        web,
        monitor,
        route,
        retention,
        dashboard,
        probes,
        service_monitors,
    )
    _validate(config)
    return config


def _parse_probe(raw: Any) -> ProbeTarget:
    if not isinstance(raw, dict):
        raise ConfigError("each [[probes]] value must be a table")
    try:
        kind = TargetKind(_string(raw, "kind"))
    except ValueError as error:
        raise ConfigError(f"invalid probe kind: {raw.get('kind')!r}") from error
    route_default = kind in (TargetKind.EXTERNAL_IP, TargetKind.HTTPS)
    expected_status = raw.get("expected_status")
    if expected_status is not None and not isinstance(expected_status, int):
        raise ConfigError("probe expected_status must be an integer")
    return ProbeTarget(
        target_id=_string(raw, "id"),
        kind=kind,
        endpoint=_string(raw, "endpoint"),
        timeout_seconds=_number(raw, "timeout_seconds", 3.0),
        enabled=_boolean(raw, "enabled", True),
        expected_status=expected_status,
        require_native_route=_boolean(
            raw, "require_native_route", route_default
        ),
    )


def _parse_service_monitor(raw: Any) -> ServiceMonitorConfig:
    if not isinstance(raw, dict):
        raise ConfigError("each [[service_monitors]] value must be a table")
    kind = _string(raw, "kind")
    if kind != "openvpn_client":
        raise ConfigError(f"invalid service monitor kind: {kind!r}")
    endpoint = raw.get("endpoint")
    if endpoint is not None and (not isinstance(endpoint, str) or not endpoint):
        raise ConfigError("service monitor endpoint must be a non-empty string")
    status_value = raw.get("status_file")
    if status_value is not None and (
        not isinstance(status_value, str) or not status_value
    ):
        raise ConfigError("service monitor status_file must be a non-empty string")
    client_name = raw.get("client_name")
    if client_name is not None and (
        not isinstance(client_name, str) or not client_name
    ):
        raise ConfigError("service monitor client_name must be a non-empty string")
    return ServiceMonitorConfig(
        monitor_id=_string(raw, "id"),
        label=_string(raw, "label"),
        kind=kind,
        endpoint=endpoint,
        status_file=Path(status_value) if status_value is not None else None,
        client_name=client_name,
        interval_seconds=_number(raw, "interval_seconds", 20.0),
        timeout_seconds=_number(raw, "timeout_seconds", 3.0),
        failure_threshold=_integer(raw, "failure_threshold", 3),
        recovery_threshold=_integer(raw, "recovery_threshold", 2),
        dashboard=_string(raw, "dashboard", "compact"),
        default_window=_string(raw, "default_window", "7d"),
        enabled=_boolean(raw, "enabled", True),
    )


def _validate(config: AppConfig) -> None:
    if not _SITE_ID.fullmatch(config.site.site_id):
        raise ConfigError("site.id must contain lowercase letters, digits, _ or -")
    if not re.fullmatch(r"[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)*", config.site.timezone):
        raise ConfigError("site.timezone is not a valid IANA timezone name")
    known_timezones = available_timezones()
    if known_timezones and config.site.timezone not in known_timezones:
        raise ConfigError("site.timezone is not a known IANA timezone")
    # A POSIX path parsed on Windows has a root but no drive, so pathlib does
    # not call it absolute. The deployed application is Linux-only.
    if not config.storage.database_path.root:
        raise ConfigError("storage.database_path must be absolute")
    if config.web.host != "localhost":
        try:
            web_address = ip_address(config.web.host)
        except ValueError as error:
            raise ConfigError("web.host must be an IP address or localhost") from error
        if not (
            web_address.is_loopback
            or web_address.is_private
            or web_address.is_unspecified
        ):
            raise ConfigError("web.host must be loopback, private, or wildcard")
    if not 1 <= config.web.port <= 65535:
        raise ConfigError("web.port must be between 1 and 65535")
    monitor = config.monitor
    if monitor.interval_seconds <= 0 or monitor.round_timeout_seconds <= 0:
        raise ConfigError("monitor intervals and timeouts must be positive")
    if monitor.round_timeout_seconds >= monitor.interval_seconds:
        raise ConfigError("monitor.round_timeout_seconds must be below interval_seconds")
    if min(
        monitor.max_concurrency,
        monitor.failure_threshold,
        monitor.recovery_threshold,
    ) < 1:
        raise ConfigError("monitor counts and thresholds must be positive")
    if config.retention.raw_samples_days < 1:
        raise ConfigError("raw sample retention must be positive")
    if (
        config.retention.incidents_days is not None
        and config.retention.incidents_days < 1
    ):
        raise ConfigError("incident retention must be positive or null")
    if config.retention.latency_aggregates_days < 1:
        raise ConfigError("latency aggregate retention must be positive")
    if config.retention.latency_aggregates_days <= config.retention.raw_samples_days:
        raise ConfigError("latency aggregate retention must exceed raw sample retention")
    if not 0 <= config.dashboard.mtbf_minimum_incident_minutes <= 1440:
        raise ConfigError("MTBF minimum incident duration must be between 0 and 1440 minutes")
    if config.dashboard.default_timeline_window not in _TIMELINE_WINDOWS:
        raise ConfigError("default timeline window must be 24h, 7d, or 30d")
    if config.dashboard.default_latency_window not in _LATENCY_WINDOWS:
        raise ConfigError("default latency window must be 1h, 24h, or 7d")

    enabled = tuple(probe for probe in config.probes if probe.enabled)
    ids = [probe.target_id for probe in config.probes]
    if len(ids) != len(set(ids)):
        raise ConfigError("probe ids must be unique")
    if any(not _SITE_ID.fullmatch(probe.target_id) for probe in config.probes):
        raise ConfigError("probe ids must contain lowercase letters, digits, _ or -")
    if any(probe.timeout_seconds <= 0 for probe in config.probes):
        raise ConfigError("probe timeouts must be positive")
    if any(probe.timeout_seconds >= monitor.round_timeout_seconds for probe in enabled):
        raise ConfigError("probe timeouts must be below the round timeout")
    required = {
        TargetKind.GATEWAY: 1,
        TargetKind.EXTERNAL_IP: 2,
        TargetKind.DNS: 1,
        TargetKind.HTTPS: 1,
    }
    for kind, minimum in required.items():
        count = sum(probe.kind is kind for probe in enabled)
        if count < minimum:
            raise ConfigError(f"at least {minimum} enabled {kind.value} probe(s) required")
    for probe in config.probes:
        if probe.kind is TargetKind.GATEWAY and probe.endpoint != "auto":
            try:
                ip_address(probe.endpoint)
            except ValueError as error:
                raise ConfigError("gateway probes require 'auto' or an IP address") from error
        if probe.kind is TargetKind.EXTERNAL_IP:
            try:
                ip_address(probe.endpoint)
            except ValueError as error:
                raise ConfigError("external IP probes require an IP address") from error
        if probe.kind is TargetKind.HTTPS:
            parsed = urlparse(probe.endpoint)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ConfigError("HTTPS probes require an https:// URL")
            if probe.expected_status is not None and not 100 <= probe.expected_status <= 599:
                raise ConfigError("probe expected_status must be between 100 and 599")

    service_ids = [monitor.monitor_id for monitor in config.service_monitors]
    if len(service_ids) != len(set(service_ids)):
        raise ConfigError("service monitor ids must be unique")
    for service in config.service_monitors:
        if not _SITE_ID.fullmatch(service.monitor_id):
            raise ConfigError(
                "service monitor ids must contain lowercase letters, digits, _ or -"
            )
        if service.dashboard not in _SERVICE_DASHBOARD_MODES:
            raise ConfigError("service monitor dashboard must be hidden, compact, or detailed")
        if service.default_window not in _LATENCY_WINDOWS:
            raise ConfigError("service default window must be 1h, 24h, or 7d")
        if min(
            service.interval_seconds,
            service.timeout_seconds,
            service.failure_threshold,
            service.recovery_threshold,
        ) <= 0:
            raise ConfigError("service monitor intervals, timeouts and thresholds must be positive")
        if service.timeout_seconds >= service.interval_seconds:
            raise ConfigError("service monitor timeout must be below its interval")
        if service.endpoint is None and (
            service.status_file is None or service.client_name is None
        ):
            raise ConfigError(
                "OpenVPN client monitor requires endpoint or status_file and client_name"
            )
        if service.endpoint is not None:
            try:
                address = ip_address(service.endpoint)
            except ValueError as error:
                raise ConfigError("OpenVPN client endpoint must be an IP address") from error
            if not address.is_private:
                raise ConfigError("OpenVPN client endpoint must be a private IP address")
        if (service.status_file is None) != (service.client_name is None):
            raise ConfigError(
                "OpenVPN status_file and client_name must be configured together"
            )
        if service.status_file is not None and not service.status_file.root:
            raise ConfigError("OpenVPN status_file must be an absolute path")


def _table(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] table is required")
    return value


def _optional_table(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _string(raw: Mapping[str, Any], key: str, default: Optional[str] = None) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def _optional_string(
    raw: Mapping[str, Any], key: str, default: Optional[str] = None
) -> Optional[str]:
    value = raw.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string or null")
    return value


def _number(raw: Mapping[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number")
    return float(value)


def _integer(raw: Mapping[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    return value


def _optional_positive_integer(
    raw: Mapping[str, Any], key: str, default: Optional[int]
) -> Optional[int]:
    value = raw.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer or null")
    return value


def _boolean(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false")
    return value
