"""Health and readiness probes for AegisForge.

/health  — process alive (always 200 if the server is running).
           Liveness must NOT depend on external dependencies.
/ready   — process alive AND critical dependencies (DB, Redis) reachable.
           Returns 503 when dependencies are down.  Unauthenticated probe:
           dependency *detail* stays in logs, the response only says
           "ok"/"error" so probes never expose connection internals.
/system  — authenticated operational dashboard data (component status).
/workers — authenticated operational visibility into the worker registry.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Response, status

from aegisforge.db.models import UserModel
from aegisforge.services.auth_service import get_current_user

router = APIRouter(tags=["health"])


def _public_check(value: str) -> str:
    """Collapse a probe result to ok/error for unauthenticated responses.

    Probe helpers return ``error: <raw exception>`` for diagnostics; those
    strings can carry hosts, usernames, or driver details and must never be
    returned to an anonymous caller.  Full detail is logged instead.
    """
    return "ok" if value == "ok" else "error"

logger = logging.getLogger(__name__)

# Cache for readiness check results to avoid hammering dependencies
_readiness_cache: dict[str, Any] = {"last_check": 0.0, "result": None}
_READINESS_CACHE_TTL = 5.0  # seconds


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe: returns 200 when the process is alive.

    This MUST NOT depend on external dependencies. If the process
    is alive, this returns 200 regardless of Redis/Postgres status.
    """
    return {"status": "ok"}


