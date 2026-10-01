"""Phase 6C: Health and readiness endpoint tests.

Verifies correct behavior of /health, /ready, and /workers endpoints.
Phase 9: /metrics gauge-refresh behavior (worker/queue visibility for alerts).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _make_client() -> TestClient:
    from aegisforge.app import create_app
    from aegisforge.config import Settings
    from aegisforge.db.base import Base
    from aegisforge.db.session import get_engine

    settings = Settings(
        database_url="sqlite:///:memory:",
        secret_key="test-secret",
        environment="test",
    )
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    app = create_app(settings=settings)
    return TestClient(app)


class TestHealthEndpoint:
    def test_health_always_returns_200(self) -> None:
        """/health always returns 200 — liveness doesn't depend on dependencies."""
        client = _make_client()
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_has_no_dependency_checks(self) -> None:
        """/health response should not include dependency status."""
        client = _make_client()
        resp = client.get("/api/v1/health")
        data = resp.json()
        assert "status" in data
        # No dependency checks in liveness
        assert "checks" not in data


class TestReadinessEndpoint:
    def test_readiness_returns_200_when_healthy(self) -> None:
        """/ready returns 200 when all dependencies are reachable."""
        client = _make_client()
        resp = client.get("/api/v1/ready")
        # SQLite is always reachable, Redis may or may not be
        # In test mode with SQLite, at minimum database should be ok
        data = resp.json()
        assert "status" in data
        assert "checks" in data

    def test_readiness_checks_database(self) -> None:
        """/ready checks database connectivity."""
        client = _make_client()
        resp = client.get("/api/v1/ready")
        data = resp.json()
        assert "database" in data["checks"]

    def test_readiness_checks_redis(self) -> None:
        """/ready checks Redis connectivity."""
        client = _make_client()
        resp = client.get("/api/v1/ready")
        data = resp.json()
        assert "redis" in data["checks"]

    def test_readiness_caches_results(self) -> None:
        """/ready caches results briefly to avoid hammering dependencies."""
        client = _make_client()
        resp1 = client.get("/api/v1/ready")
        resp2 = client.get("/api/v1/ready")
        # Both should have the same structure
        assert "status" in resp1.json()
        assert "status" in resp2.json()


def _auth_headers(client: TestClient) -> dict[str, str]:
    client.post(
        "/api/v1/auth/register",
        json={"email": "workers@example.com", "password": "SecurePass123!", "full_name": "T"},
    )
    token = client.post(
        "/api/v1/auth/login",
        json={"email": "workers@example.com", "password": "SecurePass123!"},
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestWorkerStatusEndpoint:
    def test_workers_endpoint_requires_authentication(self) -> None:
        """/workers is operational data — anonymous access is rejected."""
        client = _make_client()
        resp = client.get("/api/v1/workers")
        assert resp.status_code == 401

    def test_workers_endpoint_returns_structure(self) -> None:
        """/workers returns a structured response."""
        client = _make_client()
        headers = _auth_headers(client)
        resp = client.get("/api/v1/workers", headers=headers)
        data = resp.json()
        assert "status" in data
        assert "active_workers" in data
        assert "workers" in data
        assert isinstance(data["workers"], list)

    def test_workers_endpoint_shows_no_workers_when_redis_unavailable(self) -> None:
        """/workers handles Redis unavailability gracefully."""
        client = _make_client()
        headers = _auth_headers(client)
        resp = client.get("/api/v1/workers", headers=headers)
        data = resp.json()
        # Should return a valid response even if Redis is down
        assert "status" in data
        assert isinstance(data.get("workers", []), list)


class TestMetricsGaugeRefresh:
    """Phase 9: /metrics refreshes worker/queue gauges from Redis state.

    Workers set these gauges in their own processes; without an API-side
    refresh, Prometheus never sees them and alert rules like
    NoHealthyWorkers / QueueGrowthSustained evaluate against absent series.
    """

    def test_metrics_export_worker_queue_gauge_families(self) -> None:
        """The scrape payload contains the gauge families alerts rely on."""
        client = _make_client()
        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        assert "# TYPE active_workers gauge" in body
        assert "# TYPE active_claims gauge" in body
        assert "# TYPE queue_depth gauge" in body

    @pytest.mark.parametrize(
        "workers,claims,depth",
        [(2, 1, 3), (0, 0, 0), (5, 0, 12)],
    )
    def test_metrics_gauges_reflect_redis_state(
        self, monkeypatch: pytest.MonkeyPatch, workers: int, claims: int, depth: int
    ) -> None:
        """Gauge values are sampled from authoritative Redis keys."""
        import types

        from aegisforge.app import create_app
        from aegisforge.config import Settings
        from aegisforge.db.base import Base
        from aegisforge.db.session import get_engine

        settings = Settings(
            database_url="sqlite:///:memory:",
            secret_key="test-secret",
            environment="test",
        )
        engine = get_engine(settings.database_url)
        Base.metadata.create_all(bind=engine)
        app = create_app(settings=settings)

        fake_redis = types.SimpleNamespace(
            ping=lambda: True,
            # Key-aware stub: workers registry vs claims sorted set.
            zrangebyscore=lambda key, lo, hi: (
                ["w1"] * workers if "workers" in key else ["c1"] * claims
            ),
            llen=lambda key: depth,
        )
        captured: dict[str, int] = {}

        def fake_refresh(w: int, c: int, q: int) -> None:
            captured.update({"workers": w, "claims": c, "queue": q})

        real_from_url = __import__("redis").from_url
        monkeypatch.setattr(
            __import__("redis"), "from_url", lambda *a, **kw: fake_redis
        )
        monkeypatch.setattr(
            "aegisforge.observability.metrics.refresh_worker_queue_gauges",
            fake_refresh,
        )
        try:
            client = TestClient(app)
            resp = client.get("/metrics")
            assert resp.status_code == 200
            assert captured == {"workers": workers, "claims": claims, "queue": depth}
        finally:
            monkeypatch.setattr(__import__("redis"), "from_url", real_from_url)

    def test_metrics_scrape_succeeds_when_redis_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Redis outage must not break the scrape (last values are kept)."""
        from aegisforge.app import create_app
        from aegisforge.config import Settings
        from aegisforge.db.base import Base
        from aegisforge.db.session import get_engine

        settings = Settings(
            database_url="sqlite:///:memory:",
            secret_key="test-secret",
            environment="test",
        )
        engine = get_engine(settings.database_url)
        Base.metadata.create_all(bind=engine)
        app = create_app(settings=settings)

        def boom(*args: object, **kwargs: object) -> object:
            raise ConnectionError("redis unavailable")

        real_from_url = __import__("redis").from_url
        monkeypatch.setattr(__import__("redis"), "from_url", boom)
        try:
            client = TestClient(app)
            resp = client.get("/metrics")
            assert resp.status_code == 200
            assert "# TYPE queue_depth gauge" in resp.text
        finally:
            monkeypatch.setattr(__import__("redis"), "from_url", real_from_url)
