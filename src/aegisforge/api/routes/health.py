"""Health and readiness probes for AegisForge.

/health — process alive (always 200 if the server is running).
/ready  — process alive AND critical dependencies (DB, Redis) reachable.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Response, status

router = APIRouter(tags=["health"])

logger = logging.getLogger(__name__)


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe: returns 200 when the process is alive."""
    return {"status": "ok"}


@router.get("/ready")
def readiness(response: Response) -> dict[str, Any]:
    """Readiness probe: checks database and Redis connectivity.

    Returns 200 when all critical dependencies are reachable, 503 otherwise.
    """
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
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if all_ok else "not_ready", "checks": checks}
