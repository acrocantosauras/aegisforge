"""Health and readiness probes for AegisForge.

/health  — process alive (always 200 if the server is running).
           Liveness must NOT depend on external dependencies.
/ready   — process alive AND critical dependencies (DB, Redis) reachable.
           Returns 503 when dependencies are down.
/workers — operational visibility into worker registry.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Response, status

router = APIRouter(tags=["health"])

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
        return cached

    checks: dict[str, str] = {}

    # --- Database check ---
    try:
        from sqlalchemy import text

        from aegisforge.db.session import get_engine

        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning("Readiness check: database unavailable: %s", exc)
        checks["database"] = f"error: {exc}"

    # --- Redis check ---
    try:
        import redis as redis_lib

        from aegisforge.config import get_settings

        settings = get_settings()
        client = redis_lib.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        logger.warning("Readiness check: redis unavailable: %s", exc)
        checks["redis"] = f"error: {exc}"

    all_ok = all(v == "ok" for v in checks.values())
    result = {"status": "ready" if all_ok else "not_ready", "checks": checks, "all_ok": all_ok}

    # Cache the result
    _readiness_cache["last_check"] = now
    _readiness_cache["result"] = result

    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": result["status"], "checks": result["checks"]}


@router.get("/workers")
def worker_status() -> dict[str, Any]:
    """Worker registry status for operational visibility.

    Returns the list of active workers and their heartbeat freshness.
    This is an operational endpoint, not a health probe.
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
        return {
            "status": "error",
            "error": str(exc),
            "active_workers": 0,
            "workers": [],
        }
