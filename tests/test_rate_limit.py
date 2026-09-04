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