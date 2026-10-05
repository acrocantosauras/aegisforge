"""Rate limiting tests (Phase 4.2 Part 11).

Verifies: disabled by default (dev-friendly), 429 with Retry-After when
exceeded, exempt paths bypass the limiter, and authenticated vs
anonymous keys are distinct.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from aegisforge.app import create_app
from aegisforge.config import Settings
from aegisforge.db.base import Base
from aegisforge.db.session import get_engine


def _make_client(**overrides) -> TestClient:
    get_engine.cache_clear()
    settings = Settings(
        database_url="sqlite:///:memory:",
        secret_key="test-secret",
        environment="test",
        **overrides,
    )
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    app = create_app(settings=settings)
    return TestClient(app)


class TestRateLimit:
    def test_disabled_by_default(self):
        """Rate limiting must not break normal development."""
        client = _make_client()
        for _ in range(500):
            resp = client.get("/api/v1/health")
        assert resp.status_code == 200

    def test_429_after_limit(self):
        client = _make_client(
            rate_limit_enabled=True,
            rate_limit_max_requests=5,
            rate_limit_window_seconds=60,
        )
        # 5 requests allowed (unauthenticated /requests → 401, still counted)
        for _ in range(5):
            resp = client.get("/api/v1/requests")
            assert resp.status_code == 401
        # 6th request is limited
        resp = client.get("/api/v1/requests")
        assert resp.status_code == 429
        assert resp.headers.get("Retry-After") is not None

    def test_metrics_exempt(self):
        client = _make_client(
            rate_limit_enabled=True,
            rate_limit_max_requests=2,
            rate_limit_window_seconds=60,
        )
        client.get("/api/v1/health")
        client.get("/api/v1/health")
        # Exempt path still reachable after the limit
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_auth_requests_have_higher_limit(self):
        client = _make_client(
            rate_limit_enabled=True,
            rate_limit_max_requests=3,
            rate_limit_auth_max_requests=10,
            rate_limit_window_seconds=3600,
        )
        # Register + login (unauthenticated → IP key, low limit)
        client.post(
            "/api/v1/auth/register",
            json={"email": "rl@test.com", "password": "TestPass123!", "full_name": "RL"},
        )
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "rl@test.com", "password": "TestPass123!"},
        )
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Anonymous calls exhaust the IP bucket → 429
        for _ in range(3):
            client.get("/api/v1/requests")
        assert client.get("/api/v1/requests").status_code == 429

        # Authenticated calls use the user bucket (still allowed)
        for _ in range(5):
            resp = client.get("/api/v1/requests", headers=headers)
            assert resp.status_code == 200


class TestWS13ProductionRateLimit:
    """WS13: Redis trouble must never create an unlimited request path."""

    def _production_client(self, **overrides) -> TestClient:  # type: ignore[no-untyped-def]
        get_engine.cache_clear()
        settings = Settings(
            database_url="sqlite:///:memory:",
            secret_key="ws13-production-secret-0123456789abcdef",
            environment="production",
            rate_limit_enabled=True,
            rate_limit_max_requests=5,
            rate_limit_window_seconds=60,
            redis_url="redis://localhost:6399/0",  # nothing listens here
            **overrides,
        )
        engine = get_engine(settings.database_url)
        Base.metadata.create_all(bind=engine)
        return TestClient(create_app(settings=settings))

    def test_redis_unreachable_still_bounded_in_production(self) -> None:
        """Connect-time Redis failure → bounded in-memory fallback, not NO limit.

        Regression: an unreachable Redis must not silently disable rate
        limiting in production (the unlimited-request path).
        """
        client = self._production_client()
        for _ in range(5):
            resp = client.get("/api/v1/requests")
            assert resp.status_code == 401
        resp = client.get("/api/v1/requests")
        assert resp.status_code == 429, "Redis down must not mean unlimited requests"
        assert resp.headers.get("Retry-After") is not None

    def test_runtime_redis_failure_fails_open_with_critical_log(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """Documented fail-open: Redis dying mid-flight allows requests,
        but must log CRITICAL so operators see the limiter is down."""
        import logging

        import aegisforge.security.rate_limit as rl

        class _Boom:
            def incr(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                raise ConnectionError("redis died mid-window")

            def expire(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                raise ConnectionError("redis died mid-window")

        prev_client, prev_available = rl._redis_client, rl._redis_available
        rl._redis_client, rl._redis_available = _Boom(), True
        try:
            client = self._production_client()
            with caplog.at_level(logging.CRITICAL, logger="aegisforge.security.rate_limit"):
                resp = client.get("/api/v1/requests")
            # Fail-open (documented): the request is allowed, not dropped…
            assert resp.status_code in {200, 401}
            # …and the outage is loudly recorded.
            assert any(r.levelno == logging.CRITICAL for r in caplog.records)
        finally:
            rl._redis_client, rl._redis_available = prev_client, prev_available

    def test_exempt_probe_survives_exhausted_limit(self) -> None:
        client = self._production_client()
        for _ in range(6):
            client.get("/api/v1/requests")
        # Liveness probe stays reachable even when the bucket is exhausted.
        assert client.get("/api/v1/health").status_code == 200

class TestWindowBoundary:
    """The window starts at the first request, not at the clock boundary.

    A boundary-aligned window lets a client send up to 2x the limit inside a
    few milliseconds across the boundary; the fixed-window TTL start removes
    that burst.
    """

    def test_window_does_not_reset_at_the_clock_boundary(self, monkeypatch) -> None:
        import aegisforge.security.rate_limit as rl

        client = _make_client(
            rate_limit_enabled=True,
            rate_limit_max_requests=5,
            rate_limit_window_seconds=60,
        )
        now = [1_000_000.4]  # just after a 60s boundary
        monkeypatch.setattr(rl.time, "time", lambda: now[0])

        for _ in range(5):
            assert client.get("/api/v1/requests").status_code == 401

        # Cross the wall-clock minute boundary (…020) while only ~30s of the
        # 60s window has elapsed: the window must NOT reset.
        now[0] = 1_000_030.1
        resp = client.get("/api/v1/requests")
        assert resp.status_code == 429, "boundary crossing must not refill the bucket"
        assert resp.headers.get("Retry-After") is not None

        # The window only refills once it has genuinely elapsed.
        now[0] = 1_000_065.5
        assert client.get("/api/v1/requests").status_code == 401