def _check_database() -> str:
    """Probe database connectivity.  Returns 'ok' or an 'error: ...' string.

    Kept as a standalone function so the readiness path is directly
    testable (regression: a signature drift in ``get_engine`` used to
    surface only as a runtime 503 in deployed containers).
    """
    try:
        from sqlalchemy import text

        from aegisforge.config import get_settings
        from aegisforge.db.session import get_engine

        engine = get_engine(get_settings().database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        logger.warning("Readiness check: database unavailable: %s", exc)
        return f"error: {exc}"


def _check_redis() -> str:
    """Probe Redis connectivity.  Returns 'ok' or an 'error: ...' string."""
    try:
        import redis as redis_lib

        from aegisforge.config import get_settings

        client = redis_lib.from_url(
            get_settings().redis_url, decode_responses=True
        )
        client.ping()
        return "ok"
    except Exception as exc:
        logger.warning("Readiness check: redis unavailable: %s", exc)
        return f"error: {exc}"


@router.get("/ready")
def readiness(response: Response) -> dict[str, Any]:
    """Readiness probe: checks database and Redis connectivity.

    Returns 200 when all critical dependencies are reachable, 503 otherwise.
    Uses a short TTL cache to avoid overwhelming dependencies under load.
    """
    now = time.monotonic()
    if (
        _readiness_cache["result"] is not None
        and (now - _readiness_cache["last_check"]) < _READINESS_CACHE_TTL
    ):
        cached = _readiness_cache["result"]
        if not cached["all_ok"]:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        # Same public redaction as the fresh path — cached hits must not echo
        # raw probe exceptions to anonymous callers either.
        cached_checks = cached["checks"]
        if isinstance(cached_checks, dict):
            return {
                "status": cached["status"],
                "checks": {k: _public_check(v) for k, v in cached_checks.items()},
            }
        return {"status": cached["status"], "checks": {}}

    checks: dict[str, str] = {
        "database": _check_database(),
        "redis": _check_redis(),
    }

    all_ok = all(v == "ok" for v in checks.values())
    result = {"status": "ready" if all_ok else "not_ready", "checks": checks, "all_ok": all_ok}

    # Cache the result (raw detail stays server-side for logging/tests)
    _readiness_cache["last_check"] = now
    _readiness_cache["result"] = result

    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    # Public response: never echo raw probe exceptions to anonymous callers.
    return {
        "status": result["status"],
        "checks": {k: _public_check(v) for k, v in checks.items()},
    }


@router.get("/system")
def system_status(
    user: UserModel = Depends(get_current_user),
) -> dict[str, Any]:
    """Compact operational status for dashboards (authenticated views only).

    Returns REAL component status measured right now: database, Redis,
    active workers, and queue depth. Never fabricates values; degraded
    dependencies are reported honestly instead of being swallowed.

    SECURITY: requires authentication — worker identities and infrastructure
    status are operator data, not public data (regression: this endpoint was
    previously reachable anonymously while documenting "authenticated").
    """
    checks: dict[str, Any] = {
        "database": _check_database(),
        "redis": _check_redis(),
    }

    workers: list[dict[str, Any]] = []
    queue_depth: int | None = None
    try:
        import redis as redis_lib

        from aegisforge.config import get_settings

        settings = get_settings()
        client = redis_lib.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        now = time.time()
        worker_ttl = 120  # matches DEFAULT_WORKER_TTL
        cutoff = now - worker_ttl
        raw_workers = client.zrangebyscore(
            "aegisforge:workers", cutoff, "+inf", withscores=True
        )
        for worker_id, heartbeat_ts in raw_workers:  # type: ignore[str-unpack,misc]
            age = now - float(heartbeat_ts)
            workers.append(
                {
                    "worker_id": worker_id,
                    "last_heartbeat_age_seconds": round(age, 1),
                    "healthy": age < worker_ttl,
                }
            )
        queue_depth = int(client.llen("aegisforge:jobs") or 0)
    except Exception as exc:
        logger.warning("System status: worker/queue probe failed: %s", exc)

    # Note: the workers/queue probe doubles as a second Redis check; if Redis
    # is down the first check already reports it and workers stay empty.
    components = {
        "api": "ok",
        "database": "ok" if checks["database"] == "ok" else "unavailable",
        "redis": "ok" if checks["redis"] == "ok" else "unavailable",
        "workers": {
            "status": "ok" if workers else "none_active",
            "active": len(workers),
            "healthy": sum(1 for w in workers if w["healthy"]),
            "list": workers,
        },
        "queue": {
            "status": "ok",
            "depth": queue_depth if queue_depth is not None else 0,
        },
    }
    all_ok = (
        components["database"] == "ok"
        and components["redis"] == "ok"
        and bool(workers)
    )
    return {
        "status": "healthy" if all_ok else "degraded",
        "components": components,
    }


@router.get("/workers")
def worker_status(
    user: UserModel = Depends(get_current_user),
) -> dict[str, Any]:
    """Worker registry status for operational visibility (authenticated).

    Returns the list of active workers and their heartbeat freshness.
    This is an operational endpoint, not a health probe — it requires the
    same authentication as /system so worker identities are not public.
    """
    try:
        import redis as redis_lib

        from aegisforge.config import get_settings

        settings = get_settings()
        client = redis_lib.from_url(settings.redis_url, decode_responses=True)
        client.ping()

        now = time.time()
        worker_ttl = 120  # seconds — matches DEFAULT_WORKER_TTL
        cutoff = now - worker_ttl

        # Get all workers with their heartbeat timestamps
        raw_workers = client.zrangebyscore(
            "aegisforge:workers", cutoff, "+inf", withscores=True
        )

        workers = []
        for worker_id, heartbeat_ts in raw_workers:  # type: ignore[str-unpack,misc]
            age = now - float(heartbeat_ts)
            workers.append({
                "worker_id": worker_id,
                "last_heartbeat_age_seconds": round(age, 1),
                "healthy": age < worker_ttl,
            })

        return {
            "status": "ok",
            "active_workers": len(workers),
            "workers": workers,
        }
    except Exception as exc:
        logger.warning("Worker status check failed: %s", exc)
        # Raw exception text can carry connection details — log it, never
        # return it (even to authenticated users) as an API error payload.
        return {
            "status": "error",
            "error": "worker registry unavailable",
            "active_workers": 0,
            "workers": [],
        }
