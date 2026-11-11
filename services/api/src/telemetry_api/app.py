"""FastAPI application: thin HTTP layer over :class:`FleetService`.

No ``from __future__ import annotations`` here on purpose: the route signatures reference the
run-time ``settings`` (page-size limit), and FastAPI must evaluate them eagerly.
"""

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Path, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime

from telemetry_api import __version__
from telemetry_api.config import ApiSettings
from telemetry_api.errors import InvalidQueryError, RepositoryUnavailableError, VehicleNotFoundError
from telemetry_api.models import (
    HealthResponse,
    TelemetryPage,
    TelemetryRecord,
    VehicleDetail,
    VehicleSummary,
)
from telemetry_api.repository import VehicleRepository
from telemetry_api.service import FleetService
from telemetry_common.schema import VEHICLE_ID_PATTERN

logger = logging.getLogger("telemetry_api")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
REQUEST_ID_HEADER = "X-Request-ID"

VehicleId = Annotated[
    str, Path(pattern=VEHICLE_ID_PATTERN, description="Vehicle identifier, e.g. `VH-0001`.")
]

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "Unknown vehicle"}}


def get_service(request: Request) -> FleetService:
    service: FleetService = request.app.state.service
    return service


ServiceDep = Annotated[FleetService, Depends(get_service)]


def _build_router(settings: ApiSettings) -> APIRouter:
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        """Liveness probe. Deliberately does not touch the database."""
        return HealthResponse(version=__version__)

    @router.get("/vehicles", response_model=list[VehicleSummary], tags=["vehicles"])
    def list_vehicles(service: ServiceDep) -> list[VehicleSummary]:
        """All vehicles that have ever reported data, with online state."""
        return service.list_vehicles()

    @router.get(
        "/vehicles/{vehicle_id}",
        response_model=VehicleDetail,
        responses=_NOT_FOUND,
        tags=["vehicles"],
    )
    def get_vehicle(vehicle_id: VehicleId, service: ServiceDep) -> VehicleDetail:
        """Current state of one vehicle including its latest telemetry snapshot."""
        return service.get_vehicle(vehicle_id)

    @router.get(
        "/vehicles/{vehicle_id}/latest",
        response_model=TelemetryRecord,
        responses=_NOT_FOUND,
        tags=["telemetry"],
    )
    def get_latest(vehicle_id: VehicleId, service: ServiceDep) -> TelemetryRecord:
        """The most recent telemetry snapshot (served from the small `vehicles` table)."""
        return service.latest(vehicle_id)

    @router.get(
        "/vehicles/{vehicle_id}/telemetry",
        response_model=TelemetryPage,
        responses={**_NOT_FOUND, 400: {"description": "Invalid time range or cursor"}},
        tags=["telemetry"],
    )
    def get_telemetry(
        vehicle_id: VehicleId,
        service: ServiceDep,
        start: Annotated[
            AwareDatetime | None,
            Query(description="Inclusive lower bound, ISO 8601 with timezone (use `Z`)."),
        ] = None,
        end: Annotated[
            AwareDatetime | None,
            Query(description="Inclusive upper bound, ISO 8601 with timezone (use `Z`)."),
        ] = None,
        limit: Annotated[
            int, Query(ge=1, le=settings.max_page_size, description="Items per page.")
        ] = settings.default_page_size,
        cursor: Annotated[
            str | None, Query(description="`next_cursor` of the previous page.")
        ] = None,
        order: Annotated[
            Literal["asc", "desc"], Query(description="Sort by time; newest first by default.")
        ] = "desc",
    ) -> TelemetryPage:
        """Telemetry history of one vehicle, paginated."""
        return service.telemetry_page(
            vehicle_id,
            start=start,
            end=end,
            limit=limit,
            cursor=cursor,
            newest_first=order == "desc",
        )

    return router


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def build_app(
    repository: VehicleRepository,
    settings: ApiSettings | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Create the application. The repository is injected, so tests need no AWS."""
    settings = settings or ApiSettings()
    app = FastAPI(
        title="Vehicle Telemetry API",
        version=__version__,
        description="Read access to vehicle telemetry collected via AWS IoT Core.",
    )
    service_kwargs = {"clock": clock} if clock else {}
    app.state.service = FleetService(
        repository, online_threshold_s=settings.online_threshold_s, **service_kwargs
    )
    app.include_router(_build_router(settings))

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET"],
            allow_headers=[REQUEST_ID_HEADER],
        )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        # Only well-formed IDs are echoed into logs/headers (no log or header injection).
        request_id = incoming if _REQUEST_ID_RE.fullmatch(incoming) else str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return response

    @app.exception_handler(VehicleNotFoundError)
    async def not_found(_: Request, exc: VehicleNotFoundError) -> JSONResponse:
        return _error(404, str(exc))

    @app.exception_handler(InvalidQueryError)
    async def bad_query(_: Request, exc: InvalidQueryError) -> JSONResponse:
        return _error(400, str(exc))

    @app.exception_handler(RepositoryUnavailableError)
    async def unavailable(request: Request, exc: RepositoryUnavailableError) -> JSONResponse:
        logger.error(
            "repository_unavailable",
            exc_info=exc,
            extra={"request_id": getattr(request.state, "request_id", None)},
        )
        return _error(503, "data store temporarily unavailable")

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_error",
            exc_info=exc,
            extra={"request_id": getattr(request.state, "request_id", None)},
        )
        return _error(500, "internal server error")  # no internals in the response

    return app
