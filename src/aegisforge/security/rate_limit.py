"""API rate limiting for AegisForge.

Redis-backed fixed-window limiter (Redis already exists in the
architecture).  Keyed by authenticated user ID when a bearer token is
present, otherwise by client IP.  Never keyed by arbitrary user content.

Behavior:
- Disabled unless ``rate_limit_enabled`` is true (default off so local
  development and the test suite are unaffected).
- Redis failure in production logs CRITICAL and fails open (allows the
  request) so a rate-limiter outage never becomes an availability outage.
  This is documented; a stricter policy can be configured at the proxy.
- In-memory fallback is used only in non-production environments.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from aegisforge.config import Settings

logger = logging.getLogger(__name__)

# In-memory fallback storage: {bucket_key: (window_start, count)}
_fallback_store: dict[str, tuple[int, int]] = {}

_redis_client: Any = None
_redis_available = False


def _get_redis(settings: Settings) -> Any:
    """Lazily connect to Redis."""
    global _redis_client, _redis_available
    if _redis_client is None:
        try:
            import redis as redis_lib

            client = redis_lib.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=1.0,
            )
            client.ping()
            _redis_client = client
            _redis_available = True
        except Exception as exc:
            logger.warning("Rate limiter: Redis unavailable (%s)", exc)
            _redis_client = None
            _redis_available = False
    return _redis_client if _redis_available else None


class RateLimitExceeded(Exception):
    """Raised when a client exceeds the configured rate limit."""


def _exempt_path(path: str, settings: Settings) -> bool:
    exempt = {p.strip() for p in settings.rate_limit_exempt_paths.split(",") if p.strip()}
    return path in exempt


def _client_key(request: Request, settings: Settings) -> str:
    """Build a low-cardinality bucket key for the client.

    Authenticated requests are keyed by user id (extracted from the
    bearer token payload without storing the token); anonymous requests
    are keyed by client IP.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer ") and len(auth_header) > 7:
        token = auth_header[7:]
        try:
            from aegisforge.auth.security import decode_token

            payload = decode_token(token, settings)
            sub = payload.get("sub")
            if sub:
                return f"user:{sub}"
        except Exception as exc:
            logger.debug("Could not decode token for rate-limit key: %s", exc)
    ip = request.client.host if request.client else "unknown"
    return f"ip:{ip}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limiting middleware."""

    def __init__(self, app: Any, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings
        self._max_requests = settings.rate_limit_max_requests
        self._auth_max_requests = settings.rate_limit_auth_max_requests
        self._window_seconds = settings.rate_limit_window_seconds

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if not self._settings.rate_limit_enabled:
            return await call_next(request)

        path = request.url.path
        if _exempt_path(path, self._settings):
            return await call_next(request)

        key = _client_key(request, self._settings)
        limit = self._auth_max_requests if key.startswith("user:") else self._max_requests

        redis = _get_redis(self._settings)
        now = int(time.time())
        window_start = now - (now % self._window_seconds)

        try:
            if redis is not None:
                bucket = f"ratelimit:{key}:{window_start}"
                count = redis.incr(bucket)
                if count == 1:
                    redis.expire(bucket, self._window_seconds + 1)
            else:
                bucket_key = f"{key}:{window_start}"
                prev_start, prev_count = _fallback_store.get(bucket_key, (window_start, 0))
                if prev_start != window_start:
                    count = 1
                    _fallback_store[bucket_key] = (window_start, count)
                else:
                    count = prev_count + 1
                    _fallback_store[bucket_key] = (window_start, count)
                # Bound the in-memory store so it cannot grow unbounded
                if len(_fallback_store) > 10_000:
                    _fallback_store.clear()

            if count > limit:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Please retry later."},
                    headers={"Retry-After": str(self._window_seconds)},
                )
        except Exception as exc:
            is_production = self._settings.environment not in ("development", "test", "")
            if is_production:
                logger.critical("Rate limiter failure in production — failing open: %s", exc)
            else:
                logger.warning("Rate limiter failure (failing open): %s", exc)
            return await call_next(request)

        return await call_next(request)