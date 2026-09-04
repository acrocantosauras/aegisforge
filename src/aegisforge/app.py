from __future__ import annotations

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from aegisforge.api.routes.agent import router as agent_router
from aegisforge.api.routes.approvals import router as approvals_router
from aegisforge.api.routes.audit import router as audit_router
from aegisforge.api.routes.auth import router as auth_router
from aegisforge.api.routes.documents import router as documents_router
from aegisforge.api.routes.evaluation import router as evaluation_router
from aegisforge.api.routes.execution import router as execution_router
from aegisforge.api.routes.health import router as health_router
from aegisforge.api.routes.requests import router as requests_router
from aegisforge.config import Settings, get_settings
from aegisforge.db.session import get_db, get_session_factory
from aegisforge.logging_config import configure_logging
from aegisforge.observability.metrics import (
    get_metrics,
    get_metrics_content_type,
    HAS_PROMETHEUS,
    track_http_request,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    # Override the get_db dependency to use the provided settings
    def get_db_override():
        SessionLocal = get_session_factory(settings)
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = get_db_override

    # F11: CORS — configurable origins, no wildcard for authenticated usage
    cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id") or request.headers.get("X-Request-ID")
        response = await call_next(request)
        if request_id:
            response.headers["x-request-id"] = request_id
        else:
            response.headers["x-request-id"] = "generated"
        return response

    # F3: Prometheus HTTP metrics middleware
    @app.middleware("http")
    async def track_metrics(request: Request, call_next):  # type: ignore[no-untyped-def]
        method = request.method
        path = request.url.path
        start_time = __import__("time").monotonic()
        response = await call_next(request)
        duration = __import__("time").monotonic() - start_time

        if HAS_PROMETHEUS:
            from aegisforge.observability.metrics import HTTP_REQUESTS_TOTAL, HTTP_REQUEST_DURATION

            # Normalize path to avoid high cardinality labels
            normalized_path = _normalize_path(path)
            HTTP_REQUESTS_TOTAL.labels(
                method=method, endpoint=normalized_path, status=str(response.status_code)
            ).inc()
            HTTP_REQUEST_DURATION.labels(
                method=method, endpoint=normalized_path
            ).observe(duration)

        return response

    app.include_router(health_router, prefix=settings.api_prefix)
    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(requests_router, prefix=settings.api_prefix)
    app.include_router(agent_router, prefix=settings.api_prefix)
    app.include_router(execution_router, prefix=settings.api_prefix)
    app.include_router(documents_router, prefix=settings.api_prefix)
    app.include_router(approvals_router, prefix=settings.api_prefix)
    app.include_router(evaluation_router, prefix=settings.api_prefix)
    app.include_router(audit_router, prefix=settings.api_prefix)

    # Prometheus metrics endpoint
    @app.get("/metrics")
    def metrics() -> Response:
        return Response(
            content=get_metrics(),
            media_type=get_metrics_content_type(),
        )

    return app


def _normalize_path(path: str) -> str:
    """Normalize API path for metrics to avoid high-cardinality labels.

    /api/v1/requests/abc123 → /api/v1/requests/{id}
    /api/v1/execution/requests/abc123/execute → /api/v1/execution/requests/{id}/execute
    """
    import re
    # Replace UUIDs and hex IDs with {id}
    path = re.sub(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "/{id}", path)
    path = re.sub(r"/[0-9a-f]{24,}", "/{id}", path)
    # Replace common patterns
    path = re.sub(r"/doc-[a-z0-9]+", "/doc-{id}", path)
    path = re.sub(r"/wf-[a-z0-9]+", "/wf-{id}", path)
    path = re.sub(r"/job-[a-z0-9]+", "/job-{id}", path)
    path = re.sub(r"/approval-[a-z0-9]+", "/approval-{id}", path)
    path = re.sub(r"/req-[a-z0-9]+", "/req-{id}", path)
    path = re.sub(r"/cp-[a-z0-9]+", "/cp-{id}", path)
    return path


app = create_app()
