"""FastAPI application exposing monitoring data and a bounded manual test."""

import asyncio
import csv
import io
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from home_internet_monitor import __version__
from home_internet_monitor.monitor.config import AppConfig
from home_internet_monitor.monitor.config import ConfigError
from home_internet_monitor.monitor.rounds import RoundExecutor
from home_internet_monitor.monitor.routing import LinuxRouteInspector
from home_internet_monitor.monitor.settings import RuntimeSettingsStore
from home_internet_monitor.monitor.service_monitors import OpenVpnClientCheck
from home_internet_monitor.storage import connect_readonly

from .queries import MonitoringQueries, default_window


def create_app(
    config: AppConfig,
    settings_store: Optional[RuntimeSettingsStore] = None,
) -> FastAPI:
    static_directory = Path(__file__).resolve().parents[1] / "web" / "static"
    app = FastAPI(
        title="Raspberry Pi Network Monitor",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.mount("/assets", StaticFiles(directory=static_directory), name="assets")
    manual_test_lock = asyncio.Lock()
    runtime_settings = settings_store or RuntimeSettingsStore(config)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(static_directory / "index.html")

    @app.get("/settings", include_in_schema=False)
    def settings():
        return FileResponse(static_directory / "settings.html")

    @app.get("/history", include_in_schema=False)
    def history_page():
        return FileResponse(static_directory / "history.html")

    @app.get("/report", include_in_schema=False)
    def report_page():
        return FileResponse(static_directory / "report.html")

    @app.get("/service", include_in_schema=False)
    def service_page():
        return FileResponse(static_directory / "service.html")

    def queries() -> Iterator[MonitoringQueries]:
        connection = connect_readonly(config.storage.database_path)
        try:
            yield MonitoringQueries(connection)
        finally:
            connection.close()

    @app.get("/api/v1/health")
    def health(query: MonitoringQueries = Depends(queries)):
        return query.health(
            config.site.site_id,
            datetime.now(timezone.utc),
            stale_after_seconds=max(30.0, config.monitor.interval_seconds * 3),
        )

    @app.get("/api/v1/status")
    def status(query: MonitoringQueries = Depends(queries)):
        try:
            payload = query.current_status(config.site.site_id)
            active_config = runtime_settings.load_config()
            payload["hostname"] = socket.gethostname()
            payload["version"] = __version__
            payload["dashboard"] = {
                "hide_short_incidents": (
                    active_config.dashboard.hide_short_incidents
                ),
                "mtbf_minimum_incident_minutes": (
                    active_config.dashboard.mtbf_minimum_incident_minutes
                ),
                "default_timeline_window": (
                    active_config.dashboard.default_timeline_window
                ),
                "default_latency_window": (
                    active_config.dashboard.default_latency_window
                ),
            }
            return payload
        except KeyError as error:
            raise HTTPException(status_code=404, detail="site not initialized") from error

    @app.get("/api/v1/incidents")
    def incidents(
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        minimum_duration_seconds: int = Query(0, ge=0, le=86_400),
        query: MonitoringQueries = Depends(queries),
    ):
        return {
            "items": query.incidents(
                config.site.site_id,
                limit,
                offset,
                minimum_duration_seconds=minimum_duration_seconds,
            ),
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/v1/gaps")
    def gaps(
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        minimum_duration_seconds: int = Query(0, ge=0, le=86400),
        query: MonitoringQueries = Depends(queries),
    ):
        return {
            "items": query.gaps(
                config.site.site_id,
                limit,
                offset,
                minimum_duration_seconds=minimum_duration_seconds,
            ),
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/v1/history")
    def history(
        event_type: Optional[str] = None,
        category: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        query: MonitoringQueries = Depends(queries),
    ):
        try:
            return query.history(
                config.site.site_id,
                limit,
                offset,
                event_type=event_type,
                category=category,
                start=_optional_utc(start),
                end=_optional_utc(end),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/v1/history/item")
    def history_item(
        event_type: str,
        event_id: str,
        query: MonitoringQueries = Depends(queries),
    ):
        try:
            item = query.history_item(config.site.site_id, event_type, event_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if item is None:
            raise HTTPException(status_code=404, detail="history event not found")
        return item

    @app.get("/api/v1/availability")
    def availability(
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        query: MonitoringQueries = Depends(queries),
    ):
        effective_end = _utc(end or datetime.now(timezone.utc))
        default_start, _ = default_window(effective_end)
        effective_start = _utc(start or default_start)
        try:
            active_config = runtime_settings.load_config()
            return query.availability(
                config.site.site_id,
                effective_start,
                effective_end,
                minimum_incident_duration_seconds=(
                    active_config.dashboard.mtbf_minimum_incident_minutes * 60
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/v1/timeline")
    def timeline(
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        query: MonitoringQueries = Depends(queries),
    ):
        effective_end = _utc(end or datetime.now(timezone.utc))
        default_start, _ = default_window(effective_end)
        effective_start = _utc(start or default_start)
        try:
            return query.timeline(config.site.site_id, effective_start, effective_end)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/v1/latency")
    def latency(
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        bucket_seconds: int = Query(300, ge=60, le=86_400),
        query: MonitoringQueries = Depends(queries),
    ):
        effective_end = _utc(end or datetime.now(timezone.utc))
        effective_start = _utc(start or (effective_end - timedelta(hours=24)))
        try:
            return query.latency(
                config.site.site_id,
                effective_start,
                effective_end,
                bucket_seconds,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/v1/services")
    def services(query: MonitoringQueries = Depends(queries)):
        return {
            "items": query.service_summaries(
                config.site.site_id, datetime.now(timezone.utc)
            )
        }

    @app.get("/api/v1/services/{monitor_id}")
    def service_summary(
        monitor_id: str, query: MonitoringQueries = Depends(queries)
    ):
        payload = query.service_summary(
            config.site.site_id, monitor_id, datetime.now(timezone.utc)
        )
        if payload is None:
            raise HTTPException(status_code=404, detail="service monitor not found")
        return payload

    @app.get("/api/v1/services/{monitor_id}/timeline")
    def service_timeline(
        monitor_id: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        query: MonitoringQueries = Depends(queries),
    ):
        effective_end = _utc(end or datetime.now(timezone.utc))
        effective_start = _utc(start or (effective_end - timedelta(days=7)))
        try:
            return query.service_timeline(
                config.site.site_id, monitor_id, effective_start, effective_end
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="service monitor not found") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/v1/services/{monitor_id}/latency")
    def service_latency(
        monitor_id: str, start: Optional[datetime] = None, end: Optional[datetime] = None,
        bucket_seconds: int = Query(60, ge=60, le=86_400),
        query: MonitoringQueries = Depends(queries),
    ):
        effective_end = _utc(end or datetime.now(timezone.utc))
        effective_start = _utc(start or (effective_end - timedelta(hours=1)))
        try:
            return query.service_latency(
                config.site.site_id, monitor_id, effective_start, effective_end, bucket_seconds,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="service monitor not found") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/v1/services/{monitor_id}/incidents")
    def service_incidents(
        monitor_id: str,
        limit: int = Query(20, ge=1, le=100),
        query: MonitoringQueries = Depends(queries),
    ):
        return {
            "items": query.service_incidents(config.site.site_id, monitor_id, limit)
        }

    @app.post("/api/v1/services/{monitor_id}/test")
    async def manual_service_test(
        monitor_id: str,
        action: Optional[str] = Header(None, alias="X-Monitor-Action"),
    ):
        if action != "manual-test":
            raise HTTPException(status_code=403, detail="manual test header required")
        if manual_test_lock.locked():
            raise HTTPException(status_code=409, detail="a manual test is already running")
        async with manual_test_lock:
            active_config = runtime_settings.load_config()
            monitor = next(
                (item for item in active_config.service_monitors if item.monitor_id == monitor_id),
                None,
            )
            if monitor is None:
                raise HTTPException(status_code=404, detail="service monitor not found")
            if not monitor.enabled:
                raise HTTPException(status_code=409, detail="service monitor is disabled")
            try:
                observation = await asyncio.wait_for(
                    OpenVpnClientCheck().check(monitor),
                    timeout=max(5.0, monitor.timeout_seconds + 3.0),
                )
            except asyncio.TimeoutError as error:
                raise HTTPException(status_code=504, detail="service test timed out") from error
        return {
            "status": observation.status,
            "last_checked": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "last_latency_ms": observation.latency_ms,
            "error_class": observation.error_class,
        }

    @app.post("/api/v1/test")
    async def manual_test(
        action: Optional[str] = Header(None, alias="X-Monitor-Action"),
    ):
        if action != "manual-test":
            raise HTTPException(status_code=403, detail="manual test header required")
        if manual_test_lock.locked():
            raise HTTPException(status_code=409, detail="a manual test is already running")
        async with manual_test_lock:
            active_config = runtime_settings.load_config()
            routes = LinuxRouteInspector(
                forbidden_interface_prefixes=(
                    active_config.route.forbidden_interface_prefixes
                ),
                required_interface=active_config.route.required_interface,
            )
            executor = RoundExecutor(
                active_config.probes,
                routes,
                max_concurrency=active_config.monitor.max_concurrency,
                round_timeout_seconds=active_config.monitor.round_timeout_seconds,
            )
            completed = await executor.execute()
        evidence = completed.evidence
        return {
            "status": completed.status.value,
            "observed_at": completed.observed_at.isoformat().replace("+00:00", "Z"),
            "components": {
                "gateway": evidence.gateway.value,
                "external_ip": evidence.external_ip.value,
                "dns": evidence.dns.value,
                "https": evidence.https.value,
            },
        }

    @app.get("/api/v1/settings")
    def get_settings():
        try:
            return runtime_settings.read()
        except ConfigError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @app.put("/api/v1/settings")
    def update_settings(
        payload: Dict[str, Any] = Body(...),
        action: Optional[str] = Header(None, alias="X-Monitor-Action"),
    ):
        if action != "settings-update":
            raise HTTPException(status_code=403, detail="settings update header required")
        try:
            saved = runtime_settings.save(payload)
        except ConfigError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"settings": saved, "monitor_restart_requested": True}

    @app.get("/api/v1/incidents.csv")
    def incidents_csv(query: MonitoringQueries = Depends(queries)):
        rows = query.incidents_for_export(config.site.site_id)
        output = io.StringIO(newline="")
        fieldnames = [
            "incident_id",
            "status",
            "lifecycle",
            "observed_start",
            "confirmed_start",
            "observed_end",
            "confirmed_end",
            "end_reason",
            "previous_incident_id",
            "notes",
            "phase_categories",
            "phase_count",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(_csv_safe(row) for row in rows)
        return Response(
            content=output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="incidents.csv"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/history.csv")
    def history_csv(
        event_type: Optional[str] = None,
        category: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        query: MonitoringQueries = Depends(queries),
    ):
        try:
            rows = query.history_for_export(
                config.site.site_id,
                event_type=event_type,
                category=category,
                start=_optional_utc(start),
                end=_optional_utc(end),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        output = io.StringIO(newline="")
        fieldnames = [
            "event_type",
            "event_id",
            "category",
            "state",
            "start",
            "confirmed_start",
            "end",
            "confirmed_end",
            "duration_seconds",
            "end_reason",
            "previous_incident_id",
            "phase_categories",
            "phase_count",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(_csv_safe(row) for row in rows)
        return Response(
            content=output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="history.csv"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(status_code=400, detail="timestamps must include an offset")
    return value.astimezone(timezone.utc)


def _optional_utc(value: Optional[datetime]) -> Optional[datetime]:
    return _utc(value) if value is not None else None


def _csv_safe(row):
    """Prevent user-authored notes from becoming spreadsheet formulas."""

    safe = {}
    for key, value in row.items():
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
            safe[key] = "'" + value
        else:
            safe[key] = value
    return safe
